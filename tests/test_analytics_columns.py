"""Phase 5A: prompt fingerprints, promoted analytics columns, migration backfill logic."""
import dataclasses
import importlib.util
from pathlib import Path

from sqlalchemy import select

from app.db.models import RequestLog
from app.fingerprint import fingerprint_text, prompt_fingerprint, prompt_preview
from app.schemas import ChatRequest, Message


def req(text: str, system: str | None = None) -> ChatRequest:
    messages = ([Message(role="system", content=system)] if system else []) + [
        Message(role="user", content=text)]
    return ChatRequest(team_id="t1", feature="f1", messages=messages)


# ------------------------------------------------------------------ fingerprints

def test_same_pattern_with_different_numbers_shares_a_fingerprint():
    assert (prompt_fingerprint(req("Summarise ticket #4521 for Alice"))
            == prompt_fingerprint(req("summarise   TICKET #87 for alice")))


def test_different_prompts_and_system_prompts_differ():
    assert prompt_fingerprint(req("Summarise this")) != prompt_fingerprint(req("Translate this"))
    assert (prompt_fingerprint(req("Summarise this", system="Be brief"))
            != prompt_fingerprint(req("Summarise this")))


def test_only_the_latest_user_message_counts():
    history = ChatRequest(team_id="t", feature="f", messages=[
        Message(role="user", content="first question 1"),
        Message(role="assistant", content="an answer"),
        Message(role="user", content="Summarise ticket 9")])
    assert prompt_fingerprint(history) == prompt_fingerprint(req("Summarise ticket 1"))


def test_fingerprint_is_a_sha256_hex_and_preview_is_capped():
    fp = fingerprint_text("hello")
    assert len(fp) == 64 and int(fp, 16) >= 0
    assert prompt_preview(req("word " * 100), 20) == ("word " * 4).strip()


# ------------------------------------------------------------------ gateway writes the columns

async def rows_for(app, team: str) -> list[RequestLog]:
    async with app.state.session_factory() as s:
        return list((await s.scalars(select(RequestLog).where(RequestLog.team_id == team))).all())


def body(text: str, team: str, **kw) -> dict:
    return {"team_id": team, "feature": "f1", "max_tokens": 1000,
            "messages": [{"role": "user", "content": text}], **kw}


async def test_routed_request_fills_every_analytics_column(sampled_api):
    client, app = sampled_api
    resp = await client.post("/v1/chat", json=body("Hello gateway 42", "t-cols"))
    [row] = await rows_for(app, "t-cols")
    assert (row.routed_tier, row.route_source, row.classifier_confidence) == (1, "routed", 0.5)
    assert row.baseline_cost_usd > row.cost_usd > 0
    assert (row.escalated, row.pre_escalated, row.downgraded) == (False, False, False)
    assert row.prompt_fingerprint == prompt_fingerprint(req("Hello gateway 7"))
    assert row.prompt_preview == "Hello gateway 42"
    assert str(row.id) == resp.json()["request_id"]


async def test_escalation_pre_call_and_downgrade_flags(sampled_api):
    client, app = sampled_api
    await client.post("/v1/chat", json=body("Hello [[mock:empty]]", "t-esc"))
    await client.post("/v1/chat", json=body("Hello gateway", "t-pre", priority="high"))
    await client.put("/v1/budgets/team/t-down", json={"daily_limit_usd": "0.0002"})
    await client.post("/v1/chat", json=body("Please analyze this.", "t-down", max_tokens=100))
    [esc] = await rows_for(app, "t-esc")
    [pre] = await rows_for(app, "t-pre")
    [down] = await rows_for(app, "t-down")
    assert esc.escalated and esc.routed_tier == 2 and esc.model == "mock-medium"
    assert pre.pre_escalated and pre.routed_tier == 2 and not pre.escalated
    assert down.downgraded and down.routed_tier == 1


async def test_failed_and_invalid_requests(sampled_api):
    client, app = sampled_api
    await client.post("/v1/chat", json=body("Hello", "t-fail", model="nope"))
    await client.post("/v1/chat", json={"team_id": "t-fail", "feature": "f1", "messages": []})
    rows = await rows_for(app, "t-fail")
    unknown = next(r for r in rows if r.error_code == "unknown_model")
    invalid = next(r for r in rows if r.error_code == "request_validation")
    assert unknown.prompt_fingerprint and unknown.routed_tier is None
    assert invalid.prompt_fingerprint is None and invalid.escalated is False


async def test_preview_respects_privacy(sampled_api):
    _, app = sampled_api
    gateway = app.state.gateway
    quality = gateway.quality
    gateway.quality = dataclasses.replace(
        quality, privacy=dataclasses.replace(quality.privacy, store_prompts=False))
    try:
        assert gateway._preview(req("secret customer data")) is None
    finally:
        gateway.quality = quality


# ------------------------------------------------------------------ migration backfill logic

def load_migration():
    path = Path("migrations/versions/0003_analytics_columns.py")
    spec = importlib.util.spec_from_file_location("migration_0003", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_extraction_matches_gateway_metadata():
    m = load_migration()
    meta = {"routing": {"source": "routed", "final_tier": 2, "baseline_cost_usd": "0.00010000",
                        "classifier": {"confidence": 0.65}, "downgrades": [{"model": "x"}]},
            "escalation": {"escalated": True, "pre_call": {"applied": True}}}
    assert m._row_values(meta) == {
        "route_source": "routed", "routed_tier": 2, "classifier_confidence": 0.65,
        "baseline_cost_usd": "0.00010000", "escalated": True, "pre_escalated": True,
        "downgraded": True}


def test_backfill_tolerates_missing_or_null_metadata():
    m = load_migration()
    empty = {"route_source": None, "routed_tier": None, "classifier_confidence": None,
             "baseline_cost_usd": None, "escalated": False, "pre_escalated": False,
             "downgraded": False}
    assert m._row_values({}) == empty
    assert m._row_values({"routing": None, "escalation": {"pre_call": None}}) == empty
