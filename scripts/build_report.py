"""Build the savings and quality report for a simulation run (Phase 6).

    python scripts/build_report.py <run_id>          # reports/<run_id>/report.md + charts + summary.json

Works from reports/<run_id>/results.jsonl.gz (+ run.json) ALONE: no server or database needed, so
any run's report can be regenerated and reviewed later.

Fairness rule: modes can block different requests (the tight budget bites earlier on the
expensive baseline), and a blocked request costs $0. Headline savings are therefore computed on
the PAIRED set: requests that succeeded in both modes being compared.
"""
import argparse
import gzip
import json
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.analytics.common import percentile_cont, wilson_interval  # noqa: E402

TIERS = (1, 2, 3)
D0 = Decimal(0)


# ---------------------------------------------------------------- loading

def load(run_dir: Path) -> tuple[list[dict], dict]:
    gz, plain = run_dir / "results.jsonl.gz", run_dir / "results.jsonl"
    if gz.exists():
        with gzip.open(gz, "rt", encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = plain.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    run_path = run_dir / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8")) if run_path.exists() else {}
    return rows, run


def by_label(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["label"]].append(r)
    return dict(groups)


def ok(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["status"] == "ok"]


def money(rows: list[dict], key: str = "cost_usd") -> Decimal:
    return sum((Decimal(r.get(key) or "0") for r in rows), D0)


def pct(part, whole) -> float | None:
    return round(float(part) / float(whole) * 100, 2) if whole else None


# ---------------------------------------------------------------- cost

def cost_summary(rows: list[dict]) -> dict:
    blocked = [r for r in rows if r.get("error_code") in ("budget_exceeded", "override_required")]
    errors = [r for r in rows if r["status"] != "ok" and r not in blocked]
    spend, verification = money(rows), money(rows, "verification_cost_usd")
    return {"requests": len(rows), "succeeded": len(ok(rows)), "blocked": len(blocked),
            "errors": len(errors), "cost_usd": spend, "verification_cost_usd": verification,
            "total_cost_usd": spend + verification,
            "cost_per_1k_usd": (spend + verification) / len(rows) * 1000 if rows else D0}


def paired_savings(baseline: list[dict], candidate: list[dict]) -> dict:
    """Savings of `candidate` vs `baseline` on requests that succeeded in BOTH."""
    base_ok = {r["id"]: r for r in ok(baseline)}
    pairs = [(base_ok[r["id"]], r) for r in ok(candidate) if r["id"] in base_ok]
    base_cost = sum((Decimal(b["cost_usd"]) for b, _ in pairs), D0)
    cand_cost = sum((Decimal(c["cost_usd"]) for _, c in pairs), D0)
    verification = sum((Decimal(c.get("verification_cost_usd") or "0") for _, c in pairs), D0)
    return {"paired_requests": len(pairs), "baseline_cost_usd": base_cost,
            "cost_usd": cand_cost, "verification_cost_usd": verification,
            "gross_savings_usd": base_cost - cand_cost,
            "gross_savings_pct": pct(base_cost - cand_cost, base_cost),
            "net_savings_usd": base_cost - cand_cost - verification,
            "net_savings_pct": pct(base_cost - cand_cost - verification, base_cost)}


def cost_by(rows: list[dict], key: str) -> dict:
    out: dict = defaultdict(lambda: {"requests": 0, "cost_usd": D0})
    for r in ok(rows):
        bucket = out[str(r.get(key))]
        bucket["requests"] += 1
        bucket["cost_usd"] += Decimal(r["cost_usd"])
    return dict(sorted(out.items()))


# ---------------------------------------------------------------- routing quality

def confusion(rows: list[dict], key: str) -> list[list[int]]:
    """matrix[true_tier-1][predicted_tier-1] over successful rows with a predicted tier."""
    matrix = [[0, 0, 0] for _ in TIERS]
    for r in ok(rows):
        predicted = r.get(key)
        if predicted in TIERS:
            matrix[r["true_tier"] - 1][predicted - 1] += 1
    return matrix


def routing_errors(rows: list[dict], key: str = "served_tier") -> dict:
    """Under-routing (predicted < true: the DANGEROUS error, a weaker model than needed) and
    over-routing (predicted > true: the WASTEFUL one)."""
    scored = [r for r in ok(rows) if r.get(key) in TIERS]
    under = sum(r[key] < r["true_tier"] for r in scored)
    over = sum(r[key] > r["true_tier"] for r in scored)
    correct = len(scored) - under - over
    per_tier = {}
    for t in TIERS:
        truly = [r for r in scored if r["true_tier"] == t]
        predicted = [r for r in scored if r[key] == t]
        hit = sum(r[key] == t for r in truly)
        per_tier[str(t)] = {"support": len(truly), "recall": pct(hit, len(truly)),
                            "precision": pct(hit, len(predicted))}
    return {"scored": len(scored), "accuracy_pct": pct(correct, len(scored)),
            # Ground-truth quality: share served by a model at least as strong as needed
            "adequately_served_pct": pct(len(scored) - under, len(scored)),
            "under_routed": under, "under_routed_pct": pct(under, len(scored)),
            "over_routed": over, "over_routed_pct": pct(over, len(scored)), "per_tier": per_tier}


def visible_failures(rows: list[dict]) -> dict:
    """Answers that still failed a post-call check (empty, refusal, truncated, bad JSON)."""
    first = sum(bool(r.get("first_check_failed")) for r in ok(rows))
    final = sum(bool(r.get("final_check_failed")) for r in ok(rows))
    by_check = Counter(r["first_check_failed"] for r in ok(rows) if r.get("first_check_failed"))
    still = Counter(r["final_check_failed"] for r in ok(rows) if r.get("final_check_failed"))
    return {"cheap_attempt_failed": first, "returned_broken": final,
            "first_failure_by_check": dict(sorted(by_check.items())),
            "still_broken_by_check": dict(sorted(still.items())),
            "escalated": sum(bool(r.get("escalated")) for r in ok(rows)),
            "pre_call_escalated": sum(bool(r.get("pre_escalated")) for r in ok(rows))}


def verification_summary(rows: list[dict]) -> dict:
    verdicts = Counter(r["verdict"] for r in rows if r.get("verdict"))
    judged = verdicts["pass"] + verdicts["fail"]
    by_category: dict = defaultdict(lambda: {"pass": 0, "fail": 0})
    under_fail = [0, 0]                     # [fail, judged] among under-routed verified answers
    for r in rows:
        if r.get("verdict") in ("pass", "fail"):
            by_category[r["category"]][r["verdict"]] += 1
            if (r.get("served_tier") or 9) < r["true_tier"]:
                under_fail[1] += 1
                under_fail[0] += r["verdict"] == "fail"
    return {"verified": sum(verdicts.values()), **{v: verdicts[v] for v in
                                                   ("pass", "fail", "inconclusive", "skipped")},
            "judged": judged, "pass_rate_pct": pct(verdicts["pass"], judged),
            "pass_rate_ci95_pct": ([round(x * 100, 1) for x in wilson_interval(verdicts["pass"], judged)]
                                   if judged else None),
            "fail_rate_when_under_routed_pct": pct(*under_fail),
            "misses_by_category": {c: v["fail"] for c, v in sorted(by_category.items()) if v["fail"]}}


def budget_events(rows: list[dict]) -> dict:
    teams: dict = defaultdict(Counter)
    for r in rows:
        t = teams[r["team_id"]]
        t["requests"] += 1
        t["warnings"] += r.get("budget_status") == "warning"
        t["overridden"] += r.get("budget_status") == "overridden"
        t["downgrades"] += bool(r.get("downgraded"))
        t["blocks"] += r.get("error_code") in ("budget_exceeded", "override_required")
    return {team: dict(c) for team, c in sorted(teams.items())}


def latency(rows: list[dict], key: str = "latency_ms_client") -> dict:
    values = [float(r[key]) for r in ok(rows) if r.get(key) is not None]
    return {"p50_ms": percentile_cont(values, 0.5), "p95_ms": percentile_cont(values, 0.95),
            "p99_ms": percentile_cont(values, 0.99), "n": len(values)}


def latency_by_model(rows: list[dict]) -> dict:
    groups: dict = defaultdict(list)
    for r in ok(rows):
        groups[r.get("model")].append(r)
    return {m: latency(g, "latency_ms_server") for m, g in sorted(groups.items())}


# ---------------------------------------------------------------- summary

def build_summary(rows: list[dict], run: dict) -> dict:
    modes = by_label(rows)
    baseline = modes.get("A")
    summary = {"run_id": run.get("run_id") or (rows[0]["run_id"] if rows else None),
               "real_providers": run.get("real_providers", False),
               "prompts": run.get("prompts"), "git_commit": run.get("git_commit"),
               "dataset_sha256": run.get("dataset_sha256"), "modes": {}}
    for label, mode_rows in modes.items():
        info = {"mode": mode_rows[0]["mode"], "sample_rate": mode_rows[0].get("sample_rate"),
                "cost": cost_summary(mode_rows), "visible_failures": visible_failures(mode_rows),
                "latency_client": latency(mode_rows), "latency_server_by_model": latency_by_model(mode_rows),
                "cost_by_served_tier": cost_by(mode_rows, "served_tier"),
                "budget_events": budget_events(mode_rows)}
        if mode_rows[0]["mode"] != "A":
            info["routing_served"] = routing_errors(mode_rows, "served_tier")
            info["confusion_served"] = confusion(mode_rows, "served_tier")
            if baseline:
                info["vs_baseline"] = paired_savings(baseline, mode_rows)
        if mode_rows[0]["mode"] == "C":
            info["verification"] = verification_summary(mode_rows)
        summary["modes"][label] = info

    routed = next((m for m in ("B", "C") if m in modes), None)
    if routed:
        rows_r = modes[routed]
        summary["classifier"] = {
            "all": routing_errors(rows_r, "classifier_tier"),
            "train": routing_errors([r for r in rows_r if r["split"] == "train"], "classifier_tier"),
            "test": routing_errors([r for r in rows_r if r["split"] == "test"], "classifier_tier"),
            "confusion": confusion(rows_r, "classifier_tier")}
    if "B" in modes and "C" in modes:
        b_broken = {r["id"] for r in ok(modes["B"]) if r.get("final_check_failed")}
        c_broken = {r["id"] for r in ok(modes["C"]) if r.get("final_check_failed")}
        summary["escalation_rescue"] = {"broken_in_B": len(b_broken), "broken_in_C": len(c_broken),
                                        "rescued_by_escalation": len(b_broken - c_broken)}
    sweep = [(label, info) for label, info in summary["modes"].items() if info["mode"] == "C"]
    if len(sweep) > 1 or (sweep and sweep[0][1]["sample_rate"] is not None):
        summary["sample_rate_sweep"] = [{
            "label": label, "sample_rate": info["sample_rate"],
            "verified": info["verification"]["verified"],
            "pass_rate_pct": info["verification"]["pass_rate_pct"],
            "ci95_pct": info["verification"]["pass_rate_ci95_pct"],
            "verification_cost_usd": info["cost"]["verification_cost_usd"],
            "net_savings_pct": info.get("vs_baseline", {}).get("net_savings_pct")}
            for label, info in sorted(sweep, key=lambda x: x[1]["sample_rate"] or 0)]
    return summary


def headline(summary: dict) -> str:
    c = summary["modes"].get("C")
    if not c or "vs_baseline" not in c:
        return "Run modes A and C to compute the headline."
    v = c["verification"]
    ci = v["pass_rate_ci95_pct"]
    if not ci:
        return "No verified answers in mode C."
    adequate = c.get("routing_served", {}).get("adequately_served_pct")
    return (f"Reduced simulated LLM spend by {c['vs_baseline']['net_savings_pct']:.1f}% "
            f"(net of verification; {c['vs_baseline']['gross_savings_pct']:.1f}% gross) while "
            f"maintaining a {v['pass_rate_pct']:.1f}% verification pass rate "
            f"(95% CI {ci[0]}-{ci[1]}%, {v['judged']} verified answers); "
            f"{adequate}% of requests were served at or above their ground-truth tier.")


# ---------------------------------------------------------------- charts

def charts(summary: dict, out: Path) -> dict[str, str]:
    import matplotlib
    matplotlib.use("Agg")                                   # no display needed
    import matplotlib.pyplot as plt

    files = {}
    labels = list(summary["modes"])
    spend = [float(summary["modes"][m]["cost"]["cost_usd"]) for m in labels]
    verify = [float(summary["modes"][m]["cost"]["verification_cost_usd"]) for m in labels]
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(labels, spend, label="requests", color="#4C78A8")
    ax.bar(labels, verify, bottom=spend, label="verification", color="#F58518")
    ax.set_ylabel("Cost (USD)")
    ax.set_title("Total cost by mode (A = all on strongest model)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "cost_by_mode.png", dpi=120)
    plt.close(fig)
    files["cost"] = "cost_by_mode.png"

    if "classifier" in summary:
        matrix = summary["classifier"]["confusion"]
        fig, ax = plt.subplots(figsize=(4.5, 4))
        ax.imshow(matrix, cmap="Blues")
        for i in range(3):
            for j in range(3):
                ax.text(j, i, matrix[i][j], ha="center", va="center",
                        color="white" if matrix[i][j] > max(map(max, matrix)) / 2 else "black")
        ax.set_xticks(range(3), ["1", "2", "3"])
        ax.set_yticks(range(3), ["1", "2", "3"])
        ax.set_xlabel("Classifier tier")
        ax.set_ylabel("True tier")
        ax.set_title("Routing confusion matrix")
        fig.tight_layout()
        fig.savefig(out / "confusion_matrix.png", dpi=120)
        plt.close(fig)
        files["confusion"] = "confusion_matrix.png"

    fig, ax = plt.subplots(figsize=(7, 4))
    width = 0.25
    for k, p in enumerate(("p50_ms", "p95_ms", "p99_ms")):
        values = [summary["modes"][m]["latency_client"][p] or 0 for m in labels]
        ax.bar([i + (k - 1) * width for i in range(len(labels))], values, width, label=p[:3])
    ax.set_xticks(range(len(labels)), labels)
    ax.set_ylabel("Client latency (ms)")
    ax.set_title("Latency percentiles by mode")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "latency.png", dpi=120)
    plt.close(fig)
    files["latency"] = "latency.png"

    sweep = summary.get("sample_rate_sweep") or []
    if len(sweep) > 1:
        fig, ax1 = plt.subplots(figsize=(7, 4))
        rates = [s["sample_rate"] for s in sweep]
        ax1.plot(rates, [s["net_savings_pct"] for s in sweep], "o-", color="#4C78A8",
                 label="net savings %")
        ax1.set_xlabel("Base sample rate")
        ax1.set_ylabel("Net savings vs baseline (%)")
        ax2 = ax1.twinx()
        widths = [(s["ci95_pct"][1] - s["ci95_pct"][0]) if s["ci95_pct"] else None for s in sweep]
        ax2.plot(rates, widths, "s--", color="#F58518", label="pass-rate CI width")
        ax2.set_ylabel("95% CI width (points)")
        ax1.set_title("More verification: tighter estimate, smaller net savings")
        fig.tight_layout()
        fig.savefig(out / "sample_rate_sweep.png", dpi=120)
        plt.close(fig)
        files["sweep"] = "sample_rate_sweep.png"
    return files


# ---------------------------------------------------------------- markdown

def usd(value) -> str:
    return f"${Decimal(value):,.4f}"


def render(summary: dict, files: dict[str, str]) -> str:
    m = summary["modes"]
    lines = [f"# Simulation report: run `{summary['run_id']}`", ""]
    if summary["real_providers"]:
        lines += ["> **Real providers**: costs and answers are real (small subset).", ""]
    else:
        lines += ["> **Dev profile**: mock models priced like real tiers. Costs follow real "
                  "price ratios; answer quality is *simulated* (see Limitations).", ""]
    lines += ["## Headline", "", f"**{headline(summary)}**", "",
              f"Prompts: {summary['prompts']} · commit `{summary['git_commit']}` · dataset sha256 "
              f"`{(summary['dataset_sha256'] or '')[:16]}`", ""]

    lines += ["## Cost", "", "| Mode | Requests | OK | Blocked | Errors | Request cost | "
              "Verification | Total | Per 1k requests |", "|---|---|---|---|---|---|---|---|---|"]
    for label, info in m.items():
        c = info["cost"]
        lines.append(f"| {label} | {c['requests']} | {c['succeeded']} | {c['blocked']} | {c['errors']} | "
                     f"{usd(c['cost_usd'])} | {usd(c['verification_cost_usd'])} | "
                     f"{usd(c['total_cost_usd'])} | {usd(c['cost_per_1k_usd'])} |")
    lines += ["", "Savings vs A on the **paired** set (requests that succeeded in both modes):", "",
              "| Mode | Paired | Baseline cost | Mode cost | Verification | Gross savings | Net savings |",
              "|---|---|---|---|---|---|---|"]
    for label, info in m.items():
        if "vs_baseline" in info:
            s = info["vs_baseline"]
            lines.append(f"| {label} | {s['paired_requests']} | {usd(s['baseline_cost_usd'])} | "
                         f"{usd(s['cost_usd'])} | {usd(s['verification_cost_usd'])} | "
                         f"{s['gross_savings_pct']}% | {s['net_savings_pct']}% |")
    if "cost" in files:
        lines += ["", f"![Cost by mode]({files['cost']})"]

    lines += ["", "### Cost by served tier", "", "| Mode | Tier 1 | Tier 2 | Tier 3 |", "|---|---|---|---|"]
    for label, info in m.items():
        t = info["cost_by_served_tier"]
        cells = [f"{t[k]['requests']} req · {usd(t[k]['cost_usd'])}" if k in t else "–"
                 for k in ("1", "2", "3")]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    if "classifier" in summary:
        cl = summary["classifier"]
        lines += ["", "## Routing quality", "",
                  "Classifier tier vs ground-truth tier (routed requests):", "",
                  "| Split | Scored | Accuracy | Under-routed (dangerous) | Over-routed (wasteful) |",
                  "|---|---|---|---|---|"]
        for split in ("all", "train", "test"):
            e = cl[split]
            lines.append(f"| {split} | {e['scored']} | {e['accuracy_pct']}% | {e['under_routed']} "
                         f"({e['under_routed_pct']}%) | {e['over_routed']} ({e['over_routed_pct']}%) |")
        lines += ["", "Per tier (all): " + ", ".join(
            f"tier {t}: precision {v['precision']}% / recall {v['recall']}%"
            for t, v in cl["all"]["per_tier"].items())]
        if "confusion" in files:
            lines += ["", f"![Confusion matrix]({files['confusion']})"]
        for label, info in m.items():
            if "routing_served" in info:
                e = info["routing_served"]
                lines.append(f"\n**Served tier, mode {label}** (after feature rules, pre-call "
                             f"escalation, downgrades and the cascade): accuracy {e['accuracy_pct']}%, "
                             f"**adequately served {e['adequately_served_pct']}%**, under-routed "
                             f"{e['under_routed_pct']}%, over-routed {e['over_routed_pct']}%.")

    if "escalation_rescue" in summary:
        r = summary["escalation_rescue"]
        lines += ["", "## Escalation", "",
                  f"Visibly broken answers returned to users: **{r['broken_in_B']} in B** (no "
                  f"escalation) vs **{r['broken_in_C']} in C**; {r['rescued_by_escalation']} rescued "
                  "by the cascade."]
        for label, info in m.items():
            v = info["visible_failures"]
            lines.append(f"- {label}: cheap attempt failed {v['cheap_attempt_failed']} "
                         f"{v['first_failure_by_check'] or ''}, escalated {v['escalated']}, "
                         f"pre-call escalations {v['pre_call_escalated']}, returned broken "
                         f"{v['returned_broken']} {v['still_broken_by_check'] or ''}")
        lines += ["", "Mock models never emit real JSON, so `invalid_json` failures on JSON-output "
                  "prompts survive escalation in the dev profile: that remainder is a property of "
                  "the mocks, not of the cascade."]

    for label, info in m.items():
        if "verification" in info:
            v = info["verification"]
            lines += ["", f"## Verification (mode {label}, sample rate {info['sample_rate']})", "",
                      f"{v['verified']} verified: {v['pass']} pass, {v['fail']} fail, "
                      f"{v['inconclusive']} inconclusive, {v['skipped']} skipped. Pass rate "
                      f"**{v['pass_rate_pct']}%** (95% Wilson CI {v['pass_rate_ci95_pct']}). "
                      f"Fail rate among under-routed verified answers: "
                      f"{v['fail_rate_when_under_routed_pct']}%.", "",
                      "Misses by category: " + (", ".join(f"{c} {n}" for c, n in
                                                          v["misses_by_category"].items()) or "none")]
    if summary.get("sample_rate_sweep"):
        lines += ["", "## Sample-rate sweep", "",
                  "| Base rate | Verified | Pass rate | 95% CI | Verification cost | Net savings |",
                  "|---|---|---|---|---|---|"]
        for s in summary["sample_rate_sweep"]:
            lines.append(f"| {s['sample_rate']} | {s['verified']} | {s['pass_rate_pct']}% | "
                         f"{s['ci95_pct']} | {usd(s['verification_cost_usd'])} | {s['net_savings_pct']}% |")
        if "sweep" in files:
            lines += ["", f"![Sample-rate sweep]({files['sweep']})"]

    lines += ["", "## Budgets", "",
              "| Mode | Team | Requests | Warnings | Downgrades | Blocks | Overridden |",
              "|---|---|---|---|---|---|---|"]
    for label, info in m.items():
        for team, e in info["budget_events"].items():
            if e.get("warnings") or e.get("downgrades") or e.get("blocks") or e.get("overridden"):
                lines.append(f"| {label} | {team} | {e['requests']} | {e['warnings']} | "
                             f"{e['downgrades']} | {e['blocks']} | {e['overridden']} |")

    lines += ["", "## Latency (client-side)", "", "| Mode | p50 | p95 | p99 |", "|---|---|---|---|"]
    for label, info in m.items():
        lt = info["latency_client"]
        lines.append(f"| {label} | {lt['p50_ms']} ms | {lt['p95_ms']} ms | {lt['p99_ms']} ms |")
    if "latency" in files:
        lines += ["", f"![Latency]({files['latency']})"]

    lines += ["", "## What this means", "",
              "- Routing moves most traffic off the strongest model; the gross saving comes mainly "
              "from tier-1 and tier-2 requests.",
              "- Verification is the price of knowing the routing is safe: compare gross and net.",
              "- The cascade turns visibly broken cheap answers into good ones at the cost of a "
              "second call on those requests only.",
              "- Under-routing is the error to watch. Here it is measured against ground truth: "
              "with mock models the similarity judge cannot see it (a short prompt that needed "
              "tier 3 still gets an 'equivalent' echo from tier 1), so the verification pass "
              "rate overstates quality. With real models an LLM judge is needed to catch it.",
              "", "## Limitations", "",
              "- Mock models: answer quality depends on prompt length and test directives, not on "
              "true task difficulty, so pass rates are simulated; costs follow real price ratios.",
              "- The baseline counterfactual prices the *actual* tokens on the strongest model; a "
              "stronger model might write a different number of tokens.",
              "- Ground-truth tiers are one author's labels; the dataset and the classifier share an "
              "author, which risks overfitting (mitigated by the template-level held-out split).",
              "- The keyword classifier is English-only.",
              "- One run on one machine: latency numbers are for comparison between modes only."]
    return "\n".join(lines) + "\n"


def jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the report for a simulation run.")
    parser.add_argument("run_id")
    parser.add_argument("--no-charts", action="store_true")
    args = parser.parse_args(argv)
    run_dir = ROOT / "reports" / args.run_id
    rows, run = load(run_dir)
    summary = build_summary(rows, run)
    files = {} if args.no_charts else charts(summary, run_dir)
    (run_dir / "summary.json").write_text(json.dumps(jsonable(summary), indent=2), encoding="utf-8")
    (run_dir / "report.md").write_text(render(summary, files), encoding="utf-8")
    print(headline(summary))
    print(f"wrote {run_dir / 'report.md'}, summary.json" + (f" and {len(files)} chart(s)" if files else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
