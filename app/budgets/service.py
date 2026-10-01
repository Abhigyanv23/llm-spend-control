"""Budget enforcement with RESERVE-THEN-SETTLE.

    reserve()  BEFORE the provider call: hold the worst-case cost against every limit
    settle()   AFTER a successful call: release the hold, book the actual cost
    release()  AFTER a failed call: release the hold, book nothing

Like a hotel card pre-authorisation: the hold blocks the money immediately, and
the final charge (usually smaller) replaces it at checkout.
"""
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.budgets.periods import as_utc, current_periods
from app.budgets.policies import get_policy, limit_for, policies_for_request
from app.budgets.reconcile import reconcile_counters
from app.budgets.store import Counter, RedisBudgetStore
from app.db.models import BudgetAlert
from app.errors import BudgetExceededError, BudgetUnavailableError, OverrideRequiredError
from app.money import format_usd, nanos_to_usd, usd_str, usd_to_nanos
from app.schemas import Priority

logger = logging.getLogger(__name__)

BLOCK_THRESHOLD = Decimal("1.00")
# Errors that mean "the budget system itself is unavailable" (not a budget decision)
INFRA_ERRORS = (RedisError, SQLAlchemyError, OSError)


def iso_utc(moment: datetime) -> str:
    return as_utc(moment).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class LimitCheck:
    """One limit evaluated for one request. All amounts are nano-dollars, the same
    integers the Lua script compared, so Python and Redis always agree."""
    counter: Counter
    limit: int
    spent: int
    reserved: int
    estimate: int

    @property
    def projected(self) -> int:
        return self.spent + self.reserved + self.estimate

    @property
    def ratio(self) -> Decimal:
        # A zero limit means "no spend allowed": treat it as already at 100%
        return Decimal(self.projected) / Decimal(self.limit) if self.limit > 0 else Decimal(1)

    @property
    def exceeded(self) -> bool:
        return self.ratio >= BLOCK_THRESHOLD

    @property
    def percent_used(self) -> float:
        return round(float(self.ratio * 100), 2)

    @property
    def label(self) -> str:
        return f"{self.counter.scope}:{self.counter.scope_id}:{self.counter.period.name}"

    def to_dict(self) -> dict:
        c = self.counter
        return {"scope": c.scope, "scope_id": c.scope_id, "period": c.period.name,
                "period_key": c.period.key,
                "limit_usd": usd_str(nanos_to_usd(self.limit)),
                "spent_usd": usd_str(nanos_to_usd(self.spent)),
                "reserved_usd": usd_str(nanos_to_usd(self.reserved)),
                "estimated_cost_usd": usd_str(nanos_to_usd(self.estimate)),
                "projected_usd": usd_str(nanos_to_usd(self.projected)),
                "percent_used": self.percent_used,
                "resets_at": iso_utc(c.period.resets_at)}

    def describe(self) -> str:
        c = self.counter
        period = "daily" if c.period.name == "day" else "monthly"
        return (f"{c.scope.capitalize()} '{c.scope_id}' {period} budget of "
                f"{format_usd(nanos_to_usd(self.limit))} reached: spent "
                f"{format_usd(nanos_to_usd(self.spent))} + reserved "
                f"{format_usd(nanos_to_usd(self.reserved))} + this request's estimate "
                f"{format_usd(nanos_to_usd(self.estimate))} = "
                f"{format_usd(nanos_to_usd(self.projected))} ({self.percent_used}% of limit). "
                f"Resets at {iso_utc(c.period.resets_at)}.")


