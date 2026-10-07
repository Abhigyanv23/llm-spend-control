"""Analytics layer (Phase 5): pure, read-only query functions over the audit trail.

No HTTP here: the API (app/api/analytics.py) validates parameters, caches, and serialises.
"""
from app.analytics.cache import TTLCache
from app.analytics.common import (
    AnalyticsFilter,
    WindowError,
    make_filter,
    percentile_cont,
    wilson_interval,
)
from app.analytics.projections import ewma, projections
from app.analytics.quality import latency_by_model, routing_quality, savings
from app.analytics.spend import (
    cost_by_model,
    error_breakdown,
    spend_timeseries,
    top_patterns,
    top_requests,
)
from app.analytics.summary import summary

__all__ = [
    "TTLCache", "AnalyticsFilter", "WindowError", "make_filter", "percentile_cont",
    "wilson_interval", "ewma", "projections", "latency_by_model", "routing_quality", "savings",
    "cost_by_model", "error_breakdown", "spend_timeseries", "top_patterns", "top_requests",
    "summary",
]
