"""Verification worker process.

    python -m app.worker                 run until Ctrl+C
    python -m app.worker --once          drain the jobs available now, print a summary, exit
    python -m app.worker --concurrency 8 --consumer laptop-a

A separate process from the API on purpose: verification is slow (two model calls per job),
must never compete with user requests for the API's event loop, can be scaled independently
(run several workers: the consumer group splits jobs between them), and can be stopped or
redeployed without touching the API.
"""
import argparse
import asyncio
import contextlib
import logging
import os
import signal
import socket
import sys

from app.bootstrap import build_core
from app.config import Settings
from app.quality.judges import build_judge
from app.quality.worker import VerificationWorker

logger = logging.getLogger("app.worker")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.worker",
                                     description="Verify sampled cheap answers.")
    parser.add_argument("--once", action="store_true",
                        help="process the jobs available now, then exit")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="jobs in flight at once (default: WORKER_CONCURRENCY)")
    parser.add_argument("--consumer", default=None,
                        help="consumer name, unique per process (default: WORKER_CONSUMER_NAME "
                             "or <hostname>-<pid>)")
    return parser.parse_args(argv)


def install_stop_handlers(stop: asyncio.Event) -> None:
    """Graceful shutdown that works on Windows: loop.add_signal_handler() is Unix-only, so use
    signal.signal() and hand the event to the loop thread-safely. First Ctrl+C: finish the
    current batch and exit. Second Ctrl+C: stop immediately (un-acked jobs are NOT lost; they
    stay pending and another worker reclaims them)."""
    loop = asyncio.get_running_loop()

    def handler(signum, _frame):
        if stop.is_set():
            raise KeyboardInterrupt
        logger.warning("Shutdown requested (signal %s): finishing the current batch. "
                       "Press Ctrl+C again to force.", signum)
        loop.call_soon_threadsafe(stop.set)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):       # SIGBREAK = Ctrl+Break on Windows
        if hasattr(signal, name):
            with contextlib.suppress(ValueError, OSError):    # not settable on this platform
                signal.signal(getattr(signal, name), handler)


async def amain(args: argparse.Namespace) -> int:
    settings = Settings()
    # The worker can afford a patient Redis client (no user is waiting on it)
    core = build_core(settings, redis_timeout_s=max(settings.redis_timeout_s, 5.0))
    try:
        quality = core.quality_config
        if not quality.verify_enabled:
            logger.warning("VERIFY_ENABLED=false: nothing to do.")
            return 0
        vcfg = quality.verification
        judge = build_judge(vcfg.judge, core.registry, core.adapters,
                            team_id=vcfg.budget_team_id, feature=vcfg.budget_feature)
        consumer = (args.consumer or settings.worker_consumer_name
                    or f"{socket.gethostname()}-{os.getpid()}")
        await core.store.load_scripts()
        worker = VerificationWorker(
            queue=core.queue, quality=quality, registry=core.registry, adapters=core.adapters,
            budgets=core.budgets, session_factory=core.session_factory, judge=judge,
            consumer=consumer, concurrency=args.concurrency or settings.worker_concurrency)
        logger.info("Reference model: %s; judge: %s; verification budget: team '%s' / "
                    "feature '%s'", vcfg.reference_model, judge.name, vcfg.budget_team_id,
                    vcfg.budget_feature)

        if args.once:
            stats = await worker.run_once()
            print(f"Processed {stats.summary()}")
            print(f"Queue: {await core.queue.stats()}")
        else:
            stop = asyncio.Event()
            install_stop_handlers(stop)
            await worker.run_forever(stop)
        return 0
    finally:
        await core.aclose()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    try:
        return asyncio.run(amain(parse_args(argv)))
    except KeyboardInterrupt:
        logger.warning("Forced stop. Un-acked jobs stay pending and will be reclaimed.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