@dataclass
class Reservation:
    """The hold placed for one request. Settle or release it exactly once."""
    counters: list[Counter]
    amount: int                     # nano-dollars held on EVERY counter
    estimate_usd: Decimal
    now: datetime                   # request time: fixes WHICH day/month keys are used
    held: bool                      # False if the budget check was skipped (fail-open)
    warnings: list[LimitCheck] = field(default_factory=list)
    overridden: list[LimitCheck] = field(default_factory=list)
    override_reason: str | None = None
    degraded_reason: str | None = None

    @property
    def status(self) -> str:
        if self.degraded_reason:
            return "unchecked"
        if self.overridden:
            return "overridden"
        return "warning" if self.warnings else "ok"

    def warning_header(self) -> str | None:
        """Value for the X-Budget-Warning response header (None = no header)."""
        if self.degraded_reason:
            return "budget-check-unavailable"
        parts = [f"{w.label}={w.percent_used:.1f}%" for w in self.warnings]
        parts += [f"{o.label}={o.percent_used:.1f}% (overridden)" for o in self.overridden]
        return "; ".join(parts) or None

    def metadata(self) -> dict:
        items = ([{"kind": "warning", **w.to_dict()} for w in self.warnings]
                 + [{"kind": "overridden", **o.to_dict()} for o in self.overridden])
        if self.degraded_reason:
            items.append({"kind": "unchecked", "reason": self.degraded_reason})
        return {"budget": {"status": self.status,
                           "estimated_cost_usd": usd_str(self.estimate_usd)},
                "budget_warnings": items}


