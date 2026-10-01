"""Verification queue on Redis Streams.

Why Streams and not Celery/RQ: Celery doesn't officially support Windows and is sync-first,
RQ needs fork() (not on Windows), and Streams reuse the Redis we already run while exposing
the fundamentals directly: consumer groups, acknowledgements, pending entries, redelivery.

Delivery is AT-LEAST-ONCE:
  XADD          producer appends a job (MAXLEN ~ caps memory)
  XREADGROUP    a consumer in the group claims new jobs; they enter its Pending Entries List
  XACK          only after the result is durably stored -> removed from the PEL
  XAUTOCLAIM    jobs pending longer than job_timeout (worker crashed or failed) are taken over
                by another consumer; every claim increments the job's delivery count
  dead letter   after max_attempts the job is copied to a dead-letter stream and acked
A job can therefore be processed twice; idempotency comes from verifications.request_id UNIQUE.
"""
import logging
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.quality.config import VerificationConfig
from app.quality.jobs import VerificationJob

logger = logging.getLogger(__name__)

Entry = tuple[str, dict[str, str]]          # (message id, fields)


class VerificationQueue:
    def __init__(self, client: Redis, *, stream: str, group: str, dead_letter_stream: str,
                 maxlen: int):
        self.client = client
        self.stream = stream
        self.group = group
        self.dead_letter_stream = dead_letter_stream
        self.maxlen = maxlen

    @classmethod
    def from_config(cls, client: Redis, cfg: VerificationConfig) -> "VerificationQueue":
        return cls(client, stream=cfg.stream, group=cfg.consumer_group,
                   dead_letter_stream=cfg.dead_letter_stream, maxlen=cfg.maxlen)

    # ------------------------------------------------------------------ producer

    async def enqueue(self, job: VerificationJob) -> str:
        # approximate=True (MAXLEN ~): Redis trims whole internal nodes, which is much cheaper
        # than trimming to an exact length on every append
        return await self.client.xadd(self.stream, job.to_fields(), maxlen=self.maxlen,
                                      approximate=True)

    async def enqueue_safe(self, job: VerificationJob) -> str | None:
        """For the API's background task: verification is best-effort and must never affect
        the user's request. Failures are logged and dropped."""
        try:
            message_id = await self.enqueue(job)
            logger.info("Verification job queued request_id=%s id=%s", job.request_id,
                        message_id)
            return message_id
        except Exception as exc:
            logger.warning("Could not enqueue verification job request_id=%s (%s: %s)",
                           job.request_id, type(exc).__name__, exc)
            return None

    # ------------------------------------------------------------------ consumer

    async def ensure_group(self) -> None:
        """Create the consumer group (and the stream) if missing. id="0": a brand-new group
        also picks up jobs that were queued before any worker ever started."""
        try:
            await self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read(self, consumer: str, count: int, block_ms: int | None) -> list[Entry]:
        """New jobs never delivered to anyone (">"). block_ms=None returns immediately."""
        response = await self.client.xreadgroup(self.group, consumer, {self.stream: ">"},
                                                count=count, block=block_ms)
        entries: list[Entry] = []
        for _stream, messages in response or []:
            entries.extend((mid, fields) for mid, fields in messages if fields is not None)
        return entries

    async def reclaim(self, consumer: str, min_idle_ms: int, count: int) -> list[Entry]:
        """Take over jobs that another (or this) consumer received but never acked within
        min_idle_ms: the 'visibility timeout' of this queue."""
        result = await self.client.xautoclaim(self.stream, self.group, consumer,
                                              min_idle_time=min_idle_ms, start_id="0-0",
                                              count=count)
        messages = result[1] if len(result) > 1 else []
        return [(mid, fields) for mid, fields in messages if fields]

    async def delivery_count(self, message_id: str) -> int:
        pending = await self.client.xpending_range(self.stream, self.group, min=message_id,
                                                   max=message_id, count=1)
        return int(pending[0]["times_delivered"]) if pending else 1

    async def ack(self, message_id: str) -> None:
        await self.client.xack(self.stream, self.group, message_id)

    async def dead_letter(self, message_id: str, fields: dict[str, str], reason: str,
                          attempts: int) -> None:
        """Park a job that can't be processed (poison message or too many failures) where a
        human can inspect it, then ack it so it stops being redelivered."""
        entry = {**fields, "original_id": message_id, "reason": reason[:500],
                 "attempts": str(attempts),
                 "failed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")}
        async with self.client.pipeline(transaction=True) as pipe:
            pipe.xadd(self.dead_letter_stream, entry, maxlen=self.maxlen, approximate=True)
            pipe.xack(self.stream, self.group, message_id)
            await pipe.execute()
        logger.error("Verification job %s dead-lettered after %d attempt(s): %s",
                     message_id, attempts, reason)

    async def remove_consumer_if_idle(self, consumer: str) -> bool:
        """Tidy up short-lived consumers (e.g. `--once` runs). Only when nothing is pending,
        because XGROUP DELCONSUMER discards that consumer's pending entries."""
        try:
            consumers = await self.client.xinfo_consumers(self.stream, self.group)
        except ResponseError:
            return False
        for info in consumers:
            if info["name"] == consumer and int(info["pending"]) == 0:
                await self.client.xgroup_delconsumer(self.stream, self.group, consumer)
                return True
        return False

    # ------------------------------------------------------------------ observability

    async def stats(self) -> dict:
        length = await self.client.xlen(self.stream)
        dead = await self.client.xlen(self.dead_letter_stream)
        try:
            summary = await self.client.xpending(self.stream, self.group)
            consumers = await self.client.xinfo_consumers(self.stream, self.group)
        except ResponseError:            # group not created yet: no worker has ever run
            summary, consumers = {"pending": 0}, []
        return {"stream": self.stream, "consumer_group": self.group,
                "length": int(length), "pending": int(summary.get("pending") or 0),
                "dead_letter_stream": self.dead_letter_stream, "dead_letter": int(dead),
                "consumers": [{"name": c["name"], "pending": int(c["pending"]),
                               "idle_ms": int(c["idle"])} for c in consumers]}
