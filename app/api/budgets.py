"""Budget policy management and live status. No auth yet (known limitation)."""
from datetime import UTC, datetime

from fastapi import APIRouter, Path, Request

from app.budgets.policies import list_policies, upsert_policy
from app.schemas import (ID_MAX_LENGTH, ID_PATTERN, BudgetPolicyIn, BudgetPolicyOut,
                         BudgetScope, BudgetStatusOut)

router = APIRouter(prefix="/v1/budgets", tags=["budgets"])

ScopeId = Path(min_length=1, max_length=ID_MAX_LENGTH, pattern=ID_PATTERN)


@router.get("")
async def get_policies(request: Request) -> dict:
    async with request.app.state.session_factory() as session:
        policies = await list_policies(session)
    return {"policies": [BudgetPolicyOut.model_validate(p) for p in policies]}


@router.put("/{scope}/{scope_id}", response_model=BudgetPolicyOut)
async def put_policy(scope: BudgetScope, body: BudgetPolicyIn, request: Request,
                     scope_id: str = ScopeId):
    async with request.app.state.session_factory() as session:
        policy = await upsert_policy(session, scope.value, scope_id, body.daily_limit_usd,
                                     body.monthly_limit_usd, body.enabled)
        await session.commit()
    # Takes effect on the next request: policies are read per request, counters already exist
    return BudgetPolicyOut.model_validate(policy)


@router.get("/{scope}/{scope_id}/status", response_model=BudgetStatusOut)
async def get_status(scope: BudgetScope, request: Request, scope_id: str = ScopeId):
    return await request.app.state.budgets.status(scope.value, scope_id, datetime.now(UTC))


@router.post("/reconcile")
async def reconcile(request: Request) -> dict:
    """Rebuild Redis counters from request_logs (same as the startup reconciliation).
    Safe when no requests are in flight: it resets all holds to 0."""
    return await request.app.state.budgets.reconcile(datetime.now(UTC))
