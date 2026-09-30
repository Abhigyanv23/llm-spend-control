import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.background import BackgroundTask

from app.api import budgets as budgets_api
from app.api import routing as routing_api
from app.api import usage as usage_api
from app.audit import AuditLogger, AuditRecord
from app.budgets import BudgetService, RedisBudgetStore
from app.config import Settings, settings as default_settings
from app.db import create_engine, create_session_factory
from app.errors import GatewayError
from app.gateway import Gateway
from app.providers import build_adapters
from app.registry import ModelRegistry
from app.routing import Router, available_providers, load_routing_config
from app.schemas import ChatRequest, ChatResponse

logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")


def create_redis(url: str, timeout_s: float) -> Redis:
    # Short timeouts: when Redis is down we want a fast fail-open/closed decision,
    # not every request hanging for seconds
    return Redis.from_url(url, decode_responses=True, socket_timeout=timeout_s,
                          socket_connect_timeout=timeout_s)


def create_app(settings: Settings | None = None, *, engine: AsyncEngine | None = None,
               redis_client: Redis | None = None) -> FastAPI:
    """App factory. Production uses the defaults; tests inject SQLite + fakeredis."""
    settings = settings or default_settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: build every long-lived dependency once and wire them together
        registry = ModelRegistry(settings.model_registry_path)
        # Validate routing config BEFORE opening connections: a typo fails startup immediately
        routing_config = load_routing_config(settings.routing_config_path,
                                             settings.routing_profile, registry)
        providers = available_providers(settings)
        router = Router(registry, routing_config, providers)
        logger.info("Routing profile '%s' loaded; available providers: %s",
                    routing_config.profile, ", ".join(sorted(providers)))

        client = httpx.AsyncClient(timeout=settings.request_timeout_s)
        db_engine = engine or create_engine(settings.database_url)
        redis = redis_client or create_redis(settings.redis_url, settings.redis_timeout_s)
        session_factory = create_session_factory(db_engine)

        store = RedisBudgetStore(redis)
        budgets = BudgetService(store, session_factory,
                                warn_threshold=settings.budget_warn_threshold,
                                fail_mode=settings.budget_fail_mode)
        app.state.settings = settings
        app.state.registry = registry
        app.state.router = router
        app.state.engine = db_engine
        app.state.redis = redis
        app.state.session_factory = session_factory
        app.state.budgets = budgets
        app.state.audit = AuditLogger(session_factory)
        app.state.gateway = Gateway(registry, build_adapters(client, settings), budgets, router)

        try:
            await store.load_scripts()
        except Exception as exc:
            logger.error("Could not preload Redis Lua scripts (%s: %s)", type(exc).__name__, exc)
        if settings.reconcile_on_startup:
            try:
                summary = await budgets.reconcile(datetime.now(UTC))
                logger.info("Budget counters reconciled from Postgres: %s", summary)
            except Exception as exc:
                # Don't refuse to start: fail mode decides per request what happens
                logger.error("Startup reconciliation failed (%s: %s). Counters may be stale.",
                             type(exc).__name__, exc)
        yield
        # Shutdown: close only what we created
        await client.aclose()
        if redis_client is None:
            await redis.aclose()
        if engine is None:
            await db_engine.dispose()

    app = FastAPI(title="LLM Spend Control Center", version="0.3.0", lifespan=lifespan)
    app.include_router(usage_api.router)
    app.include_router(budgets_api.router)
    app.include_router(routing_api.router)

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError):
        # Failed and blocked requests are audited too. BackgroundTasks injected into the
        # route are discarded when it raises, so the audit write rides on THIS response.
        record: AuditRecord | None = getattr(exc, "audit_record", None)
        task = BackgroundTask(request.app.state.audit.write, record) if record else None
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict(), background=task)

    @app.exception_handler(SQLAlchemyError)
    @app.exception_handler(OSError)
    async def database_error_handler(request: Request, exc: Exception):
        # e.g. /v1/usage while Postgres is down: a structured 503, not a bare 500
        logger.error("Database unavailable on %s: %s: %s", request.url.path,
                     type(exc).__name__, exc)
        err = GatewayError(f"Database unavailable ({type(exc).__name__})", status_code=503,
                           code="database_unavailable", retryable=True)
        return JSONResponse(status_code=err.status_code, content=err.to_dict())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        response = await request_validation_exception_handler(request, exc)
        if request.url.path == "/v1/chat":
            response.background = BackgroundTask(request.app.state.audit.write,
                                                 _validation_record(exc.body))
        return response

    @app.get("/health")
    async def health(request: Request):
        """Liveness plus dependency status. Always 200 while the process is up."""
        checks = {}
        try:
            async with request.app.state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception as exc:
            checks["postgres"] = f"down ({type(exc).__name__})"
        try:
            await request.app.state.redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:
            checks["redis"] = f"down ({type(exc).__name__})"
        status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
        return {"status": status, **checks,
                "budget_fail_mode": request.app.state.settings.budget_fail_mode,
                "routing_profile": request.app.state.router.config.profile}

    @app.get("/v1/models")
    async def list_models(request: Request):
        return {"models": [m.to_dict() for m in request.app.state.registry.all()]}

    @app.post("/v1/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest, request: Request, response: Response,
                   background_tasks: BackgroundTasks,
                   x_budget_override: str | None = Header(
                       default=None, description="Reason for exceeding a budget "
                       "(honoured for high/critical priority only)")):
        result = await request.app.state.gateway.handle(body, override_reason=x_budget_override)
        # Off the critical path: the client gets its answer before the INSERT runs
        background_tasks.add_task(request.app.state.audit.write, result.audit)
        for name, value in result.headers.items():
            response.headers[name] = _header_safe(value)
        return result.response

    return app


def _validation_record(body) -> AuditRecord:
    """Best-effort audit row for a body that failed Pydantic validation."""
    data = body if isinstance(body, dict) else {}

    def text_field(name: str, default: str | None, length: int) -> str | None:
        value = data.get(name)
        return str(value)[:length] if value not in (None, "") else default

    return AuditRecord(request_id=str(uuid.uuid4()), created_at=datetime.now(UTC),
                       team_id=text_field("team_id", "unknown", 64),
                       feature=text_field("feature", "unknown", 64),
                       priority=text_field("priority", "normal", 16),
                       model=text_field("model", None, 128),
                       status="validation_error", error_code="request_validation")


def _header_safe(value: str) -> str:
    # HTTP header values must be latin-1; team/feature ids are ASCII by validation anyway
    return value.encode("ascii", "replace").decode("ascii")


app = create_app()