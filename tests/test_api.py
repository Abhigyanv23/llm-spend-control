"""End-to-end through HTTP: FastAPI app + gateway + budgets + audit log (SQLite + fakeredis)."""
from decimal import Decimal


def chat_body(team="t1", feature="f1", priority="normal", **extra) -> dict:
    return {"team_id": team, "feature": feature, "priority": priority, "max_tokens": 1000,
            "messages": [{"role": "user", "content": "Hello gateway"}], **extra}


async def put_budget(api, scope, scope_id, daily=None, monthly=None):
    resp = await api.put(f"/v1/budgets/{scope}/{scope_id}",
                         json={"daily_limit_usd": daily, "monthly_limit_usd": monthly})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def usage(api, **params) -> dict:
    resp = await api.get("/v1/usage", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_health_and_models(api):
    health = (await api.get("/health")).json()
    assert health == {"status": "ok", "postgres": "ok", "redis": "ok",
                      "budget_fail_mode": "open", "routing_profile": "dev"}
    models = (await api.get("/v1/models")).json()["models"]
    assert next(m for m in models if m["name"] == "mock-echo")["input_cost_per_mtok"] == "0.1"


async def test_success_is_logged_with_decimal_cost(api):
    resp = await api.post("/v1/chat", json=chat_body())
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["cost_usd"], str)                     # Decimal -> JSON string
    assert data["metadata"]["budget"] == {"status": "ok", "estimated_cost_usd": "0.00040070"}
    assert data["metadata"]["budget_warnings"] == []

    page = await usage(api, team_id="t1")
    assert page["totals"]["count"] == 1
    row = page["items"][0]
    assert row["request_id"] == data["request_id"] and row["status"] == "success"
    assert Decimal(row["cost_usd"]) == Decimal(data["cost_usd"])
    assert Decimal(row["estimated_cost_usd"]) == Decimal("0.00040070")


async def test_warning_header_near_limit(api):
    # estimate is $0.0004007 -> a $0.00045 limit puts the request at ~89%
    await put_budget(api, "team", "t-warn", daily="0.00045")
    resp = await api.post("/v1/chat", json=chat_body(team="t-warn"))
    assert resp.status_code == 200
    assert resp.headers["X-Budget-Warning"].startswith("team:t-warn:day=89.")
    assert resp.json()["metadata"]["budget_warnings"][0]["kind"] == "warning"


async def test_block_override_flow_and_audit(api):
    await put_budget(api, "team", "t-block", daily="0.0001")

    low = await api.post("/v1/chat", json=chat_body(team="t-block", priority="low"))
    assert low.status_code == 402
    err = low.json()["error"]
    assert err["code"] == "budget_exceeded" and err["scope_id"] == "t-block"
    assert err["resets_at"].endswith("T00:00:00Z") and "request_id" in err

    high = await api.post("/v1/chat", json=chat_body(team="t-block", priority="high"))
    assert (high.status_code, high.json()["error"]["code"]) == (402, "override_required")

    ok = await api.post("/v1/chat", json=chat_body(team="t-block", priority="high"),
                        headers={"X-Budget-Override": "  launch demo for CFO  "})
    assert ok.status_code == 200
    assert "(overridden)" in ok.headers["X-Budget-Warning"]

    rows = (await usage(api, team_id="t-block"))["items"]
    by_status = sorted((r["status"], r["error_code"], r["override_reason"]) for r in rows)
    assert by_status == [("budget_blocked", "budget_exceeded", None),
                         ("budget_blocked", "override_required", None),
                         ("success", None, "launch demo for CFO")]
    blocked = [r for r in rows if r["status"] == "budget_blocked"]
    assert all(Decimal(r["cost_usd"]) == 0 for r in blocked)


async def test_money_is_fixed_point_in_json(api):
    status = (await api.get("/v1/budgets/team/nobody/status")).json()
    assert status["policy"] is None and status["day"]["limit_usd"] is None
    assert status["day"]["spent_usd"] == "0.00000000"            # not "0E-8"


async def test_status_endpoint_reflects_spend(api):
    await put_budget(api, "feature", "f-status", daily="1.00", monthly="10.00")
    cost = Decimal((await api.post("/v1/chat", json=chat_body(feature="f-status"))).json()["cost_usd"])
    status = (await api.get("/v1/budgets/feature/f-status/status")).json()
    assert status["policy"]["daily_limit_usd"] == "1.00000000"
    assert Decimal(status["day"]["spent_usd"]) == cost
    assert Decimal(status["day"]["reserved_usd"]) == 0           # hold released at settle
    assert Decimal(status["month"]["remaining_usd"]) == Decimal("10") - cost
    assert status["day"]["resets_at"].endswith("T00:00:00Z")


async def test_provider_error_releases_hold_and_is_logged(api):
    resp = await api.post("/v1/chat", json=chat_body(team="t-prov", model="gpt-4o-mini"))
    assert (resp.status_code, resp.json()["error"]["code"]) == (503, "provider_error")
    status = (await api.get("/v1/budgets/team/t-prov/status")).json()
    assert Decimal(status["day"]["reserved_usd"]) == 0 and Decimal(status["day"]["spent_usd"]) == 0
    row = (await usage(api, team_id="t-prov"))["items"][0]
    assert (row["status"], row["provider"], Decimal(row["cost_usd"])) == ("provider_error", "openai", 0)


async def test_validation_errors_are_logged(api):
    assert (await api.post("/v1/chat", json=chat_body(team="t-val", model="nope"))).status_code == 400
    assert (await api.post("/v1/chat", json=chat_body(team="t-val", messages=[]))).status_code == 422
    rows = (await usage(api, team_id="t-val", status="validation_error"))["items"]
    assert sorted(r["error_code"] for r in rows) == ["request_validation", "unknown_model"]


async def test_policy_crud_and_validation(api):
    await put_budget(api, "team", "crud", daily="5")
    await put_budget(api, "team", "crud", daily="7.5", monthly="100")      # update, not duplicate
    policies = [p for p in (await api.get("/v1/budgets")).json()["policies"]
                if p["scope_id"] == "crud"]
    assert len(policies) == 1 and policies[0]["daily_limit_usd"] == "7.50000000"
    bad = await api.put("/v1/budgets/team/crud", json={"daily_limit_usd": -1})
    assert bad.status_code == 422
    assert (await api.put("/v1/budgets/org/x", json={})).status_code == 422


async def test_usage_pagination_and_totals(api):
    for _ in range(3):
        await api.post("/v1/chat", json=chat_body(team="t-page"))
    page = await usage(api, team_id="t-page", limit=2, offset=0)
    assert len(page["items"]) == 2 and page["has_more"] is True
    assert page["totals"]["count"] == 3                          # totals cover all pages
    last = await usage(api, team_id="t-page", limit=2, offset=2)
    assert len(last["items"]) == 1 and last["has_more"] is False
