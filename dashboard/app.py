"""LLM Spend Control Center: cost dashboard (Phase 5).

    python -m streamlit run dashboard/app.py

Reads ONLY from the analytics API (GATEWAY_URL, default http://127.0.0.1:8000), never from the
database: the business logic lives in one place (the API), the API can be secured later, and
the UI stays a thin presentation layer. Charts use Altair (declarative Vega-Lite, already a
Streamlit dependency, good defaults for time series and tooltips).
"""
import os
from datetime import UTC, datetime, timedelta

import altair as alt
import httpx
import pandas as pd
import streamlit as st

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")
CACHE_TTL_S = 30

st.set_page_config(page_title="LLM Spend Control Center", page_icon="💸", layout="wide")


# ---------------------------------------------------------------- data access

@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def fetch(path: str, params: tuple) -> dict:
    """GET an analytics endpoint. Cached per (path, params) for CACHE_TTL_S seconds."""
    resp = httpx.get(f"{GATEWAY_URL}/v1/analytics/{path}",
                     params={k: v for k, v in params if v not in (None, "")}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def api(path: str, **params) -> dict | None:
    try:
        return fetch(path, tuple(sorted(params.items())))
    except httpx.HTTPError as exc:
        st.error(f"Could not load `{path}` from {GATEWAY_URL}: {exc}. Is the API running?")
        return None


def usd(value) -> float:
    """API money is a fixed-point string; convert for display and charting only."""
    return float(value or 0)


def fmt_usd(value) -> str:
    amount = usd(value)
    return f"${amount:,.2f}" if abs(amount) >= 1 else f"${amount:,.4f}"


def pct(value, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}%"


def rate(value) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def empty(message: str = "No data in this window.") -> None:
    st.info(message)


# ---------------------------------------------------------------- sidebar filters

st.sidebar.title("💸 Spend Control")
days = st.sidebar.selectbox("Window", [7, 30, 90], index=1, format_func=lambda d: f"Last {d} days")
team = st.sidebar.text_input("Team (optional)", placeholder="e.g. demo-search").strip() or None
feature = st.sidebar.text_input("Feature (optional)", placeholder="e.g. summarize").strip() or None
if st.sidebar.button("Refresh data"):
    st.cache_data.clear()
st.sidebar.caption(f"API: `{GATEWAY_URL}` · cached {CACHE_TTL_S}s · all times UTC")

now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
window = {"from": (now - timedelta(days=days)).isoformat(), "to": now.isoformat(),
          "team_id": team, "feature": feature}

st.title("LLM Spend Control Center")
summary = api("summary", **window)
if summary is None:
    st.stop()
st.caption(f"Data as of {summary['generated_at']} (UTC) · window: last {days} days"
           + (f" · team **{team}**" if team else "") + (f" · feature **{feature}**" if feature else ""))

tabs = st.tabs(["Overview", "Spend", "Budgets", "Savings", "Routing quality", "Performance"])

# ---------------------------------------------------------------- Overview

with tabs[0]:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Spend today", fmt_usd(summary["spend_today_usd"]))
    c2.metric("Month to date", fmt_usd(summary["spend_month_to_date_usd"]))
    c3.metric("Projected month end", fmt_usd(summary["projected_month_end_usd"]),
              help="Run-rate: month-to-date / elapsed days x days in month")
    c4.metric("Net savings", pct(summary["net_savings_pct"]),
              help="(baseline - actual - verification spend) / baseline, routed requests")
    c5, c6, c7, c8 = st.columns(4)
    ci = summary["verifier_pass_rate_ci95"]
    c5.metric("Verifier pass rate", rate(summary["verifier_pass_rate"]),
              help=f"95% Wilson interval: {rate(ci[0])} to {rate(ci[1])}" if ci else None)
    c6.metric("Escalation rate", rate(summary["escalation_rate"]))
    c7.metric("Requests in window", f"{summary['requests']:,}")
    c8.metric("Error rate", rate(summary["error_rate"]))

    left, right = st.columns(2)
    with left:
        st.subheader("Scopes at risk")
        if summary["scopes_at_risk"]:
            st.dataframe(pd.DataFrame(summary["scopes_at_risk"]), hide_index=True,
                         width="stretch")
        else:
            st.success("No team or feature is projected to exceed its monthly limit.")
    with right:
        st.subheader("Active budget alerts")
        alerts = summary["active_budget_alerts"]
        if alerts:
            frame = pd.DataFrame(alerts)
            frame["threshold"] = (frame["threshold"] * 100).map("{:.0f}%".format)
            st.dataframe(frame[["scope", "scope_id", "period", "period_key", "threshold",
                                "projected_usd", "limit_usd"]],
                         hide_index=True, width="stretch")
        else:
            st.success("No budget alerts this day/month.")

# ---------------------------------------------------------------- Spend

with tabs[1]:
    group_by = st.radio("Group by", ["team", "feature", "model"], horizontal=True)
    spend = api("spend", **window, group_by=group_by)
    if spend and spend["series"]:
        rows = [{"date": p["date"], group_by: s["key"], "cost_usd": usd(p["cost_usd"]),
                 "requests": p["requests"]} for s in spend["series"] for p in s["points"]]
        frame = pd.DataFrame(rows)
        st.markdown(f"**Total: {fmt_usd(spend['total_cost_usd'])}** over {len(spend['days'])} days")
        chart = alt.Chart(frame).mark_area(opacity=0.8).encode(
            x=alt.X("date:T", title="Day (UTC)"),
            y=alt.Y("sum(cost_usd):Q", title="Cost (USD)", stack=True),
            color=alt.Color(f"{group_by}:N", title=group_by.capitalize()),
            tooltip=["date:T", f"{group_by}:N", alt.Tooltip("cost_usd:Q", format="$.4f"),
                     "requests:Q"])
        st.altair_chart(chart, width="stretch")
        st.subheader("Cost by model")
        models = pd.DataFrame(spend["by_model"])
        for column in ("cost_usd", "avg_cost_usd"):
            models[column] = models[column].map(usd)
        st.dataframe(models, hide_index=True, width="stretch",
                     column_config={"cost_usd": st.column_config.NumberColumn("Cost (USD)", format="$%.4f"),
                                    "avg_cost_usd": st.column_config.NumberColumn("Avg / request", format="$%.6f")})
        st.subheader("Most expensive prompt patterns")
        top = api("top", **window, kind="patterns", limit=10)
        if top and top["items"]:
            patterns = pd.DataFrame([{**i, "total_cost_usd": usd(i["total_cost_usd"]),
                                      "avg_cost_usd": usd(i["avg_cost_usd"]),
                                      "models": ", ".join(f"{m} ({n})" for m, n in i["models"].items()),
                                      "pattern": i["prompt_fingerprint"][:12]}
                                     for i in top["items"]])
            st.dataframe(patterns[["pattern", "prompt_preview", "requests", "total_cost_usd",
                                   "avg_cost_usd", "models"]], hide_index=True,
                         width="stretch")
            st.caption("Patterns group prompts by a one-way fingerprint (digits normalised); "
                       "previews appear only when privacy settings allow storing them.")
    else:
        empty()

# ---------------------------------------------------------------- Budgets

with tabs[2]:
    proj = api("projections", team_id=team, feature=feature, burndown=True)
    if proj and proj["items"]:
        st.caption(f"Month {proj['month']}: day {proj['elapsed_days']:.1f} of {proj['days_in_month']}. "
                   "Run-rate = month-to-date / elapsed days x days in month.")
        table = pd.DataFrame([{
            "scope": f"{i['scope']}: {i['scope_id']}", "status": i["status"],
            "month_to_date": usd(i["month_to_date_usd"]),
            "monthly_limit": usd(i["monthly_limit_usd"]) if i["monthly_limit_usd"] else None,
            "projected (run-rate)": usd(i["projected_usd"]["run_rate"]),
            "projected (7-day avg)": usd(i["projected_usd"]["trailing_7d"]),
            "projected (EWMA)": usd(i["projected_usd"]["ewma"]),
            "projected % of limit": i["projected_pct_of_limit"],
            "exhausted at": i["projected_exhaustion_at"]} for i in proj["items"]])
        st.dataframe(table, hide_index=True, width="stretch")
        with_limits = [i for i in proj["items"] if i.get("burndown")]
        if with_limits:
            choice = st.selectbox("Burn-down", [f"{i['scope']}: {i['scope_id']}" for i in with_limits])
            item = with_limits[[f"{i['scope']}: {i['scope_id']}" for i in with_limits].index(choice)]
            points = pd.DataFrame(item["burndown"])
            long = points.melt(id_vars=["date"], value_vars=["cumulative_usd", "ideal_usd",
                                                             "projected_usd", "limit_usd"],
                               var_name="line", value_name="usd").dropna()
            long["usd"] = long["usd"].map(usd)
            chart = alt.Chart(long).mark_line(point=False).encode(
                x=alt.X("date:T", title="Day (UTC)"), y=alt.Y("usd:Q", title="Cumulative cost (USD)"),
                color=alt.Color("line:N", title=None),
                strokeDash=alt.condition(alt.datum.line == "projected_usd", alt.value([5, 5]),
                                         alt.value([0])),
                tooltip=["date:T", "line:N", alt.Tooltip("usd:Q", format="$.2f")])
            st.altair_chart(chart, width="stretch")
    else:
        empty("No spend or monthly limits this month.")

# ---------------------------------------------------------------- Savings

with tabs[3]:
    savings = api("savings", **window)
    if savings and savings["routed_requests"]:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Baseline (all strongest)", fmt_usd(savings["baseline_cost_usd"]))
        c2.metric("Actual (routed)", fmt_usd(savings["actual_cost_usd"]))
        c3.metric("Gross savings", fmt_usd(savings["gross_savings_usd"]),
                  pct(savings["gross_savings_pct"]))
        c4.metric("Net of verification", fmt_usd(savings["net_savings_usd"]),
                  pct(savings["net_savings_pct"]),
                  help=f"Verification overhead: {fmt_usd(savings['verification_overhead_usd'])}")
        bars = pd.DataFrame([
            {"item": "Baseline", "usd": usd(savings["baseline_cost_usd"])},
            {"item": "Actual", "usd": usd(savings["actual_cost_usd"])},
            {"item": "Actual + verification",
             "usd": usd(savings["actual_cost_usd"]) + usd(savings["verification_overhead_usd"])}])
        st.altair_chart(alt.Chart(bars).mark_bar().encode(
            x=alt.X("usd:Q", title="Cost (USD)"), y=alt.Y("item:N", sort=None, title=None),
            tooltip=[alt.Tooltip("usd:Q", format="$.4f")]), width="stretch")
        left, right = st.columns(2)
        for column, key, label in ((left, "by_feature", "feature"), (right, "by_tier", "tier")):
            frame = pd.DataFrame(savings[key])
            if not frame.empty:
                for c in ("baseline_cost_usd", "actual_cost_usd", "gross_savings_usd"):
                    frame[c] = frame[c].map(usd)
                column.subheader(f"By {label}")
                column.dataframe(frame, hide_index=True, width="stretch")
    else:
        empty("No routed requests in this window.")

# ---------------------------------------------------------------- Routing quality

with tabs[4]:
    quality = api("quality", **window)
    if quality and quality["successful_requests"]:
        v = quality["verification"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Verified answers", f"{v['verified']:,}")
        ci = v["pass_rate_ci95"]
        c2.metric("Pass rate", rate(v["pass_rate"]),
                  help="pass / (pass + fail). Inconclusive and skipped are excluded.")
        c3.metric("95% interval (Wilson)", f"{rate(ci[0])} – {rate(ci[1])}" if ci else "n/a")
        c4.metric("Post-call escalations", f"{quality['escalation']['post_call']:,}",
                  rate(quality["escalation"]["post_call_rate"]))
        left, right = st.columns(2)
        tiers = pd.DataFrame([{"tier": f"tier {t}", "requests": n}
                              for t, n in sorted(quality["tier_distribution"].items())])
        left.subheader("Tier mix")
        left.altair_chart(alt.Chart(tiers).mark_bar().encode(
            x=alt.X("tier:N", title=None), y=alt.Y("requests:Q", title="Requests"),
            tooltip=["tier", "requests"]), width="stretch")
        right.subheader("Pass rate by model (95% CI)")
        by_model = pd.DataFrame([{**m, "low": m["pass_rate_ci95"][0], "high": m["pass_rate_ci95"][1]}
                                 for m in quality["verification_by_model"] if m["judged"]])
        if not by_model.empty:
            base = alt.Chart(by_model).encode(y=alt.Y("model:N", title=None))
            right.altair_chart(
                base.mark_rule().encode(x=alt.X("low:Q", title="Pass rate",
                                                scale=alt.Scale(domain=[0, 1])), x2="high:Q")
                + base.mark_point(filled=True, size=80).encode(
                    x="pass_rate:Q", tooltip=["model", "judged", "pass_rate", "low", "high"]),
                width="stretch")
        else:
            right.info("No verified answers yet. Run the worker: python -m app.worker --once")
        b = quality["budget"]
        st.markdown(f"**Budget events:** {b['downgrades']:,} downgrades · {b['blocks']:,} blocks · "
                    f"{b['overrides']:,} overrides · {quality['escalation']['pre_call']:,} "
                    "pre-call escalations")
        if quality["misses_by_model"] or quality["misses_by_feature"]:
            m1, m2 = st.columns(2)
            m1.dataframe(pd.DataFrame(quality["misses_by_model"].items(),
                                      columns=["cheap model", "routing misses"]),
                         hide_index=True, width="stretch")
            m2.dataframe(pd.DataFrame(quality["misses_by_feature"].items(),
                                      columns=["feature", "routing misses"]),
                         hide_index=True, width="stretch")
    else:
        empty()

# ---------------------------------------------------------------- Performance

with tabs[5]:
    latency = api("latency", **window)
    if latency and latency["by_model"]:
        frame = pd.DataFrame(latency["by_model"])
        long = frame.melt(id_vars=["model"], value_vars=["p50_ms", "p95_ms", "p99_ms"],
                          var_name="percentile", value_name="ms")
        st.subheader("Latency percentiles by model")
        st.altair_chart(alt.Chart(long).mark_bar().encode(
            x=alt.X("model:N", title=None), xOffset="percentile:N",
            y=alt.Y("ms:Q", title="Latency (ms)"), color=alt.Color("percentile:N", title=None),
            tooltip=["model", "percentile", alt.Tooltip("ms:Q", format=".0f")]),
            width="stretch")
        st.caption("Averages hide the tail: p99 is what the slowest 1% of requests wait.")
        st.dataframe(frame, hide_index=True, width="stretch")
    else:
        empty()
    errors = api("errors", **window)
    if errors and errors["by_provider"]:
        st.subheader("Error rate by provider")
        providers = pd.DataFrame(errors["by_provider"])
        providers["error_rate"] = providers["error_rate"].map(rate)
        st.dataframe(providers, hide_index=True, width="stretch")
        if errors["by_error_code"]:
            st.subheader("Failures by code")
            st.dataframe(pd.DataFrame(errors["by_error_code"]), hide_index=True,
                         width="stretch")
