"""Offline classifier evaluation on the labelled workload (Phase 6). No server needed.

    python scripts/evaluate_classifier.py --split train --show-misses     # for DEVELOPMENT
    python scripts/evaluate_classifier.py --split test --compare          # final, held-out

Rule of the experiment: improvements are designed by looking ONLY at the training split. The
test split is used once, at the end, to compare versions. Tuning on test data would turn it into
training data and the reported improvement would be meaningless (test-set leakage).
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.registry import ModelRegistry  # noqa: E402
from app.routing import RuleBasedClassifier, load_routing_config  # noqa: E402
from app.schemas import ChatRequest  # noqa: E402

DATASET = ROOT / "data" / "workload.jsonl"


def classifier_for(version: str, routing_path: str = "config/routing.yaml") -> RuleBasedClassifier:
    registry = ModelRegistry(str(ROOT / "config" / "models.yaml"))
    config = load_routing_config(str(ROOT / routing_path), "dev", registry)
    return RuleBasedClassifier(config.classifier, version=version)


def evaluate(records: list[dict], classifier: RuleBasedClassifier) -> dict:
    confusion = [[0, 0, 0] for _ in range(3)]
    misses = []
    for r in records:
        request = ChatRequest(team_id="eval", feature=r["feature"], messages=r["messages"])
        result = classifier.classify(request)
        confusion[r["true_tier"] - 1][result.tier - 1] += 1
        if result.tier != r["true_tier"]:
            misses.append({"id": r["id"], "category": r["category"], "template": r["template_id"],
                           "true": r["true_tier"], "predicted": result.tier,
                           "reasons": list(result.reasons),
                           "text": r["messages"][-1]["content"][:110]})
    n = len(records)
    under = sum(confusion[t][p] for t in range(3) for p in range(3) if p < t)
    over = sum(confusion[t][p] for t in range(3) for p in range(3) if p > t)
    return {"n": n, "accuracy": round((n - under - over) / n, 4) if n else None,
            "under_routed": under, "over_routed": over, "confusion": confusion,
            "misses": misses}


def load(split: str) -> list[dict]:
    rows = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines()]
    return rows if split == "all" else [r for r in rows if r["split"] == split]


def show(name: str, result: dict) -> None:
    print(f"{name:10} n={result['n']}  accuracy={result['accuracy']:.1%}  "
          f"under-routed={result['under_routed']}  over-routed={result['over_routed']}")
    print("           confusion (rows = true tier 1-3, cols = predicted 1-3):",
          result["confusion"])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--split", choices=["train", "test", "all"], default="train")
    p.add_argument("--version", default="rules-v1")
    p.add_argument("--compare", action="store_true", help="evaluate rules-v1 and rules-v2")
    p.add_argument("--show-misses", action="store_true")
    p.add_argument("--json", type=Path, help="write the results as JSON")
    args = p.parse_args(argv)

    records = load(args.split)
    versions = ["rules-v1", "rules-v2"] if args.compare else [args.version]
    output = {}
    print(f"split={args.split}")
    for version in versions:
        result = evaluate(records, classifier_for(version))
        show(version, result)
        output[version] = {k: v for k, v in result.items() if k != "misses"}
        if args.show_misses:
            by = Counter((m["category"], m["true"], m["predicted"]) for m in result["misses"])
            for (category, true, predicted), count in by.most_common():
                example = next(m for m in result["misses"] if m["category"] == category
                               and m["true"] == true and m["predicted"] == predicted)
                print(f"   {count:3} x {category:18} true {true} -> {predicted}  "
                      f"e.g. {example['text']!r}  [{'; '.join(example['reasons'])}]")
    if args.json:
        args.json.write_text(json.dumps({"split": args.split, **output}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
