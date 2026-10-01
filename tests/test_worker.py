"""Verification worker on fakeredis Streams + SQLite: process, ack, idempotency, retry,
dead-letter, poison messages, budget-skipped verifications."""
import dataclasses
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import RoutingMiss, Verification
from app.errors import ProviderError
from app.providers.base import ProviderAdapter
from app.providers.mock_adapter import MockAdapter
from app.quality import SimilarityJudge, load_quality_config
from app.quality.jobs import VerificationJob
from app.quality.queue import VerificationQueue
from app.quality.worker import VerificationWorker
from app.registry import ModelRegistry
from app.routing import load_routing_config

REGISTRY = ModelRegistry("config/models.yaml")
QUALITY = load_quality_config("config/quality.yaml", REGISTRY,
                              load_routing_config("config/routing.yaml", "dev", REGISTRY),
                              {"mock", "ollama"})
LONG_PROMPT = " ".join(f"point{i}" for i in range(300))       # ~2,100 chars: mock-echo truncates


def with_verification(**changes):
    return dataclasses.replace(QUALITY, verification=dataclasses.replace(QUALITY.verification,
                                                                         **changes))


async def make_job(text: str = "Hello gateway", model: str = "mock-echo", tier: int = 1,
                   request_id: str | None = None) -> VerificationJob:
    """A job exactly as the gateway would build it: the cheap answer comes from the mock."""
    from app.schemas import ChatRequest, Message
    request = ChatRequest(team_id="t1", feature="f1", max_tokens=4000,
                          messages=[Message(role="user", content=text)])
    output = (await MockAdapter().complete(request, model)).output
    return VerificationJob(
        request_id=request_id or str(uuid.uuid4()),
        created_at=datetime.now(UTC).isoformat(), team_id="t1", feature="f1",
        priority="normal", messages=[{"role": "user", "content": text}], output=output,
        model=model, tier=tier, routing_source="routed", classifier_confidence=0.5,
        classifier_features={"input_tokens": 600, "has_code": False}, sample_rate=0.5)


class FlakyAdapter(ProviderAdapter):
    """Fails the first `failures` calls with a retryable provider error, then behaves."""
    name = "mock"

    def __init__(self, failures: int):
        self.failures, self.calls, self.inner = failures, 0, MockAdapter()

    async def complete(self, request, model):
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderError("mock", "Upstream timeout", status_code=504, retryable=True)
        return await self.inner.complete(request, model)


@pytest.fixture
def queue(redis_client):
    return VerificationQueue.from_config(redis_client, QUALITY.verification)


@pytest.fixture
def make_worker(queue, budgets, session_factory):
    def _make(quality=QUALITY, adapter=None, judge=None, min_idle_ms=0):
        return VerificationWorker(
            queue=queue, quality=quality, registry=REGISTRY,
            adapters={"mock": adapter or MockAdapter()}, budgets=budgets,
            session_factory=session_factory, judge=judge or SimilarityJudge(0.8),
            consumer="test-worker", concurrency=4, min_idle_ms=min_idle_ms)
    return _make


async def rows(session_factory, model):
    async with session_factory() as s:
        return list((await s.scalars(select(model))).all())


async def test_short_prompt_passes_and_is_acked(queue, make_worker, session_factory):
    job = await make_job()
    await queue.enqueue(job)
    stats = await make_worker().run_once()
    assert stats.outcomes == {"pass": 1}
    [v] = await rows(session_factory, Verification)
    assert (str(v.request_id), v.verdict, v.score, v.reference_model) == (
        job.request_id, "pass", 1.0, "mock-large")
    assert v.verification_cost_usd > 0 and v.attempts == 1
    assert await rows(session_factory, RoutingMiss) == []
    assert (await queue.stats())["pending"] == 0


async def test_long_prompt_fails_and_records_a_routing_miss(queue, make_worker, session_factory):
    job = await make_job(LONG_PROMPT)
    await queue.enqueue(job)
    assert (await make_worker().run_once()).outcomes == {"fail": 1}
    [v] = await rows(session_factory, Verification)
    assert v.verdict == "fail" and v.score < 0.3
    [miss] = await rows(session_factory, RoutingMiss)
    assert (miss.chosen_model, miss.chosen_tier, miss.better_model, miss.better_tier) == (
        "mock-echo", 1, "mock-large", 3)
    # Stored, but capped at privacy.max_prompt_chars (data minimisation)
    assert len(LONG_PROMPT) > 2000 and miss.prompt == LONG_PROMPT[:2000]
    assert miss.classifier_features["input_tokens"] == 600


