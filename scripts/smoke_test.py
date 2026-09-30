"""Phase 1 + Phase 2 smoke test against a RUNNING server with real Postgres + Redis:
    python scripts/smoke_test.py

Repeatable: every run uses fresh team/feature ids (smoke-<run id>-...) and creates its own
tiny budgets, so earlier runs and your seeded policies never affect the result.
"""
import os
import sys
import time
import uuid
from decimal import ROUND_DOWN, Decimal

import httpx

BASE = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000")
RUN = uuid.uuid4().hex[:8]
FEATURE = f"smoke-{RUN}"
Q8 = Decimal("0.00000001")


def team(name: str) -> str:
    return f"smoke-{RUN}-{name}"


def chat(headers: dict | None = None, **overrides) -> httpx.Response:
    body = {
        "team_id": "search",
        "feature": "summarize",
        "messages": [{"role": "user", "content": "Hello gateway"}],
    }
    body.update(overrides)
    return httpx.post(f"{BASE}/v1/chat", json=body, headers=headers or {}, timeout=120)


def chat2(team_id: str, priority: str = "normal", headers: dict | None = None,
          feature: str = FEATURE) -> httpx.Response:
    """Phase 2 request: max_tokens=1000 so the worst-case estimate is ~$0.0004."""
    return chat(headers=headers, team_id=team_id, feature=feature, priority=priority,
                max_tokens=1000)


def put_budget(scope: str, scope_id: str, daily: Decimal | None = None,
               monthly: Decimal | None = None) -> None:
    body = {"daily_limit_usd": None if daily is None else str(daily.quantize(Q8, ROUND_DOWN)),
            "monthly_limit_usd": None if monthly is None else str(monthly.quantize(Q8, ROUND_DOWN))}
    httpx.put(f"{BASE}/v1/budgets/{scope}/{scope_id}", json=body, timeout=10).raise_for_status()


