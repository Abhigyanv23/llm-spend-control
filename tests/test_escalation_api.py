"""Escalation end to end through /v1/chat, plus the quality endpoints and miss export."""
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.quality.judges import build_judge
from app.quality.worker import VerificationWorker

LONG_PROMPT = " ".join(f"point{i}" for i in range(300))


def body(content: str, team: str = "t-esc", **extra) -> dict:
    return {"team_id": team, "feature": "f1", "max_tokens": 1000,
            "messages": [{"role": "user", "content": content}], **extra}


async def chat(client, content: str, **kw) -> dict:
    resp = await client.post("/v1/chat", json=body(content, **kw))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def usage_rows(client, team: str) -> list[dict]:
    return (await client.get("/v1/usage", params={"team_id": team})).json()["items"]


# ------------------------------------------------------------------ post-call cascade

async def test_empty_cheap_answer_is_escalated_and_costs_are_summed(api):
    data = await chat(api, "Hello [[mock:empty]]")
    esc = data["metadata"]["escalation"]
    assert data["model"] == "mock-medium"
    assert data["output"] == "[mock:mock-medium] You said: Hello [[mock:empty]]"
    assert esc["escalated"] is True and esc["escalations"] == 1
    first, second = esc["attempts"]
    assert (first["model"], first["check_failed"]) == ("mock-echo", "empty")
    assert (second["model"], second["escalated_because"], second["check_failed"]) == (
        "mock-medium", "empty", None)
    total = Decimal(first["cost_usd"]) + Decimal(second["cost_usd"])
    assert Decimal(data["cost_usd"]) == total
    assert Decimal(esc["extra_cost_usd"]) == Decimal(second["cost_usd"])
    assert data["metadata"]["routing"]["final_model"] == "mock-medium"

    # ONE audit row for the request: final model, summed cost
    [row] = await usage_rows(api, "t-esc")
    assert (row["model"], Decimal(row["cost_usd"])) == ("mock-medium", total)
    # Budget counters saw both attempts (settled separately)
    status = (await api.get("/v1/budgets/team/t-esc/status")).json()
    assert Decimal(status["day"]["spent_usd"]) == total
    assert Decimal(status["day"]["reserved_usd"]) == 0


@pytest.mark.parametrize("content, failed", [
    ("Hello [[mock:refuse]]", "refusal"),
    ("Hello [[mock:truncate]]", "truncated"),
])
async def test_other_visible_failures_escalate(api, content, failed):
    esc = (await chat(api, content))["metadata"]["escalation"]
    assert esc["attempts"][0]["check_failed"] == failed
    assert esc["escalated"] is True and esc["final_check_failed"] is None


async def test_escalation_stops_at_max_escalations_even_if_still_failing(api):
    """mock-medium echoes the prompt, which still isn't JSON: we don't loop forever."""
    esc = (await chat(api, "Return JSON please [[mock:badjson]]"))["metadata"]["escalation"]
    assert esc["attempts"][0]["check_failed"] == "invalid_json"
    assert esc["escalations"] == 1 and esc["final_check_failed"] == "invalid_json"
    assert esc["note"] == "max_escalations (1) reached"


async def test_budget_blocked_escalation_returns_the_original_answer(api):
    # Room for the tier-1 attempt (~$0.0004 worst case) but not for tier 2 (~$0.005)
    await api.put("/v1/budgets/team/t-tight", json={"daily_limit_usd": "0.001"})
    data = await chat(api, "Hello [[mock:empty]]", team="t-tight")
    esc = data["metadata"]["escalation"]
    assert (data["model"], data["output"]) == ("mock-echo", "")
    assert esc["escalated"] is False
    assert esc["blocked"]["model"] == "mock-medium"
    assert esc["blocked"]["blocked_by"] == "budget_exceeded"
    assert "returning the original answer" in esc["note"]
    assert Decimal(data["cost_usd"]) == Decimal(esc["attempts"][0]["cost_usd"])


async def test_explicit_model_is_checked_but_not_escalated(api):
    data = await chat(api, "Hello [[mock:empty]]", model="mock-echo")
    esc = data["metadata"]["escalation"]
    assert data["output"] == "" and esc["escalated"] is False
    assert esc["final_check_failed"] == "empty" and "'explicit'" in esc["note"]


async def test_feature_capped_at_tier_1_is_not_escalated(api):
    data = await chat(api, "Hello [[mock:empty]]", feature="autocomplete")
    assert "highest allowed tier (1)" in data["metadata"]["escalation"]["note"]


async def test_healthy_answer_is_not_escalated(api):
    esc = (await chat(api, "Hello gateway"))["metadata"]["escalation"]
    assert esc["escalated"] is False and esc["final_check_failed"] is None
    assert len(esc["attempts"]) == 1 and esc["note"] is None


