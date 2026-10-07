"""Report maths on small, fixed result files (no server needed)."""
from decimal import Decimal

from scripts.build_report import (
    build_summary,
    confusion,
    cost_summary,
    headline,
    paired_savings,
    render,
    routing_errors,
    verification_summary,
    visible_failures,
)


def row(mode, id_, true_tier, served=None, cost="0", status="ok", error_code=None, verdict=None,
        vcost="0", classifier=None, first=None, final=None, escalated=False, split="test",
        category="cat", label=None, team="ops"):
    return {"run_id": "t", "mode": mode, "label": label or mode, "sample_rate": 0.1 if mode == "C" else None,
            "id": id_, "category": category, "true_tier": true_tier, "split": split, "tags": [],
            "team_id": team, "feature": "f", "priority": "normal", "status": status,
            "error_code": error_code, "served_tier": served, "classifier_tier": classifier or served,
            "cost_usd": cost, "verdict": verdict, "verification_cost_usd": vcost,
            "first_check_failed": first, "final_check_failed": final, "escalated": escalated,
            "latency_ms_client": 100.0, "latency_ms_server": 90.0, "model": "m"}


A = [row("A", "1", 1, 3, "10"), row("A", "2", 3, 3, "10"),
     row("A", "3", 2, None, "0", status="error", error_code="budget_exceeded")]
C = [row("C", "1", 1, 1, "1", verdict="pass", vcost="2"), row("C", "2", 3, 2, "4", verdict="fail"),
     row("C", "3", 2, 2, "3")]


def test_paired_savings_ignore_requests_blocked_in_either_mode():
    s = paired_savings(A, C)
    # id 3 was blocked in A, so it is excluded: baseline 20, candidate 5, verification 2
    assert (s["paired_requests"], s["baseline_cost_usd"], s["cost_usd"]) == (2, Decimal(20), Decimal(5))
    assert (s["gross_savings_pct"], s["net_savings_pct"]) == (75.0, 65.0)


def test_cost_summary_counts_blocks_and_verification():
    c = cost_summary(A)
    assert (c["requests"], c["succeeded"], c["blocked"], c["errors"]) == (3, 2, 1, 0)
    assert cost_summary(C)["total_cost_usd"] == Decimal(10)


def test_confusion_and_routing_errors():
    assert confusion(C, "served_tier") == [[1, 0, 0], [0, 1, 0], [0, 1, 0]]
    e = routing_errors(C)
    assert (e["accuracy_pct"], e["under_routed"], e["over_routed"]) == (66.67, 1, 0)
    assert e["adequately_served_pct"] == 66.67
    assert e["per_tier"]["2"] == {"support": 1, "recall": 100.0, "precision": 50.0}


def test_verification_summary_uses_wilson():
    v = verification_summary(C)
    assert (v["judged"], v["pass_rate_pct"]) == (2, 50.0)
    assert v["pass_rate_ci95_pct"] == [9.4, 90.5]                 # Wilson(1, 2) = (0.0945, 0.9055)
    assert v["fail_rate_when_under_routed_pct"] == 100.0         # id 2: served 2 < true 3
    assert v["misses_by_category"] == {"cat": 1}


def test_escalation_rescue_and_report_rendering():
    b = [row("B", "9", 1, 1, "1", first="empty", final="empty")]
    c = [row("C", "9", 1, 2, "3", first="empty", final=None, escalated=True, verdict="pass")]
    assert visible_failures(b)["returned_broken"] == 1 and visible_failures(c)["escalated"] == 1
    summary = build_summary([row("A", "9", 1, 3, "10")] + b + c, {"run_id": "t", "prompts": 1})
    assert summary["escalation_rescue"] == {"broken_in_B": 1, "broken_in_C": 0,
                                            "rescued_by_escalation": 1}
    text = render(summary, {})
    assert "Reduced simulated LLM spend by 70.0%" in headline(summary)
    assert "## Limitations" in text and "| C |" in text
