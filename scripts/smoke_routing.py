"""Phase 3 smoke test: routing against the real server (Postgres + Redis).

Run with the server up (ROUTING_PROFILE=dev):
    python scripts/smoke_routing.py
Uses fresh smoke3-<run id> teams with their own budgets, so it is repeatable.
Set GATEWAY_URL to target another server.
"""
import os
import sys
import time
import uuid
from decimal import Decimal

import httpx

BASE = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000")
RUN = uuid.uuid4().hex[:8]
client = httpx.Client(base_url=BASE, timeout=30)
results: list[bool] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}")
    if not ok and detail:
        print(f"        {detail[:400]}")


def chat_body(text: str, team: str, feature: str = "general", **kw) -> dict:
    return {"team_id": team, "feature": feature,
            "messages": [{"role": "user", "content": text}], **kw}


def routing_of(resp: httpx.Response) -> dict:
    try:
        return resp.json().get("metadata", {}).get("routing") or {}
    except ValueError:
        return {}


def main() -> int:
    try:
        health = client.get("/health").json()
    except httpx.HTTPError:
        print(f"Server not reachable at {BASE}. Start it with: python -m uvicorn app.main:app --reload")
        return 1
    print(f"Health: {health}\n")
    check("health reports the dev routing profile", health.get("routing_profile") == "dev",
          "These checks expect ROUTING_PROFILE=dev (mock models per tier)")

    cfg = client.get("/v1/routing").json()
    check("routing config exposes tiers 1-3", set(cfg.get("tiers", {})) == {"1", "2", "3"}, str(cfg))

    team = f"smoke3-{RUN}"
    for text, tier in [("Hello!", 1),
                       ("Summarize this meeting transcript", 2),
                       ("Analyze the trade-offs of this design", 3),
                       ("Review this contract clause", 3)]:
        r = client.post("/v1/route/preview", json=chat_body(text, team))
        got = r.json().get("routing", {}).get("tier") if r.status_code == 200 else None
        check(f"preview '{text}' -> tier {tier}", got == tier, r.text)

    # Real calls
    r = client.post("/v1/chat", json=chat_body("Hello!", team))
    savings = Decimal(routing_of(r).get("savings_usd", "0"))
    check("simple chat -> mock-echo with savings > 0",
          r.status_code == 200 and r.json().get("model") == "mock-echo" and savings > 0, r.text)

    r = client.post("/v1/chat", json=chat_body("Please analyze this architecture.", team))
    check("complex chat -> mock-large",
          r.status_code == 200 and r.json().get("model") == "mock-large", r.text)

    r = client.post("/v1/chat", json=chat_body("Hello!", team, model="mock-medium"))
    check("explicit model is honoured",
          r.status_code == 200 and routing_of(r).get("source") == "explicit"
          and r.json().get("model") == "mock-medium", r.text)

    # Budget-aware downgrade
    tiny = f"smoke3-{RUN}-tiny"
    r = client.put(f"/v1/budgets/team/{tiny}",
                   json={"daily_limit_usd": "0.0002", "monthly_limit_usd": None, "enabled": True})
    check("tiny budget created", r.status_code in (200, 201), r.text)

    r = client.post("/v1/chat", json=chat_body("Please analyze this.", tiny, max_tokens=100))
    downgraded = [d["model"] for d in routing_of(r).get("downgrades", [])]
    check("budget pressure downgrades tier 3 -> tier 1 instead of blocking",
          r.status_code == 200 and r.json().get("model") == "mock-echo"
          and downgraded == ["mock-large", "mock-medium"], r.text)

    r = client.post("/v1/chat", json=chat_body("Check this clause.", tiny,
                                                feature="contract-review", max_tokens=100))
    code = r.json().get("error", {}).get("code") if r.status_code != 200 else None
    check("contract-review is never downgraded (402 budget_exceeded)",
          r.status_code == 402 and code == "budget_exceeded", r.text)

    # Audit rows carry routing metadata (written in the background, so poll briefly)
    rows: list[dict] = []
    for _ in range(20):
        page = client.get("/v1/usage", params={"team_id": team, "limit": 50}).json()
        rows = [i for i in page.get("items", []) if i.get("status") == "success"]
        if len(rows) >= 3:
            break
        time.sleep(0.25)
    check("audit log rows include routing metadata",
          len(rows) >= 3 and all("routing" in (i.get("metadata") or {}) for i in rows),
          f"found {len(rows)} success rows")

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
