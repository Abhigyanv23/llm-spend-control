"""End-to-end routing through the real app (dev profile, config/routing.yaml)."""
from decimal import Decimal


def body(text: str, **kw) -> dict:
    return {"team_id": kw.pop("team_id", "route-team"), "feature": kw.pop("feature", "general"),
            "messages": [{"role": "user", "content": text}], **kw}


async def test_simple_prompt_uses_cheap_model_and_reports_savings(api):
    r = await api.post("/v1/chat", json=body("Hello!"))
    assert r.status_code == 200
    data = r.json()
    routing = data["metadata"]["routing"]
    assert data["model"] == "mock-echo" and routing["tier"] == 1
    assert routing["baseline_model"] == "mock-large"
    assert Decimal(routing["savings_usd"]) > 0


async def test_complex_prompt_routes_to_tier_3(api):
    r = await api.post("/v1/chat", json=body("Please analyze the trade-offs of this design."))
    routing = r.json()["metadata"]["routing"]
    assert r.json()["model"] == "mock-large" and routing["source"] == "routed"
    assert Decimal(routing["savings_usd"]) == 0          # it IS the baseline model


async def test_budget_downgrade_instead_of_block(api, set_policy):
    # Tiny budget: the tier-3 and tier-2 worst-case estimates exceed it, tier 1 fits
    await set_policy("team", "dg-team", daily="0.0002")
    r = await api.post("/v1/chat", json=body("Please analyze this.", team_id="dg-team",
                                             max_tokens=100))
    assert r.status_code == 200, r.text
    routing = r.json()["metadata"]["routing"]
    assert r.json()["model"] == "mock-echo"
    assert [d["model"] for d in routing["downgrades"]] == ["mock-large", "mock-medium"]


async def test_feature_without_downgrade_is_blocked(api, set_policy):
    await set_policy("team", "nd-team", daily="0.0002")
    r = await api.post("/v1/chat", json=body("Check this clause.", team_id="nd-team",
                                             feature="contract-review", max_tokens=100))
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "budget_exceeded"


async def test_route_preview_is_a_dry_run(api):
    r = await api.post("/v1/route/preview", json=body("Summarize this meeting."))
    assert r.status_code == 200
    data = r.json()
    assert data["routing"]["tier"] == 2 and data["routing"]["model"] == "mock-medium"
    assert "worst_case_cost_usd" in data and data["baseline_model"] == "mock-large"


async def test_routing_config_endpoint(api):
    data = (await api.get("/v1/routing")).json()
    assert data["profile"] == "dev"
    assert data["tiers"]["1"]["models"] == ["mock-echo"]
    assert "mock" in data["available_providers"]