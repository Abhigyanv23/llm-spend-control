from fastapi import APIRouter, Request

from app.money import usd_str
from app.schemas import ChatRequest
from app.tokens import estimate_input_tokens

router = APIRouter(prefix="/v1", tags=["routing"])


@router.get("/routing")
async def routing_config(request: Request):
    """The active routing policy and which providers are currently usable."""
    r = request.app.state.router
    return {**r.config.to_dict(), "available_providers": sorted(r.available_providers)}


@router.post("/route/preview")
async def route_preview(body: ChatRequest, request: Request):
    """Dry run: which model WOULD serve this request, and why. No provider call, no budget
    reservation, no audit row. Use it to tune config/routing.yaml."""
    r = request.app.state.router
    decision = r.route(body)
    tokens = estimate_input_tokens(body.messages)
    chosen, baseline = r.registry.get(decision.model), r.baseline
    return {
        "routing": decision.metadata(),
        "estimated_input_tokens": tokens,
        "worst_case_cost_usd": usd_str(chosen.worst_case_cost(tokens, body.max_tokens)),
        "baseline_model": baseline.name,
        "baseline_worst_case_cost_usd": usd_str(baseline.worst_case_cost(tokens, body.max_tokens)),
    }