"""Gateway sampling + enqueue through the real app (SQLite + fakeredis), then the worker."""
from sqlalchemy import select

from app.db.models import RoutingMiss, Verification
from app.quality.judges import build_judge
from app.quality.worker import VerificationWorker

LONG_PROMPT = " ".join(f"point{i}" for i in range(300))


def body(content="Hello gateway", **extra) -> dict:
    return {"team_id": "t1", "feature": "f1", "max_tokens": 1000,
            "messages": [{"role": "user", "content": content}], **extra}


def worker_for(app) -> VerificationWorker:
    core = app.state.core
    vcfg = core.quality_config.verification
    return VerificationWorker(
        queue=core.queue, quality=core.quality_config, registry=core.registry,
        adapters=core.adapters, budgets=core.budgets, session_factory=core.session_factory,
        judge=build_judge(vcfg.judge, core.registry, core.adapters,
                          team_id=vcfg.budget_team_id, feature=vcfg.budget_feature),
        consumer="api-test", min_idle_ms=0)


async def test_routed_request_is_sampled_and_queued(sampled_api):
    client, app = sampled_api
    resp = await client.post("/v1/chat", json=body())
    sampling = resp.json()["metadata"]["quality"]["sampling"]
    assert sampling["sampled"] is True and sampling["rate"] == 1.0
    assert (await app.state.queue.stats())["length"] == 1


async def test_explicit_model_is_not_sampled(sampled_api):
    client, app = sampled_api
    resp = await client.post("/v1/chat", json=body(model="mock-echo"))
    assert resp.json()["metadata"]["quality"]["sampling"]["sampled"] is False
    assert (await app.state.queue.stats())["length"] == 0


async def test_enqueue_failure_never_fails_the_request(sampled_api, monkeypatch):
    client, app = sampled_api

    async def broken(*args, **kwargs):
        raise ConnectionError("redis went away")
    monkeypatch.setattr(app.state.queue, "enqueue", broken)
    resp = await client.post("/v1/chat", json=body())
    assert resp.status_code == 200
    usage = (await client.get("/v1/usage", params={"team_id": "t1"})).json()
    assert usage["totals"]["count"] == 1                 # the request was still audited


async def test_end_to_end_chat_then_worker_finds_the_routing_miss(sampled_api):
    client, app = sampled_api
    short = (await client.post("/v1/chat", json=body())).json()
    long = (await client.post("/v1/chat", json=body(LONG_PROMPT))).json()
    stats = await worker_for(app).run_once()
    assert stats.outcomes == {"pass": 1, "fail": 1}
    async with app.state.session_factory() as s:
        verdicts = {str(v.request_id): v.verdict for v in (await s.scalars(select(Verification)))}
        misses = [str(m.request_id) for m in (await s.scalars(select(RoutingMiss)))]
    assert verdicts == {short["request_id"]: "pass", long["request_id"]: "fail"}
    assert misses == [long["request_id"]]
