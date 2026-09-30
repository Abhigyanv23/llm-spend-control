"""Seed example budget policies (idempotent: safe to run repeatedly).

    python scripts/seed_budgets.py

A script, not an Alembic data migration: migrations should hold schema and data every
environment needs; demo budgets are environment-specific and change freely.
"""
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # make `app` importable

from app.budgets.policies import list_policies, upsert_policy  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import create_engine, create_session_factory  # noqa: E402
from app.money import format_usd  # noqa: E402

# (scope, scope_id, daily_limit_usd, monthly_limit_usd); None = unlimited
POLICIES = [
    ("team", "search", Decimal("5.00"), Decimal("100.00")),
    ("team", "research", Decimal("1.00"), Decimal("20.00")),
    ("team", "demo-tiny", Decimal("0.0005"), None),         # hits its limit in a few calls
    ("feature", "summarize", Decimal("2.00"), None),
    ("feature", "chat-assistant", None, Decimal("50.00")),
]


def show(value: Decimal | None) -> str:
    return "unlimited" if value is None else format_usd(value)


async def main() -> None:
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        for scope, scope_id, daily, monthly in POLICIES:
            await upsert_policy(session, scope, scope_id, daily, monthly)
        await session.commit()
        policies = await list_policies(session)
    await engine.dispose()

    print(f"{'scope':<9}{'scope_id':<18}{'daily':>12}{'monthly':>12}  enabled")
    for p in policies:
        print(f"{p.scope:<9}{p.scope_id:<18}{show(p.daily_limit_usd):>12}"
              f"{show(p.monthly_limit_usd):>12}  {p.enabled}")
    print(f"\nSeeded {len(POLICIES)} policies ({len(policies)} total in budget_policies)")


if __name__ == "__main__":
    asyncio.run(main())