def wait_for_log(team_id: str, request_id: str, timeout_s: float = 5.0) -> dict | None:
    """The audit write runs AFTER the response is sent (background task): poll briefly."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = httpx.get(f"{BASE}/v1/usage", params={"team_id": team_id, "limit": 100},
                         timeout=10)
        if resp.status_code != 200:
            print(f"        /v1/usage returned {resp.status_code}: {resp.text[:200]}")
            return None
        for row in resp.json()["items"]:
            if row["request_id"] == request_id:
                return row
        time.sleep(0.2)
    return None


def error_code(resp: httpx.Response) -> str | None:
    err = resp.json().get("error")
    return err.get("code") if isinstance(err, dict) else None


# ------------------------------------------------------------------ Phase 1

# (description, request overrides, expected status, expected error code)
PHASE1_CASES = [
    ("mock model returns a response", {}, 200, None),
    ("unknown model -> 400", {"model": "fake-model"}, 400, "unknown_model"),
    # Expects 503 only while OPENAI_API_KEY is unset in .env
    ("provider without API key -> 503", {"model": "gpt-4o-mini"}, 503, "provider_error"),
    ("empty messages -> 422 validation", {"messages": []}, 422, None),
    ("oversized prompt -> 400",
     {"messages": [{"role": "user", "content": "x" * 200_000}]}, 400, "context_too_long"),
]


def run_phase1() -> list[tuple[str, bool, str]]:
    results = []
    for desc, overrides, want_status, want_code in PHASE1_CASES:
        resp = chat(**overrides)
        got_code = error_code(resp)
        ok = resp.status_code == want_status and (want_code is None or got_code == want_code)
        results.append((desc, ok, f"status={resp.status_code} code={got_code}"))
    return results


# ------------------------------------------------------------------ Phase 2

def run_phase2() -> list[tuple[str, bool, str]]:
    results = []

    def check(desc: str, ok: bool, detail: str) -> None:
        results.append((desc, bool(ok), detail))

    # 1. Logged + learn this request's worst-case estimate E (used to size tiny budgets)
    probe_team = team("probe")
    resp = chat2(probe_team)
    data = resp.json()
    estimate = Decimal(data["metadata"]["budget"]["estimated_cost_usd"])
    row = wait_for_log(probe_team, data["request_id"])
    check("request is logged to the audit trail",
          resp.status_code == 200 and row is not None and row["status"] == "success"
          and Decimal(row["cost_usd"]) == Decimal(data["cost_usd"]),
          f"status={row and row['status']} cost_usd={row and row['cost_usd']} estimate={estimate}")

    # 2. Warning: limit = E / 0.9 -> this request lands at ~90% of the limit
    warn_team = team("warn")
    put_budget("team", warn_team, daily=estimate / Decimal("0.9"))
    resp = chat2(warn_team)
    warning = resp.headers.get("X-Budget-Warning")
    warn_cost = Decimal(resp.json().get("cost_usd", "0"))
    kinds = [w.get("kind") for w in resp.json().get("metadata", {}).get("budget_warnings", [])]
    check("warning appears near 80% (header + metadata)",
          resp.status_code == 200 and f"team:{warn_team}:day=" in (warning or "")
          and kinds == ["warning"],
          f"status={resp.status_code} X-Budget-Warning={warning!r}")

    # 3-5. Block / override on a budget smaller than one request's estimate
    block_team = team("block")
    put_budget("team", block_team, daily=estimate / 2)
    resp = chat2(block_team, priority="low")
    blocked_id = resp.json().get("error", {}).get("request_id")
    check("low priority blocked at 100% -> 402 budget_exceeded",
          resp.status_code == 402 and error_code(resp) == "budget_exceeded",
          f"status={resp.status_code} code={error_code(resp)}")
    if resp.status_code == 402:
        print(f"        message: {resp.json()['error']['message']}")

    resp = chat2(block_team, priority="high")
    check("high priority without override -> 402 override_required",
          resp.status_code == 402 and error_code(resp) == "override_required",
          f"status={resp.status_code} code={error_code(resp)}")

    reason = f"smoke test {RUN}"
    resp = chat2(block_team, priority="high", headers={"X-Budget-Override": reason})
    row = wait_for_log(block_team, resp.json().get("request_id", "")) if resp.status_code == 200 else None
    check("high priority WITH X-Budget-Override -> 200, reason audited",
          resp.status_code == 200 and row is not None and row["override_reason"] == reason,
          f"status={resp.status_code} override_reason={row and row['override_reason']!r}")

    row = wait_for_log(block_team, blocked_id) if blocked_id else None
    check("blocked request is logged with cost 0",
          row is not None and row["status"] == "budget_blocked" and Decimal(row["cost_usd"]) == 0,
          f"status={row and row['status']} cost_usd={row and row['cost_usd']}")

    # 6. Team has no policy, feature does: the stricter (feature) limit wins
    strict_feature = f"smoke-{RUN}-strict"
    put_budget("feature", strict_feature, daily=estimate / 2)
    resp = chat2(team("probe"), feature=strict_feature)
    scope = resp.json().get("error", {}).get("scope")
    check("strictest of team/feature policy wins",
          resp.status_code == 402 and scope == "feature",
          f"status={resp.status_code} scope={scope}")

    # 7. Status endpoint reflects the warn team's settled spend
    status = httpx.get(f"{BASE}/v1/budgets/team/{warn_team}/status", timeout=10).json()
    day = status.get("day", {})
    check("status endpoint reflects spend",
          Decimal(day.get("spent_usd", "-1")) == warn_cost
          and Decimal(day.get("reserved_usd", "-1")) == 0 and day.get("percent_used") is not None,
          f"spent={day.get('spent_usd')} reserved={day.get('reserved_usd')} "
          f"percent_used={day.get('percent_used')} resets_at={day.get('resets_at')}")
    return results


def main() -> int:
    try:
        health = httpx.get(f"{BASE}/health", timeout=5)
        health.raise_for_status()
    except httpx.HTTPError:
        print(f"Server not reachable at {BASE}. Start it with: python -m uvicorn app.main:app --reload")
        return 1
    print(f"Health: {health.json()}")
    if health.json().get("status") != "ok":
        print("WARNING: a dependency is down; Phase 2 checks will likely fail. "
              "Is `docker compose up -d` running?")

    sections = [("Phase 1: gateway", run_phase1()),
                (f"Phase 2: budgets + audit log (run id {RUN})", run_phase2())]
    total = failures = 0
    for title, results in sections:
        print(f"\n{title}")
        for desc, ok, detail in results:
            total += 1
            failures += not ok
            print(f"[{'PASS' if ok else 'FAIL'}] {desc}: {detail}")

    print("\nSample mock response:")
    print(chat().json())
    print(f"\n{total - failures}/{total} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
