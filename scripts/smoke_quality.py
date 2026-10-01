"""Phase 4 smoke test: verification queue, worker, escalation and quality API, against the
real server + Postgres + Redis.

Run with the server up (ROUTING_PROFILE=dev, migrations at head):
    python scripts/smoke_quality.py
It sends a mix of short/long prompts and [[mock:...]] directives, runs
`python -m app.worker --once`, then checks verification rows, routing misses, escalations and
the quality endpoints. Fresh smoke4-<run id> teams, so it is repeatable.
Set GATEWAY_URL to target another server.
"""
import os
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

import httpx

BASE = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000")
ROOT = Path(__file__).resolve().parents[1]
RUN = uuid.uuid4().hex[:8]
TEAM = f"smoke4-{RUN}"
client = httpx.Client(base_url=BASE, timeout=30)
results: list[bool] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    short = ok and detail and len(detail) <= 120        # passes stay one line
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f": {detail}" if short else ""))
    if not ok and detail:
        print(f"        {detail[:500]}")


def chat(text: str, team: str = TEAM, **kw) -> httpx.Response:
    body = {"team_id": team, "feature": "smoke4", "max_tokens": 1000,
            "messages": [{"role": "user", "content": text}], **kw}
    return client.post("/v1/chat", json=body)


def long_prompt(i: int) -> str:
    return " ".join(f"point{n}" for n in range(300)) + f" (variant {i})"


def collect_sampled(make_text, wanted: int, max_tries: int = 40) -> list[str]:
    """Sampling is a deterministic hash of the (random) request id, so keep sending until
    `wanted` requests were sampled. At the 50% low-confidence rate, 40 tries never fall short
    in practice."""
    sampled: list[str] = []
    for i in range(max_tries):
        resp = chat(make_text(i))
        if resp.status_code == 200 and resp.json()["metadata"]["quality"]["sampling"]["sampled"]:
            sampled.append(resp.json()["request_id"])
            if len(sampled) == wanted:
                break
    return sampled


def escalation(resp: httpx.Response) -> dict:
    try:
        return resp.json()["metadata"]["escalation"]
    except (ValueError, KeyError, TypeError):
        return {}