class BudgetService:
    def __init__(self, store: RedisBudgetStore, session_factory: async_sessionmaker,
                 warn_threshold: Decimal = Decimal("0.8"), fail_mode: str = "open",
                 verifier_scope: tuple[str, str] | None = None):
        self.store = store
        self.session_factory = session_factory
        self.warn_threshold = warn_threshold
        self.fail_mode = fail_mode
        # (team_id, feature) that verification spend is charged to; reconciliation needs it
        self.verifier_scope = verifier_scope

    # ------------------------------------------------------------ reserve

    async def reserve(self, *, request_id: str, team_id: str, feature: str,
                      priority: Priority, estimate_usd: Decimal,
                      override_reason: str | None, now: datetime) -> Reservation:
        counters = [Counter(scope, scope_id, period)
                    for scope, scope_id in (("team", team_id), ("feature", feature))
                    for period in current_periods(now)]
        amount = usd_to_nanos(estimate_usd)
        allow_over = priority.can_override_budget and bool(override_reason)

        try:
            async with self.session_factory() as session:
                policies = await policies_for_request(session, team_id, feature)
            limits = [limit_for(policies.get((c.scope, c.scope_id)), c.period.name)
                      for c in counters]
            limits_nanos = [None if lim is None else usd_to_nanos(lim) for lim in limits]
            # ONE atomic round trip: check all 4 counters and hold `amount` on all of them.
            # Counters without a limit are still reserved/settled, so spend is tracked for
            # every team and feature and a newly created policy sees today's spend at once.
            allowed, values = await self.store.reserve(counters, limits_nanos, amount,
                                                       allow_over, now)
        except INFRA_ERRORS as exc:
            return self._on_unavailable(counters, amount, estimate_usd, now, exc)

        checks = [LimitCheck(c, lim, v.spent, v.reserved, amount)
                  for c, lim, v in zip(counters, limits_nanos, values) if lim is not None]
        # Strictest outcome wins: the most-exceeded limit is the one reported
        exceeded = sorted((ch for ch in checks if ch.exceeded),
                          key=lambda ch: ch.ratio, reverse=True)

        if not allowed:
            await self._record_alerts(exceeded, BLOCK_THRESHOLD, request_id, now)
            raise self._block_error(exceeded, priority, override_reason)

        warnings = [ch for ch in checks
                    if not ch.exceeded and ch.ratio >= self.warn_threshold]
        await self._record_alerts(warnings, self.warn_threshold, request_id, now)
        await self._record_alerts(exceeded, BLOCK_THRESHOLD, request_id, now)
        return Reservation(counters=counters, amount=amount, estimate_usd=estimate_usd,
                           now=now, held=True, warnings=warnings, overridden=exceeded,
                           override_reason=override_reason if exceeded else None)

    def _on_unavailable(self, counters: list[Counter], amount: int, estimate_usd: Decimal,
                        now: datetime, exc: Exception) -> Reservation:
        reason = f"{type(exc).__name__}: {exc}"[:200]
        if self.fail_mode == "closed":
            logger.error("Budget check unavailable, rejecting (fail-closed): %s", reason)
            raise BudgetUnavailableError(type(exc).__name__)
        logger.warning("Budget check unavailable, ALLOWING request (fail-open): %s", reason)
        return Reservation(counters=counters, amount=amount, estimate_usd=estimate_usd,
                           now=now, held=False, degraded_reason=reason)

    def _block_error(self, exceeded: list[LimitCheck], priority: Priority,
                     override_reason: str | None) -> Exception:
        if not exceeded:   # cannot happen: Lua and Python evaluate the same integers
            return BudgetExceededError("Budget limit reached", {})
        worst = exceeded[0]
        details = {**worst.to_dict(), "priority": priority.value,
                   "limits_exceeded": [ch.label for ch in exceeded]}
        if priority.can_override_budget:
            return OverrideRequiredError(
                worst.describe() + f" Priority '{priority.value}' requests may proceed by "
                "sending an X-Budget-Override header with a reason.", details)
        message = worst.describe()
        if override_reason:
            message += " X-Budget-Override is only honoured for high/critical priority."
        return BudgetExceededError(message, details)

    # ------------------------------------------------------------ settle / release

    async def settle(self, reservation: Reservation, actual_usd: Decimal) -> None:
        await self._settle(reservation, usd_to_nanos(actual_usd))

    async def release(self, reservation: Reservation) -> None:
        await self._settle(reservation, 0)

    async def _settle(self, reservation: Reservation, actual: int) -> None:
        held = reservation.amount if reservation.held else 0
        if held == 0 and actual == 0:
            return
        try:
            # Settle against the reservation's OWN keys: a request that started at
            # 23:59:59 is charged to that day even if it finishes after midnight
            await self.store.settle(reservation.counters, held, actual, reservation.now)
        except RedisError as exc:
            # Don't fail a request that already succeeded. The hold/spend drift is
            # corrected by reconciliation from Postgres (the source of truth).
            logger.warning("Budget settle failed (reconciliation will correct it): %s", exc)

    # ------------------------------------------------------------ alerts

    async def _record_alerts(self, checks: list[LimitCheck], threshold: Decimal,
                             request_id: str, now: datetime) -> None:
        threshold = threshold.quantize(Decimal("0.01"))
        for ch in checks:
            try:
                # Cheap Redis dedup first, so requests near a limit don't hit Postgres every time
                key = f"alert:{ch.counter.base_key}:{threshold}"
                if not await self.store.claim_once(key, ch.counter.period.ttl_seconds(now)):
                    continue
                logger.warning("BUDGET ALERT %s crossed %s%% (%.2f%% used)", ch.label,
                               int(threshold * 100), ch.percent_used)
                async with self.session_factory() as session:
                    session.add(BudgetAlert(
                        scope=ch.counter.scope, scope_id=ch.counter.scope_id,
                        period=ch.counter.period.name, period_key=ch.counter.period.key,
                        threshold=threshold, projected_usd=nanos_to_usd(ch.projected),
                        limit_usd=nanos_to_usd(ch.limit), request_id=uuid.UUID(request_id)))
                    await session.commit()
            except IntegrityError:
                pass    # already recorded (e.g. Redis was flushed): the unique key dedups
            except Exception:
                logger.exception("Failed to record budget alert for %s", ch.label)

    # ------------------------------------------------------------ status / reconcile

    async def status(self, scope: str, scope_id: str, now: datetime) -> dict:
        async with self.session_factory() as session:
            policy = await get_policy(session, scope, scope_id)
        periods = current_periods(now)
        counters = [Counter(scope, scope_id, p) for p in periods]
        try:
            values = await self.store.read(counters)
        except RedisError as exc:
            raise BudgetUnavailableError(type(exc).__name__) from exc

        result: dict = {"scope": scope, "scope_id": scope_id, "policy": policy}
        for counter, v in zip(counters, values):
            limit = limit_for(policy, counter.period.name)
            used = v.spent + v.reserved
            limit_n = None if limit is None else usd_to_nanos(limit)
            result[counter.period.name] = {
                "period": counter.period.name, "period_key": counter.period.key,
                "spent_usd": nanos_to_usd(v.spent), "reserved_usd": nanos_to_usd(v.reserved),
                "limit_usd": limit,
                "percent_used": (None if not limit_n
                                 else round(float(Decimal(used) / limit_n * 100), 2)),
                "remaining_usd": (None if limit_n is None
                                  else nanos_to_usd(max(limit_n - used, 0))),
                "resets_at": counter.period.resets_at,
            }
        return result

    async def reconcile(self, now: datetime) -> dict:
        return await reconcile_counters(self.session_factory, self.store, now,
                                        self.verifier_scope)
