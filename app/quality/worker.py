"""The verification worker: consume sampled jobs, ask the reference model, judge, store.

Per job:
  1. parse (malformed -> dead letter at once: a poison message never succeeds on retry)
  2. already verified? -> ack and skip (idempotency: the job may have been delivered twice)
  3. reserve the verification budget (blocked -> store verdict 'skipped', ack)
  4. reference call + judge; settle what was actually spent (always, via finally)
  5. store verification (+ routing miss on 'fail') in one transaction
  6. XACK, only now that the result is durable
Any exception in 3-5 leaves the job un-acked: XAUTOCLAIM redelivers it after job_timeout_s,
up to max_attempts, then it goes to the dead-letter stream.
"""
import asyncio
import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.budgets import BudgetService
from app.db.models import RoutingMiss, Verification
from app.errors import BudgetExceededError, OverrideRequiredError
from app.money import usd_str
from app.providers.base import ProviderAdapter
from app.quality.config import QualityConfig
from app.quality.jobs import VerificationJob
from app.quality.judges import Judge, JudgeResult
from app.quality.queue import Entry, VerificationQueue
from app.quality.store import save_verification, verification_exists
from app.registry import ModelRegistry
from app.schemas import ChatRequest, Message, Priority
from app.tokens import estimate_input_tokens

logger = logging.getLogger(__name__)

BUDGET_BLOCKS = (BudgetExceededError, OverrideRequiredError)
# Pause after an empty poll. Redis already blocks up to 1 s inside XREADGROUP, but a backend
# that returns immediately (an emulator, a proxy) would otherwise turn the loop into a
# 100%-CPU spin that also starves every other task on the event loop.
IDLE_SLEEP_S = 0.1


@dataclass
class WorkerStats:
    outcomes: Counter = field(default_factory=Counter)

    def add(self, outcome: str) -> None:
        self.outcomes[outcome] += 1

    @property
    def processed(self) -> int:
        return sum(self.outcomes.values())

    def summary(self) -> str:
        keys = ("pass", "fail", "inconclusive", "skipped", "duplicate", "dead_letter", "error")
        return (f"{self.processed} job(s): "
                + ", ".join(f"{self.outcomes.get(k, 0)} {k}" for k in keys))


