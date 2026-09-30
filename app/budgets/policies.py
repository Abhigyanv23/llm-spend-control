"""Repository for budget_policies: the only module that knows how policies are stored."""
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BudgetPolicy


async def policies_for_request(session: AsyncSession, team_id: str,
                               feature: str) -> dict[tuple[str, str], BudgetPolicy]:
    """Enabled policies that apply to a request: its team's and its feature's (0, 1 or 2 rows).
    Uses the unique (scope, scope_id) index, so it's a cheap lookup."""
    stmt = select(BudgetPolicy).where(
        BudgetPolicy.enabled.is_(True),
        or_((BudgetPolicy.scope == "team") & (BudgetPolicy.scope_id == team_id),
            (BudgetPolicy.scope == "feature") & (BudgetPolicy.scope_id == feature)))
    rows = (await session.scalars(stmt)).all()
    return {(p.scope, p.scope_id): p for p in rows}


async def get_policy(session: AsyncSession, scope: str, scope_id: str) -> BudgetPolicy | None:
    stmt = select(BudgetPolicy).where(BudgetPolicy.scope == scope,
                                      BudgetPolicy.scope_id == scope_id)
    return await session.scalar(stmt)


async def list_policies(session: AsyncSession) -> list[BudgetPolicy]:
    stmt = select(BudgetPolicy).order_by(BudgetPolicy.scope, BudgetPolicy.scope_id)
    return list((await session.scalars(stmt)).all())


async def upsert_policy(session: AsyncSession, scope: str, scope_id: str,
                        daily_limit_usd: Decimal | None, monthly_limit_usd: Decimal | None,
                        enabled: bool = True) -> BudgetPolicy:
    """Create or replace a policy. The caller commits.
    (The unique (scope, scope_id) constraint protects against two concurrent creates.)"""
    policy = await get_policy(session, scope, scope_id)
    if policy is None:
        policy = BudgetPolicy(scope=scope, scope_id=scope_id)
        session.add(policy)
    policy.daily_limit_usd = daily_limit_usd
    policy.monthly_limit_usd = monthly_limit_usd
    policy.enabled = enabled
    policy.updated_at = datetime.now(UTC)
    await session.flush()
    return policy


def limit_for(policy: BudgetPolicy | None, period_name: str) -> Decimal | None:
    """The limit that applies for a period, or None if unlimited / no policy."""
    if policy is None or not policy.enabled:
        return None
    return policy.daily_limit_usd if period_name == "day" else policy.monthly_limit_usd
