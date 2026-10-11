"""Where the dashboard gets its data: the live analytics API, or a committed JSON snapshot.

Live mode is for local use (API + Postgres + Redis running). Snapshot mode is for hosting the
dashboard somewhere the API is not reachable (Streamlit Community Cloud): every analytics
response the dashboard can ask for is exported once, from seeded demo data, into
dashboard/snapshot/snapshot.json by scripts/export_dashboard_snapshot.py.

This module has no Streamlit import, so the export script and the tests can use it too.
"""
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

SNAPSHOT_PATH = Path(__file__).parent / "snapshot" / "snapshot.json"
WINDOWS = (7, 30, 90)                       # the sidebar's window choices, in days
GROUP_BYS = ("team", "feature", "model")    # the Spend tab's group-by choices
PER_WINDOW = ("savings", "quality", "latency", "errors")


def window_end(now: datetime | None = None) -> datetime:
    """End of the query window: the next full hour (UTC), so cache keys stay stable."""
    now = now or datetime.now(UTC)
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def window_params(days: int, team: str | None = None, feature: str | None = None,
                  end: datetime | None = None) -> dict:
    end = end or window_end()
    return {"from": (end - timedelta(days=days)).isoformat(), "to": end.isoformat(),
            "team_id": team, "feature": feature}


def snapshot_key(path: str, days: int | None = None, **extra) -> str:
    """Stable key for one dashboard request, e.g. 'spend|days=30|group_by=team'."""
    parts = [path]
    if days is not None:
        parts.append(f"days={days}")
    parts += [f"{k}={v}" for k, v in sorted(extra.items()) if v not in (None, "")]
    return "|".join(parts)


def dashboard_requests() -> Iterator[tuple[str, int | None, dict]]:
    """Every (path, days, extra params) the dashboard can request without team/feature filters."""
    for days in WINDOWS:
        yield "summary", days, {}
        for group_by in GROUP_BYS:
            yield "spend", days, {"group_by": group_by}
        yield "top", days, {"kind": "patterns", "limit": 10}
        for path in PER_WINDOW:
            yield path, days, {}
    yield "projections", None, {"burndown": True}


def fetch_live(base_url: str, path: str, params: dict) -> dict:
    resp = httpx.get(f"{base_url}/v1/analytics/{path}",
                     params={k: v for k, v in params.items() if v not in (None, "")},
                     timeout=30)
    resp.raise_for_status()
    return resp.json()


def api_is_up(base_url: str, timeout_s: float = 2.0) -> bool:
    try:
        return httpx.get(f"{base_url}/health", timeout=timeout_s).status_code == 200
    except httpx.HTTPError:
        return False


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