# ------------------------------------------------------------------ pre-call

async def test_pre_call_escalation_for_uncertain_high_priority(api):
    high = await chat(api, "Hello gateway", priority="high")
    assert high["model"] == "mock-medium"
    assert high["metadata"]["escalation"]["pre_call"]["applied"] is True
    normal = await chat(api, "Hello gateway", priority="normal")
    assert normal["model"] == "mock-echo"
    assert normal["metadata"]["escalation"]["pre_call"] is None


async def test_route_preview_shows_the_pre_call_bump(api):
    resp = await api.post("/v1/route/preview", json=body("Hello gateway", priority="critical"))
    data = resp.json()
    assert data["routing"]["model"] == "mock-medium"
    assert data["pre_call_escalation"]["applied"] is True


# ------------------------------------------------------------------ interaction with sampling

async def test_escalated_requests_are_not_sampled(sampled_api):
    client, app = sampled_api
    data = await chat(client, "Hello [[mock:empty]]")
    sampling = data["metadata"]["quality"]["sampling"]
    assert sampling["sampled"] is False and "escalated" in sampling["reason"]
    assert (await app.state.queue.stats())["length"] == 0


# ------------------------------------------------------------------ quality endpoints + export

def worker_for(app) -> VerificationWorker:
    core = app.state.core
    vcfg = core.quality_config.verification
    return VerificationWorker(
        queue=core.queue, quality=core.quality_config, registry=core.registry,
        adapters=core.adapters, budgets=core.budgets, session_factory=core.session_factory,
        judge=build_judge(vcfg.judge, core.registry, core.adapters,
                          team_id=vcfg.budget_team_id, feature=vcfg.budget_feature),
        consumer="t", min_idle_ms=0)


async def test_quality_endpoints_and_export(sampled_api, tmp_path):
    client, app = sampled_api
    await chat(client, "Hello gateway")                  # sampled -> pass
    long = await chat(client, LONG_PROMPT)                # sampled -> fail -> routing miss
    await chat(client, "Hello [[mock:empty]]")           # escalated, not sampled
    assert (await worker_for(app).run_once()).outcomes == {"pass": 1, "fail": 1}

    q = (await client.get("/v1/quality")).json()
    v = q["verification"]
    assert (v["verified"], v["pass"], v["fail"], v["miss_rate"]) == (2, 1, 1, 0.5)
    assert v["miss_rate_margin_95"] == round(1.96 * (0.25 / 2) ** 0.5, 4)
    assert v["by_model"] == [{"model": "mock-echo", "judged": 2, "fail": 1, "miss_rate": 0.5}]
    assert q["misses"]["by_model"] == [{"model": "mock-echo", "count": 1}]
    assert q["requests"] == {"successful": 3, "sampled": 2, "sample_rate": 0.6667}
    assert q["escalation"]["post_call_escalations"] == 1
    costs = q["costs"]
    assert Decimal(costs["verification_cost_usd"]) == Decimal(v["verification_cost_usd"]) > 0
    assert Decimal(costs["net_savings_usd"]) == (Decimal(costs["gross_savings_usd"])
                                                 - Decimal(costs["verification_cost_usd"]))

    misses = (await client.get("/v1/quality/misses")).json()
    [item] = misses["items"]
    assert item["request_id"] == long["request_id"] and misses["total"] == 1
    assert len(item["prompt_preview"]) == 200 and item["prompt_chars"] == 2000
    assert "prompt" not in item                           # listings never carry the full text

    queue = (await client.get("/v1/quality/queue")).json()
    assert (queue["length"], queue["pending"], queue["dead_letter"]) == (2, 0, 0)

    filtered = (await client.get("/v1/quality", params={"feature": "nope"})).json()
    assert filtered["verification"]["verified"] == 0 and filtered["verification"]["miss_rate"] is None

    from scripts.export_misses import export
    from app.quality.reports import QualityFilter
    out = tmp_path / "misses.jsonl"
    assert await export(app.state.session_factory, out, QualityFilter()) == 1
    [example] = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert (example["chosen_tier"], example["label_tier"]) == (1, 3)
    assert len(example["prompt"]) == 2000 and example["classifier_features"]


async def test_quality_window_excludes_old_data(sampled_api):
    client, app = sampled_api
    await chat(client, "Hello gateway")
    await worker_for(app).run_once()
    future = datetime(2099, 1, 1, tzinfo=UTC).isoformat()
    q = (await client.get("/v1/quality", params={"from": future})).json()
    assert q["verification"]["verified"] == 0 and q["requests"]["successful"] == 0
