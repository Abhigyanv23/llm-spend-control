"""The demo-data seeder: deterministic, idempotent, and removes only its own rows."""
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.db.models import BudgetPolicy, RequestLog
from scripts.seed_demo_data import DEMO_TEAMS, reset, seed

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


async def count(session_factory, model, **where) -> int:
    async with session_factory() as s:
        stmt = select(func.count()).select_from(model)
        for column, value in where.items():
            stmt = stmt.where(getattr(model, column) == value)
        return await s.scalar(stmt)


async def test_seed_is_deterministic_idempotent_and_resettable(session_factory, set_policy):
    await set_policy("team", "real-team", daily="5")              # must survive the reset
    first = await seed(session_factory, days=10, seed_value=1, now=NOW, preview_chars=120)
    rows = await count(session_factory, RequestLog)
    again = await seed(session_factory, days=10, seed_value=1, now=NOW, preview_chars=120)
    assert first == again                                          # same seed, same data
    assert await count(session_factory, RequestLog) == rows        # replaced, not duplicated
    assert set(first) == set(DEMO_TEAMS) and all(t["requests"] > 0 for t in first.values())
    assert await count(session_factory, BudgetPolicy, scope_id="demo-marketing") == 1

    await reset(session_factory)
    assert await count(session_factory, RequestLog) == 0
    assert await count(session_factory, BudgetPolicy, scope_id="real-team") == 1
