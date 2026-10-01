"""Export routing misses as JSONL: labelled training data for a future learned classifier.

    python scripts/export_misses.py                          -> misses.jsonl, everything
    python scripts/export_misses.py --out data/misses.jsonl --since 2026-10-01 --feature f1

Each line is one example: what the classifier saw (features, confidence, prompt if stored)
and the label (the tier that was actually needed). Prompts may contain user data: treat the
file like the database it came from.
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # make `app` importable

from app.budgets.periods import as_utc  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import create_engine, create_session_factory  # noqa: E402
from app.quality.reports import QualityFilter, iter_misses  # noqa: E402


def to_example(miss: dict) -> dict:
    return {"request_id": miss["request_id"],
            "created_at": miss["created_at"].isoformat() if miss["created_at"] else None,
            "feature": miss["feature"], "prompt": miss["prompt"],
            "classifier_features": miss["classifier_features"],
            "classifier_confidence": miss["classifier_confidence"],
            "chosen_model": miss["chosen_model"], "chosen_tier": miss["chosen_tier"],
            "better_model": miss["better_model"], "better_tier": miss["better_tier"],
            "label_tier": miss["better_tier"]}


async def export(session_factory, out_path: Path, f: QualityFilter) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out_path.open("w", encoding="utf-8", newline="\n") as fh:
        async for miss in iter_misses(session_factory, f):
            fh.write(json.dumps(to_example(miss), ensure_ascii=False) + "\n")
            count += 1
    return count


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="misses.jsonl")
    parser.add_argument("--since", type=datetime.fromisoformat, default=None,
                        help="ISO date/time, inclusive (naive = UTC)")
    parser.add_argument("--until", type=datetime.fromisoformat, default=None,
                        help="ISO date/time, exclusive")
    parser.add_argument("--feature", default=None)
    parser.add_argument("--team", default=None)
    args = parser.parse_args(argv)

    f = QualityFilter(start=as_utc(args.since) if args.since else None,
                      end=as_utc(args.until) if args.until else None,
                      team_id=args.team, feature=args.feature)
    engine = create_engine(settings.database_url)
    try:
        count = await export(create_session_factory(engine), Path(args.out), f)
    finally:
        await engine.dispose()
    print(f"Exported {count} routing miss(es) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
