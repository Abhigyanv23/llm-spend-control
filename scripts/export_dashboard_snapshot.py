"""Export every analytics response the dashboard needs into dashboard/snapshot/snapshot.json.

The hosted dashboard (Streamlit Community Cloud) cannot reach the API, Postgres or Redis, so it
reads this file instead. Run with the API up and demo data seeded:

    python scripts/seed_demo_data.py
    python scripts/export_dashboard_snapshot.py

Set GATEWAY_URL to export from another server.
"""
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
from data_source import (  # noqa: E402
    SNAPSHOT_PATH,
    api_is_up,
    dashboard_requests,
    fetch_live,
    snapshot_key,
    window_end,
    window_params,
)

BASE = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")


def main() -> int:
    if not api_is_up(BASE):
        print(f"API not reachable at {BASE}. Start it with: python -m uvicorn app.main:app")
        return 1

    end = window_end()      # one fixed window end, so every response describes the same moment
    responses: dict[str, dict] = {}
    for path, days, extra in dashboard_requests():
        params = window_params(days, end=end) if days is not None else {}
        params.update(extra)
        key = snapshot_key(path, days, **extra)
        try:
            responses[key] = fetch_live(BASE, path, params)
        except httpx.HTTPError as exc:
            print(f"FAILED {key}: {exc}")
            return 1
        print(f"ok  {key}")

    requests_30d = responses["summary|days=30"].get("requests", 0)
    if not requests_30d:
        print("\nThe 30-day window has no requests: the hosted dashboard would be empty.\n"
              "Seed demo data first: python scripts/seed_demo_data.py")
        return 1

    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": {"exported_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
                        "window_end": end.isoformat(), "responses": len(responses)},
               "responses": responses}
    SNAPSHOT_PATH.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    size_kb = SNAPSHOT_PATH.stat().st_size / 1024
    print(f"\nWrote {len(responses)} responses ({size_kb:.0f} KB) to {SNAPSHOT_PATH}"
          f"\n30-day window: {requests_30d:,} requests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
