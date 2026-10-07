import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

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
from app.api import analytics as analytics_api
from app.api import quality as quality_api
from app.api import routing as routing_api
from app.api import usage as usage_api
from app.analytics import TTLCache
from app.audit import AuditLogger, AuditRecord
from app.bootstrap import build_core
from app.config import Settings, settings as default_settings
from app.errors import GatewayError
from app.gateway import Gateway
from app.schemas import ChatRequest, ChatResponse

logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")


def create_app(settings: Settings | None = None, *, engine: AsyncEngine | None = None,
               redis_client: Redis | None = None) -> FastAPI:
    """App factory. Production uses the defaults; tests inject SQLite + fakeredis."""
    settings = settings or default_settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: the same composition root as the worker (app/bootstrap.py), so both
        # processes are wired identically. Config is validated first (fail fast).
        core = build_core(settings, engine=engine, redis_client=redis_client)
        app.state.core = core
        app.state.settings = settings
        app.state.registry = core.registry
        app.state.router = core.router
        app.state.quality_config = core.quality_config
        app.state.engine = core.engine
        app.state.redis = core.redis
        app.state.session_factory = core.session_factory
        app.state.budgets = core.budgets
        app.state.queue = core.queue
        app.state.audit = AuditLogger(core.session_factory)
        app.state.analytics_cache = TTLCache(ttl_s=settings.analytics_cache_ttl_s)
        app.state.gateway = Gateway(core.registry, core.adapters, core.budgets, core.router,
                                    quality=core.quality_config)

        try:
            await core.store.load_scripts()
        except Exception as exc:
            logger.error("Could not preload Redis Lua scripts (%s: %s)", type(exc).__name__, exc)
        if settings.reconcile_on_startup:
            try:
                summary = await core.budgets.reconcile(datetime.now(UTC))
                logger.info("Budget counters reconciled from Postgres: %s", summary)
            except Exception as exc:
                # Don't refuse to start: fail mode decides per request what happens
                logger.error("Startup reconciliation failed (%s: %s). Counters may be stale.",
                             type(exc).__name__, exc)
        yield
        # Shutdown: close only what we created
        await core.aclose()

    app = FastAPI(title="LLM Spend Control Center", version="0.5.0", lifespan=lifespan)
    app.include_router(usage_api.router)
    app.include_router(budgets_api.router)
    app.include_router(routing_api.router)
    app.include_router(quality_api.router)
    app.include_router(analytics_api.router)

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
        if result.verification_job is not None:
            # Also off the critical path, and best-effort: enqueue_safe never raises
            background_tasks.add_task(request.app.state.queue.enqueue_safe,
                                      result.verification_job)
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