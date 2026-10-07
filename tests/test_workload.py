"""The labelled workload generator: determinism, distribution, leak-free splits."""
import json
from collections import Counter, defaultdict
from pathlib import Path

from scripts.generate_workload import CATEGORIES, generate, split_templates

ROWS = generate(42)


def test_deterministic_and_committed_file_matches():
    assert generate(42) == ROWS
    assert generate(7) != ROWS
    committed = [json.loads(line) for line in
                 Path("data/workload.jsonl").read_text(encoding="utf-8").splitlines()]
    assert committed == ROWS, "data/workload.jsonl is stale: run scripts/generate_workload.py"


def test_size_ids_and_tier_mix():
    assert len(ROWS) == 1000 == sum(c[1] for c in CATEGORIES.values())
    assert len({r["id"] for r in ROWS}) == 1000
    tiers = Counter(r["true_tier"] for r in ROWS)
    assert tiers[1] > tiers[2] > 0 and tiers[3] > 0           # most traffic is simple
    assert tiers == {1: 480, 2: 270, 3: 250}


def test_split_is_by_template_with_no_leakage():
    splits_per_template = defaultdict(set)
    for r in ROWS:
        splits_per_template[r["template_id"]].add(r["split"])
    assert all(len(s) == 1 for s in splits_per_template.values())   # never in both splits
    for category in CATEGORIES:
        splits = {r["split"] for r in ROWS if r["category"] == category}
        assert splits == {"train", "test"}, category                 # every category is tested
    assert 0.25 < sum(r["split"] == "test" for r in ROWS) / 1000 < 0.35


def test_split_assignment_is_stable():
    assert split_templates("extraction", 6) == split_templates("extraction", 6)
    assert len(split_templates("negation", 4)) == 1


def test_records_are_valid_chat_requests():
    from app.schemas import ChatRequest
    for r in ROWS:
        ChatRequest(team_id=r["team_id"], feature=r["feature"], priority=r["priority"],
                    max_tokens=r["max_tokens"], messages=r["messages"])


def test_tricky_cases_and_directives_are_where_they_belong():
    directive = [r for r in ROWS if "[[mock:" in json.dumps(r["messages"])]
    assert {r["category"] for r in directive} == {"simulated_failure"}
    multi = [r for r in ROWS if r["category"] == "multi_turn"]
    assert all(len(r["messages"]) >= 3 for r in multi)
    long_docs = [r for r in ROWS if r["category"] == "long_input"]
    assert all(len(r["messages"][0]["content"]) > 24_000 for r in long_docs)   # > 6,000 tokens


def test_workload_features_have_no_seeded_feature_budgets():
    """Regression: the workload once used feature 'summarize', which has a seeded $2/day
    FEATURE budget. Feature budgets apply across teams, so simulation results depended on
    unrelated traffic and on the order the modes ran in."""
    from scripts.seed_budgets import POLICIES
    seeded = {scope_id for scope, scope_id, *_ in POLICIES if scope == "feature"}
    assert not seeded & {r["feature"] for r in ROWS}
