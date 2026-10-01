"""Phase 5 smoke test: analytics API + dashboard against the real server (Postgres + Redis).

    python scripts/seed_demo_data.py         # first: demo data so every chart has content
    python scripts/smoke_dashboard.py        # API checks + headless dashboard run
    python scripts/smoke_dashboard.py --no-ui

Set GATEWAY_URL to target another server.
"""
import os
import sys
from decimal import Decimal
from pathlib import Path

import httpx

BASE = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000")
ROOT = Path(__file__).resolve().parents[1]
client = httpx.Client(base_url=BASE, timeout=60)
results: list[bool] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f": {detail}" if detail and len(detail) <= 120 else ""))
    if not ok and detail:
        print(f"        {detail[:500]}")


def get(name: str, **params) -> dict:
    resp = client.get(f"/v1/analytics/{name}", params=params)
    check(f"GET /v1/analytics/{name} -> 200", resp.status_code == 200, resp.text[:300]
          if resp.status_code != 200 else "")
    return resp.json() if resp.status_code == 200 else {}


def main() -> int:
    try:
        client.get("/health").raise_for_status()
    except httpx.HTTPError:
        print(f"Server not reachable at {BASE}. Start it with: python -m uvicorn app.main:app --reload")
        return 1

    s = get("summary")
    check("summary has headline KPIs", {"spend_today_usd", "spend_month_to_date_usd",
                                         "projected_month_end_usd", "net_savings_pct",
                                         "verifier_pass_rate", "escalation_rate",
                                         "active_budget_alerts"} <= set(s), str(sorted(s))[:200])
    check("money is a fixed-point string", isinstance(s.get("cost_usd"), str)
          and "E" not in s["cost_usd"], str(s.get("cost_usd")))

    spend = get("spend", group_by="team")
    keys = {x["key"] for x in spend.get("series", [])}
    check("spend series include the demo teams", {"demo-search", "demo-support"} <= keys,
          str(sorted(keys))[:200])
    check("spend is zero-filled (same number of points as days)",
          all(len(x["points"]) == len(spend["days"]) for x in spend.get("series", [])))

    proj = get("projections")
    marketing = next((i for i in proj.get("items", []) if i["scope_id"] == "demo-marketing"), {})
    check("demo-marketing is projected over its monthly limit",
          marketing.get("status") in ("at_risk", "exhausted", "warning")
          and (marketing.get("projected_pct_of_limit") or 0) > 100,
          f"status={marketing.get('status')} pct={marketing.get('projected_pct_of_limit')}")
    check("burn-down covers the whole month",
          len(marketing.get("burndown", [])) == proj.get("days_in_month"))

    top = get("top", kind="patterns", limit=5)
    check("top prompt patterns are grouped by fingerprint",
          len(top.get("items", [])) == 5 and all(i["requests"] > 1 for i in top["items"]))

    sav = get("savings")
    check("savings: gross > 0 and verification overhead > 0",
          Decimal(sav.get("gross_savings_usd", "0")) > 0
          and Decimal(sav.get("verification_overhead_usd", "0")) > 0,
          f"gross={sav.get('gross_savings_pct')}% net={sav.get('net_savings_pct')}%")

    q = get("quality")
    v = q.get("verification", {})
    check("quality: pass rate with a Wilson interval",
          v.get("judged", 0) > 0 and v.get("pass_rate_ci95") is not None
          and v["pass_rate_ci95"][0] <= v["pass_rate"] <= v["pass_rate_ci95"][1],
          f"{v.get('pass_rate')} {v.get('pass_rate_ci95')}")

    lat = get("latency")
    check("latency percentiles are ordered (p50 <= p95 <= p99)",
          bool(lat.get("by_model")) and all(m["p50_ms"] <= m["p95_ms"] <= m["p99_ms"]
                                            for m in lat["by_model"]))

    err = get("errors")
    check("errors by provider and by status", bool(err.get("by_provider"))
          and "success" in err.get("by_status", {}))

    bad = client.get("/v1/analytics/spend", params={"from": "2026-10-02", "to": "2026-10-01"})
    check("invalid window -> 400 invalid_window",
          bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_window")

    if "--no-ui" not in sys.argv:
        os.environ["GATEWAY_URL"] = BASE
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=120)
        app.run()
        check("dashboard renders headlessly without exceptions", not app.exception,
              str([e.message for e in app.exception])[:400])
        check("dashboard shows the KPI tiles", len(app.metric) >= 8, f"{len(app.metric)} metrics")

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
