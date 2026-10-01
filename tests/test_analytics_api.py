"""Analytics API: shapes, money as strings, validation, defaults and caching."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

ENDPOINTS = ["summary", "spend", "projections", "top", "savings", "quality", "latency", "errors"]


async def traffic(client, team="t-ana"):
    for text in ["Hello gateway 1", "Hello gateway 2", "Summarize this report",
                 "Hello [[mock:empty]]"]:
        resp = await client.post("/v1/chat", json={
            "team_id": team, "feature": "f-ana", "max_tokens": 1000,
            "messages": [{"role": "user", "content": text}]})
        assert resp.status_code == 200


@pytest.mark.parametrize("name", ENDPOINTS)
async def test_every_endpoint_answers_with_metadata(api, name):
    await traffic(api)
    resp = await api.get(f"/v1/analytics/{name}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["generated_at"].endswith("Z") and body["cached"] is False
    if name != "projections":                                  # always month-to-date
        assert body["window"]["from"].endswith("Z")


async def test_summary_and_spend_money_are_fixed_point_strings(api):
    await traffic(api)
    summary = (await api.get("/v1/analytics/summary")).json()
    assert summary["requests"] == 4
    assert isinstance(summary["cost_usd"], str) and Decimal(summary["cost_usd"]) > 0
    assert "E" not in summary["spend_today_usd"]
    spend = (await api.get("/v1/analytics/spend", params={"group_by": "model"})).json()
    keys = {s["key"] for s in spend["series"]}
    assert keys == {"mock-echo", "mock-medium"}                # escalation served by mock-medium
    assert len(spend["days"]) == len(spend["series"][0]["points"]) >= 30   # zero-filled
    assert spend["by_model"][0]["avg_cost_usd"]


async def test_savings_quality_and_top_patterns(api):
    await traffic(api)
    savings = (await api.get("/v1/analytics/savings")).json()
    assert savings["routed_requests"] == 4 and savings["gross_savings_pct"] > 0
    quality = (await api.get("/v1/analytics/quality")).json()
    assert quality["escalation"]["post_call"] == 1
    assert quality["tier_distribution"]["2"] >= 1
    top = (await api.get("/v1/analytics/top", params={"kind": "patterns", "limit": 3})).json()
    # "Hello gateway 1" and "Hello gateway 2" share one fingerprint (digits normalised)
    assert top["items"][0]["requests"] == 2 or any(i["requests"] == 2 for i in top["items"])
    assert top["items"][0]["prompt_preview"]


async def test_filters_and_projections(api):
    await traffic(api, team="t-one")
    await traffic(api, team="t-two")
    one = (await api.get("/v1/analytics/summary", params={"team_id": "t-one"})).json()
    assert one["requests"] == 4 and one["window"]["team_id"] == "t-one"
    await api.put("/v1/budgets/team/t-one", json={"monthly_limit_usd": "0.0001"})
    proj = (await api.get("/v1/analytics/projections", params={"team_id": "t-one"})).json()
    [item] = proj["items"]
    assert item["status"] == "exhausted" and len(item["burndown"]) == proj["days_in_month"]


@pytest.mark.parametrize("params, status, code", [
    ({"from": "2026-10-02T00:00:00", "to": "2026-10-01T00:00:00"}, 400, "invalid_window"),
    ({"from": "2024-01-01T00:00:00", "to": "2026-01-01T00:00:00"}, 400, "invalid_window"),
    ({"group_by": "planet"}, 422, None),
    ({"from": "yesterday"}, 422, None),
])
async def test_validation(api, params, status, code):
    resp = await api.get("/v1/analytics/spend", params=params)
    assert resp.status_code == status
    if code:
        assert resp.json()["error"]["code"] == code


async def test_top_limit_is_bounded(api):
    assert (await api.get("/v1/analytics/top", params={"limit": 101})).status_code == 422


async def test_responses_are_cached_briefly(api):
    await traffic(api)
    params = {"from": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
              "to": (datetime.now(UTC) + timedelta(days=1)).isoformat()}
    first = (await api.get("/v1/analytics/summary", params=params)).json()
    await traffic(api)                                          # new data...
    second = (await api.get("/v1/analytics/summary", params=params)).json()
    assert second["cached"] is True and second["requests"] == first["requests"] == 4
    # ...appears once the TTL passes or with different parameters
    other = (await api.get("/v1/analytics/summary", params={**params, "team_id": "t-ana"})).json()
    assert other["cached"] is False and other["requests"] == 8
