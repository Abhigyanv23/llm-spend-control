"""Persistence for verification results and routing misses."""
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import RoutingMiss, Verification


async def verification_exists(session_factory: async_sessionmaker, request_id: str) -> bool:
    async with session_factory() as session:
        found = await session.scalar(select(Verification.id).where(
            Verification.request_id == uuid.UUID(request_id)))
    return found is not None


async def save_verification(session_factory: async_sessionmaker, verification: Verification,
                            miss: RoutingMiss | None = None) -> bool:
    """Insert the verification (and its routing miss) in ONE transaction: both or neither.
    Returns False if this request was already verified. That happens when a job is delivered
    twice (at-least-once) and two workers race; the UNIQUE request_id makes the second insert
    fail instead of creating a duplicate."""
    try:
        async with session_factory() as session:
            session.add(verification)
            if miss is not None:
                session.add(miss)
            await session.commit()
        return True
    except IntegrityError:
        return False