async def test_privacy_store_prompts_false_keeps_no_text(queue, make_worker, session_factory):
    quality = dataclasses.replace(QUALITY, privacy=dataclasses.replace(QUALITY.privacy,
                                                                       store_prompts=False))
    await queue.enqueue(await make_job(LONG_PROMPT))
    await make_worker(quality=quality).run_once()
    [miss] = await rows(session_factory, RoutingMiss)
    assert miss.prompt is None and miss.classifier_features    # features kept, text not


async def test_redelivered_job_is_not_stored_twice(queue, make_worker, session_factory):
    job = await make_job()
    await queue.enqueue(job)
    await queue.enqueue(job)                         # same request_id delivered twice
    stats = await make_worker().run_once()
    # Both copies land in the same batch and run concurrently, so both pass the "already
    # verified?" pre-check: the UNIQUE constraint is what actually stops the second insert
    assert stats.outcomes == {"pass": 1, "duplicate": 1}
    assert len(await rows(session_factory, Verification)) == 1
    assert (await queue.stats())["pending"] == 0     # both acked


async def test_job_already_verified_is_skipped_before_any_spend(queue, make_worker,
                                                                session_factory, budgets):
    """Sequential redelivery (the common case: a crash after insert, before XACK) is caught
    by the cheap pre-check, so no second reference call is paid for."""
    job = await make_job()
    await queue.enqueue(job)
    await make_worker().run_once()
    spent = (await budgets.status("team", "quality-verifier", datetime.now(UTC)))["day"]
    await queue.enqueue(job)
    assert (await make_worker().run_once()).outcomes == {"duplicate": 1}
    after = (await budgets.status("team", "quality-verifier", datetime.now(UTC)))["day"]
    assert after["spent_usd"] == spent["spent_usd"]


async def test_retries_then_dead_letters(queue, make_worker, session_factory, budgets):
    worker = make_worker(quality=with_verification(max_attempts=2),
                         adapter=FlakyAdapter(failures=99))
    job = await make_job()
    await queue.enqueue(job)
    stats = await worker.run_once()
    assert stats.outcomes == {"error": 1, "dead_letter": 1}      # attempt 1, then attempt 2
    q = await queue.stats()
    assert (q["pending"], q["dead_letter"]) == (0, 1)
    dead = await queue.client.xrange(QUALITY.verification.dead_letter_stream)
    assert dead[0][1]["request_id"] == job.request_id and dead[0][1]["attempts"] == "2"
    assert "Upstream timeout" in dead[0][1]["reason"]
    assert await rows(session_factory, Verification) == []
    # Every failed attempt released its budget hold: nothing reserved, nothing spent
    status = await budgets.status("team", "quality-verifier", datetime.now(UTC))
    assert status["day"]["reserved_usd"] == 0 and status["day"]["spent_usd"] == 0


async def test_transient_failure_succeeds_on_retry(queue, make_worker, session_factory):
    await queue.enqueue(await make_job())
    stats = await make_worker(adapter=FlakyAdapter(failures=1)).run_once()
    assert stats.outcomes == {"error": 1, "pass": 1}
    [v] = await rows(session_factory, Verification)
    assert v.attempts == 2


async def test_failed_job_waits_for_the_visibility_timeout(queue, make_worker):
    """With a real timeout, a failed job is NOT retried immediately: it stays pending."""
    await queue.enqueue(await make_job())
    stats = await make_worker(adapter=FlakyAdapter(failures=1),
                              min_idle_ms=60_000).run_once()
    assert stats.outcomes == {"error": 1}
    assert (await queue.stats())["pending"] == 1


async def test_poison_message_goes_straight_to_dead_letter(queue, make_worker, redis_client):
    await redis_client.xadd(QUALITY.verification.stream, {"payload": "{not json"})
    assert (await make_worker().run_once()).outcomes == {"dead_letter": 1}
    dead = await redis_client.xrange(QUALITY.verification.dead_letter_stream)
    assert "poison message" in dead[0][1]["reason"]


async def test_budget_blocked_verification_is_skipped(queue, make_worker, session_factory,
                                                      set_policy):
    await set_policy("team", "quality-verifier", daily="0")
    await queue.enqueue(await make_job())
    assert (await make_worker().run_once()).outcomes == {"skipped": 1}
    [v] = await rows(session_factory, Verification)
    assert v.verdict == "skipped" and v.verification_cost_usd == 0
    assert "verification budget" in v.reason
    assert (await queue.stats())["pending"] == 0                  # acked, not retried forever


