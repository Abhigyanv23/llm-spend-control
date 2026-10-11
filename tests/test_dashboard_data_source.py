"""The dashboard's snapshot keys must match between the export script and the app."""
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
from data_source import dashboard_requests, snapshot_key, window_end, window_params  # noqa: E402


def test_snapshot_key_is_stable_and_skips_empty_values():
    assert snapshot_key("spend", 30, group_by="team") == "spend|days=30|group_by=team"
    assert snapshot_key("top", 7, limit=10, kind="patterns") == "top|days=7|kind=patterns|limit=10"
    assert snapshot_key("projections", None, burndown=True) == "projections|burndown=True"
    assert snapshot_key("summary", 30, team_id=None, feature="") == "summary|days=30"


def test_every_dashboard_request_has_a_unique_key():
    keys = [snapshot_key(path, days, **extra) for path, days, extra in dashboard_requests()]
    # 3 windows x (summary + 3 spend + top + 4 per-window) + projections
    assert len(keys) == 3 * 9 + 1
    assert len(set(keys)) == len(keys)


def test_window_is_hour_aligned_and_utc():
    end = window_end(datetime(2026, 10, 11, 9, 41, 7, tzinfo=UTC))
    assert end == datetime(2026, 10, 11, 10, 0, tzinfo=UTC)
    params = window_params(7, end=end)
    assert params["from"] == "2026-10-04T10:00:00+00:00"
    assert params["to"] == "2026-10-11T10:00:00+00:00"
    assert params["team_id"] is None and params["feature"] is None