class VerificationWorker:
    def __init__(self, *, queue: VerificationQueue, quality: QualityConfig,
                 registry: ModelRegistry, adapters: dict[str, ProviderAdapter],
                 budgets: BudgetService, session_factory: async_sessionmaker, judge: Judge,
                 consumer: str, concurrency: int = 4, min_idle_ms: int | None = None):
        self.queue = queue
        self.quality = quality
        self.cfg = quality.verification
        self.registry = registry
        self.adapters = adapters
        self.budgets = budgets
        self.session_factory = session_factory
        self.judge = judge
        self.consumer = consumer
        self.concurrency = concurrency
        self.min_idle_ms = (int(self.cfg.job_timeout_s * 1000) if min_idle_ms is None
                            else min_idle_ms)

    # ------------------------------------------------------------------ loops

    async def run_once(self, max_batches: int = 10_000) -> WorkerStats:
        """Drain what is available now, then return (for tests and `--once`). Failed jobs
        that are not yet idle long enough to be reclaimed stay pending for a later run."""
        await self.queue.ensure_group()
        stats = WorkerStats()
        for _ in range(max_batches):
            batch = await self._next_batch(block_ms=None)
            if not batch:
                break
            await self._process_batch(batch, stats)
        await self.queue.remove_consumer_if_idle(self.consumer)
        return stats

    async def run_forever(self, stop: asyncio.Event) -> WorkerStats:
        await self.queue.ensure_group()
        stats = WorkerStats()
        logger.info("Worker '%s' listening on '%s' (group '%s', concurrency %d)",
                    self.consumer, self.queue.stream, self.queue.group, self.concurrency)
        while not stop.is_set():
            # Block at most 1 s so a shutdown request is noticed quickly
            batch = await self._next_batch(block_ms=1000)
            if batch:
                await self._process_batch(batch, stats)
                logger.info("Totals so far: %s", stats.summary())
            else:
                await asyncio.sleep(IDLE_SLEEP_S)
        # Graceful stop: leave the group tidy (only if nothing is pending for this consumer;
        # otherwise keep it so its jobs can still be reclaimed)
        await self.queue.remove_consumer_if_idle(self.consumer)
        logger.info("Worker '%s' stopped. %s", self.consumer, stats.summary())
        return stats

    async def _next_batch(self, block_ms: int | None) -> list[tuple[str, dict, int]]:
        # Stale jobs first: they have waited longest
        batch = [(mid, fields, await self.queue.delivery_count(mid))
                 for mid, fields in await self.queue.reclaim(self.consumer, self.min_idle_ms,
                                                             self.concurrency)]
        room = self.concurrency - len(batch)
        if room > 0:
            fresh = await self.queue.read(self.consumer, room, None if batch else block_ms)
            batch += [(mid, fields, 1) for mid, fields in fresh]
        return batch

    async def _process_batch(self, batch: list[tuple[str, dict, int]],
                             stats: WorkerStats) -> None:
        # Bounded concurrency: at most `concurrency` jobs in flight (backpressure: we only read
        # as many as we can handle, the rest wait safely in the stream)
        results = await asyncio.gather(*(self.process(mid, fields, attempt)
                                         for mid, fields, attempt in batch),
                                       return_exceptions=True)
        for result in results:
            stats.add(result if isinstance(result, str) else "error")

    # ------------------------------------------------------------------ one job

    async def process(self, message_id: str, fields: dict, attempt: int) -> str:
        try:
            job = VerificationJob.from_fields(fields)
        except Exception as exc:
            await self.queue.dead_letter(message_id, fields,
                                         f"poison message: {type(exc).__name__}: {exc}", attempt)
            return "dead_letter"

        if attempt > self.cfg.max_attempts:
            await self.queue.dead_letter(message_id, fields,
                                         f"exceeded max_attempts={self.cfg.max_attempts}",
                                         attempt)
            return "dead_letter"

        if await verification_exists(self.session_factory, job.request_id):
            await self.queue.ack(message_id)
            return "duplicate"

        try:
            outcome = await self.verify(job, attempt, message_id)
        except Exception as exc:
            if attempt >= self.cfg.max_attempts:
                await self.queue.dead_letter(message_id, fields,
                                             f"{type(exc).__name__}: {exc}", attempt)
                return "dead_letter"
            logger.warning("Verification of request_id=%s failed on attempt %d/%d (%s: %s); "
                           "left pending for retry", job.request_id, attempt,
                           self.cfg.max_attempts, type(exc).__name__, exc)
            return "error"

        await self.queue.ack(message_id)        # only after the result is durable
        return outcome

    async def verify(self, job: VerificationJob, attempt: int, message_id: str) -> str:
        reference = self.registry.get(self.cfg.reference_model)
        request = ChatRequest(
            messages=[Message(**m) for m in job.messages], team_id=self.cfg.budget_team_id,
            feature=self.cfg.budget_feature, priority=Priority.normal,
            max_tokens=self.cfg.reference_max_tokens, temperature=0.0)
        prompt = job.prompt_text()

        # Worst case for BOTH calls: the reference answer (all of max_tokens) + the judge
        # grading a reference of that size
        placeholder = "x" * min(self.cfg.reference_max_tokens * 4, 6000)
        estimate = (reference.worst_case_cost(estimate_input_tokens(request.messages),
                                              self.cfg.reference_max_tokens)
                    + self.judge.worst_case_cost(prompt=prompt, candidate=job.output,
                                                 reference=placeholder))
        try:
            reservation = await self.budgets.reserve(
                request_id=job.request_id, team_id=self.cfg.budget_team_id,
                feature=self.cfg.budget_feature, priority=Priority.normal,
                estimate_usd=estimate, override_reason=None, now=datetime.now(UTC))
        except BUDGET_BLOCKS as exc:
            # Verification is optional: when its budget is spent we record that and move on
            inserted = await self._store(job, attempt, message_id, verdict="skipped",
                                         reason=f"verification budget: {exc.message}",
                                         cost=Decimal(0))
            return "skipped" if inserted else "duplicate"

        spent = Decimal(0)
        try:
            adapter = self.adapters[reference.provider]
            ref = await adapter.complete(request, reference.name)
            spent += reference.cost(ref.input_tokens, ref.output_tokens)
            result = await self.judge.judge(prompt=prompt, candidate=job.output,
                                            reference=ref.output)
            spent += result.cost_usd
        finally:
            # Settle exactly what was spent even if the judge failed after the reference call
            # succeeded: that money is gone either way. spent == 0 is a plain release.
            await asyncio.shield(self.budgets.settle(reservation, spent))

        inserted = await self._store(job, attempt, message_id, verdict=result.verdict,
                                     result=result, reason=result.reason, cost=spent,
                                     reference=reference.name,
                                     reference_tokens=(ref.input_tokens, ref.output_tokens),
                                     better_tier=reference.tier)
        # Not inserted = another delivery of this job won the race (UNIQUE request_id)
        return result.verdict if inserted else "duplicate"

    async def _store(self, job: VerificationJob, attempt: int, message_id: str, *, verdict: str,
                     reason: str, cost: Decimal, result: JudgeResult | None = None,
                     reference: str | None = None, reference_tokens: tuple[int, int] = (0, 0),
                     better_tier: int | None = None) -> bool:
        meta = {"message_id": message_id, "request_created_at": job.created_at,
                "sample_rate": job.sample_rate, "priority": job.priority,
                "reference_tokens": {"input": reference_tokens[0],
                                     "output": reference_tokens[1]}}
        if result is not None:
            meta["judge"] = result.to_dict()
        verification = Verification(
            request_id=uuid.UUID(job.request_id), team_id=job.team_id, feature=job.feature,
            model=job.model, tier=job.tier, routing_source=job.routing_source,
            classifier_confidence=job.classifier_confidence, reference_model=reference,
            judge=result.judge if result else None, verdict=verdict,
            score=result.score if result else None, reason=(reason or "")[:2000],
            verification_cost_usd=cost, attempts=attempt, meta=meta)

        miss = None
        if verdict == "fail":
            # A labelled example: "this request needed a stronger tier than it got"
            privacy = self.quality.privacy
            miss = RoutingMiss(
                request_id=uuid.UUID(job.request_id), team_id=job.team_id, feature=job.feature,
                prompt=prompt_for_storage(job.prompt_text(), privacy.store_prompts,
                                          privacy.max_prompt_chars),
                chosen_model=job.model, chosen_tier=job.tier, better_model=reference,
                better_tier=better_tier, reason=(reason or "")[:2000],
                classifier_confidence=job.classifier_confidence,
                classifier_features=job.classifier_features or None)

        inserted = await save_verification(self.session_factory, verification, miss)
        logger.info("Verified request_id=%s model=%s -> %s%s (cost $%s)", job.request_id,
                    job.model, verdict, "" if inserted else " [already stored]", usd_str(cost))
        return inserted


def prompt_for_storage(text: str, store_prompts: bool, max_chars: int) -> str | None:
    """Data minimisation: keep nothing, or at most max_chars."""
    if not store_prompts:
        return None
    return text[:max_chars]