async def test_verification_spend_is_charged_to_the_verifier_budget(queue, make_worker,
                                                                    session_factory, budgets):
    await queue.enqueue(await make_job())
    await make_worker().run_once()
    [v] = await rows(session_factory, Verification)
    status = await budgets.status("team", "quality-verifier", datetime.now(UTC))
    assert status["day"]["spent_usd"] == v.verification_cost_usd
    assert status["day"]["reserved_usd"] == 0


async def test_judge_failure_still_settles_the_reference_cost(queue, make_worker, budgets):
    class BrokenJudge(SimilarityJudge):
        async def judge(self, **kwargs):
            raise RuntimeError("judge crashed")

    worker = make_worker(quality=with_verification(max_attempts=1), judge=BrokenJudge())
    await queue.enqueue(await make_job())
    assert (await worker.run_once()).outcomes == {"dead_letter": 1}
    status = await budgets.status("team", "quality-verifier", datetime.now(UTC))
    # The reference call happened and cost money, even though no verdict was stored
    assert status["day"]["spent_usd"] > 0 and status["day"]["reserved_usd"] == 0


async def test_queue_stats_and_consumer_cleanup(queue, make_worker, redis_client):
    assert (await queue.stats())["length"] == 0          # works before any group exists
    await queue.enqueue(await make_job())
    await queue.enqueue(await make_job())
    await make_worker().run_once()
    stats = await queue.stats()
    assert (stats["length"], stats["pending"], stats["dead_letter"]) == (2, 0, 0)
    assert stats["consumers"] == []                       # --once consumer removed when idle


async def test_job_round_trip_and_versioning():
    job = await make_job()
    assert VerificationJob.from_fields(job.to_fields()) == job
    future = {**job.to_fields(), "payload": job.to_fields()["payload"].replace(
        '"version": 1', '"version": 99')}
    with pytest.raises(ValueError, match="unsupported job version"):
        VerificationJob.from_fields(future)


def test_cost_free_judge_has_no_reservation_overhead():
    assert SimilarityJudge().worst_case_cost(prompt="a", candidate="b", reference="c") == Decimal(0)


async def test_run_forever_processes_then_stops_when_asked(queue, make_worker, session_factory):
    """The Ctrl+C handler only sets this event; the loop finishes its batch and exits."""
    import asyncio
    stop = asyncio.Event()
    worker = make_worker()
    task = asyncio.create_task(worker.run_forever(stop))
    await queue.enqueue(await make_job())
    for _ in range(100):                               # wait (max ~5 s) for the job to be stored
        if await rows(session_factory, Verification):
            break
        await asyncio.sleep(0.05)
    stop.set()
    stats = await asyncio.wait_for(task, timeout=5)    # exits within one 1 s read block
    assert stats.outcomes == {"pass": 1}
    assert (await queue.stats())["consumers"] == []    # graceful stop leaves the group tidy


async def test_reconciliation_keeps_verification_spend(queue, make_worker, session_factory,
                                                        store, redis_client):
    """Regression: startup reconciliation rebuilt counters from request_logs only, so every
    API restart reset the verifier budget to $0 and its cap never held."""
    from app.budgets import BudgetService
    budgets = BudgetService(store, session_factory)
    await queue.enqueue(await make_job())
    await make_worker().run_once()
    [v] = await rows(session_factory, Verification)
    assert v.meta["budget"] == {"team_id": "quality-verifier", "feature": "verification"}
    # A row charged to another verifier budget (e.g. a simulation run), and one with no
    # recorded budget (legacy/demo): neither may be attributed to quality-verifier
    async with session_factory() as s:
        for meta in ({"budget": {"team_id": "sim-x-verifier", "feature": "verification"}}, {}):
            s.add(Verification(request_id=uuid.uuid4(), team_id="t", feature="f", model="m",
                               tier=1, routing_source="routed", verdict="pass",
                               verification_cost_usd=Decimal("5"), meta=meta))
        await s.commit()

    await budgets.reconcile(datetime.now(UTC))          # what every API startup does
    status = await budgets.status("team", "quality-verifier", datetime.now(UTC))
    assert status["day"]["spent_usd"] == v.verification_cost_usd > 0
    sim = await budgets.status("team", "sim-x-verifier", datetime.now(UTC))
    assert sim["day"]["spent_usd"] == Decimal("5")
    feature = await budgets.status("feature", "verification", datetime.now(UTC))
    assert feature["month"]["spent_usd"] == v.verification_cost_usd + Decimal("5")
