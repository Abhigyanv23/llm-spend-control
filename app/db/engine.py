from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


def create_engine(database_url: str) -> AsyncEngine:
    """One engine per process. It owns the connection pool, like the shared httpx client."""
    # pool_pre_ping: test a pooled connection before using it, so a Postgres restart
    # produces one reconnect instead of an error on the next request
    return create_async_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    # expire_on_commit=False: objects stay readable after commit (no surprise lazy reloads,
    # which would fail in async code)
    return async_sessionmaker(engine, expire_on_commit=False)