def main() -> int:
    try:
        health = client.get("/health").json()
    except httpx.HTTPError:
        print(f"Server not reachable at {BASE}. Start it with: python -m uvicorn app.main:app --reload")
        return 1
    print(f"Health: {health}\nRun id: {RUN} (team {TEAM})\n")
    check("health ok on the dev routing profile",
          health.get("status") == "ok" and health.get("routing_profile") == "dev", str(health))

    # ---------------------------------------------------------------- synchronous escalation
    print("\n-- escalation")
    for directive, failed in [("empty", "empty"), ("refuse", "refusal"),
                              ("truncate", "truncated")]:
        r = chat(f"Hello [[mock:{directive}]]")
        esc = escalation(r)
        attempts = esc.get("attempts", [])
        total = sum(Decimal(a["cost_usd"]) for a in attempts) if attempts else Decimal(-1)
        check(f"[[mock:{directive}]] -> escalated to mock-medium, cost = sum of attempts",
              r.status_code == 200 and r.json().get("model") == "mock-medium"
              and esc.get("escalated") is True and attempts[0].get("check_failed") == failed
              and Decimal(r.json()["cost_usd"]) == total
              and r.json()["metadata"]["quality"]["sampling"]["sampled"] is False,
              r.text)

    r = chat("Return JSON please [[mock:badjson]]")
    esc = escalation(r)
    check("[[mock:badjson]] -> escalated once, stops at max_escalations",
          esc.get("escalations") == 1 and esc.get("final_check_failed") == "invalid_json"
          and "max_escalations" in (esc.get("note") or ""), r.text)

    tight = f"{TEAM}-tight"
    client.put(f"/v1/budgets/team/{tight}", json={"daily_limit_usd": "0.001"})
    r = chat("Hello [[mock:empty]]", team=tight)
    esc = escalation(r)
    check("escalation blocked by budget -> original answer returned (no error)",
          r.status_code == 200 and r.json().get("model") == "mock-echo"
          and (esc.get("blocked") or {}).get("blocked_by") == "budget_exceeded", r.text)

    # Own team: the bumped request is still eligible for sampling (tier 2, routed), and its
    # verification must not be counted with the main team's
    pre_team = f"{TEAM}-pre"
    r = chat("Hello gateway", team=pre_team, priority="high")
    check("high priority + low confidence -> pre-call escalation to mock-medium",
          r.status_code == 200 and r.json().get("model") == "mock-medium"
          and (escalation(r).get("pre_call") or {}).get("applied") is True, r.text)
    pre_stats = client.get("/v1/quality", params={"team_id": pre_team}).json()["escalation"]
    check("quality summary counts the pre-call escalation",
          pre_stats.get("pre_call_escalations") == 1, str(pre_stats))

    # One audit row per escalated request, with the summed cost
    rows = []
    for _ in range(20):
        rows = client.get("/v1/usage", params={"team_id": TEAM, "limit": 100}).json()["items"]
        if len(rows) >= 6:
            break
        time.sleep(0.25)
    escalated_rows = [x for x in rows if (x["metadata"].get("escalation") or {}).get("escalated")]
    check("audit log: one row per escalated request, model = final model",
          len(escalated_rows) == 4 and all(x["model"] == "mock-medium" for x in escalated_rows),
          f"{len(escalated_rows)} escalated rows")

    # ---------------------------------------------------------------- sampling + queue
    print("\n-- sampling and verification")
    short_ids = collect_sampled(lambda i: f"Hello smoke {RUN} {i}", 2)
    long_ids = collect_sampled(long_prompt, 2)
    check("sampled 2 short + 2 long cheap answers for verification",
          len(short_ids) == 2 and len(long_ids) == 2, f"short={len(short_ids)} long={len(long_ids)}")

    proc = subprocess.run([sys.executable, "-m", "app.worker", "--once"], cwd=ROOT,
                          capture_output=True, text=True, timeout=300)
    summary = next((line for line in proc.stdout.splitlines() if line.startswith("Processed")),
                   proc.stdout[-300:] + proc.stderr[-300:])
    check("worker --once ran", proc.returncode == 0, summary)

    # A continuous worker may have taken some jobs first: poll the API, not the worker output
    quality = {}
    for _ in range(40):
        quality = client.get("/v1/quality", params={"team_id": TEAM}).json()
        if quality["verification"]["verified"] >= 4:
            break
        time.sleep(0.5)
    v = quality.get("verification", {})
    check("4 verifications stored for this run", v.get("verified") == 4, str(v))
    if v.get("skipped"):
        print("        note: some verifications were skipped by the quality-verifier budget")
    check("short prompts pass, long prompts fail (mock capability limits)",
          v.get("pass") == 2 and v.get("fail") == 2, str(v))
    check("miss rate reported with a margin of error",
          v.get("miss_rate") == 0.5 and v.get("miss_rate_margin_95") is not None, str(v))

    misses = client.get("/v1/quality/misses", params={"team_id": TEAM}).json()
    items = misses.get("items", [])
    check("routing misses recorded for the long prompts (prompt previewed, not dumped)",
          misses.get("total") == 2 and {i["request_id"] for i in items} == set(long_ids)
          and all(len(i["prompt_preview"] or "") <= 200 for i in items)
          and all(i["better_model"] == "mock-large" for i in items), str(misses)[:400])

    esc_stats = quality.get("escalation", {})
    check("quality summary counts the post-call escalations",
          esc_stats.get("post_call_escalations") == 4, str(esc_stats))

    costs = quality.get("costs", {})
    print(f"        costs: {costs}")
    check("costs report verification spend and net savings",
          Decimal(costs.get("verification_cost_usd", "0")) > 0
          and Decimal(costs["net_savings_usd"]) == Decimal(costs["gross_savings_usd"])
          - Decimal(costs["verification_cost_usd"]), str(costs))

    queue = client.get("/v1/quality/queue").json()
    check("queue drained: nothing pending, nothing dead-lettered by this run",
          queue.get("pending") == 0, str(queue))

    verifier = client.get("/v1/budgets/team/quality-verifier/status").json()
    check("verification spend is charged to the quality-verifier budget",
          Decimal(verifier["day"]["spent_usd"]) > 0, verifier["day"]["spent_usd"])

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
