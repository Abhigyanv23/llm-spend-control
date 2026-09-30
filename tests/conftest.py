"""Shared fixtures.

Why fakeredis + SQLite instead of the Docker containers?
  - Hermetic and fast: every test gets a brand-new empty Redis and database in milliseconds,
    so tests can't interfere with each other or with your dev data.
  - Runs anywhere (CI, a laptop without Docker).
  - fakeredis[lua] runs the REAL Lua scripts through an embedded Lua interpreter, so the
    atomic reserve/settle logic is genuinely exercised.
The trade-off: both are emulations. scripts/smoke_test.py covers the real Postgres + Redis.
"""
from decimal import Decimal

import fakeredis
import httpx
import pytest

from app.budgets import BudgetService, RedisBudgetStore
from app.budgets.policies import upsert_policy
from app.config import Settings
from app.db import Base, create_engine, create_session_factory
from app.main import create_app


@pytest.fixture
def redis_server():
    return fakeredis.FakeServer()


@pytest.fixture
async def redis_client(redis_server):
    client = fakeredis.FakeAsyncRedis(server=redis_server, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def engine(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine):
    return create_session_factory(engine)


@pytest.fixture
def store(redis_client):
    return RedisBudgetStore(redis_client)


@pytest.fixture
def budgets(store, session_factory):
    return BudgetService(store, session_factory, warn_threshold=Decimal("0.8"), fail_mode="open")


@pytest.fixture
def set_policy(session_factory):
    async def _set(scope: str, scope_id: str, daily=None, monthly=None, enabled=True):
        async with session_factory() as session:
            await upsert_policy(session, scope, scope_id,
                                None if daily is None else Decimal(str(daily)),
                                None if monthly is None else Decimal(str(monthly)), enabled)
            await session.commit()
    return _set


@pytest.fixture
async def api(engine, redis_client):
    """The real FastAPI app, in-process, wired to SQLite + fakeredis."""
    settings = Settings(_env_file=None, openai_api_key=None, anthropic_api_key=None)
    app = create_app(settings, engine=engine, redis_client=redis_client)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
