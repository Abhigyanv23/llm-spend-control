from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.errors import GatewayError
from app.gateway import Gateway
from app.providers import build_adapters
from app.registry import ModelRegistry
from app.schemas import ChatRequest, ChatResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: one shared HTTP client (connection pooling) + registry + gateway
    client = httpx.AsyncClient(timeout=settings.request_timeout_s)
    registry = ModelRegistry(settings.model_registry_path)
    app.state.registry = registry
    app.state.gateway = Gateway(registry, build_adapters(client, settings))
    yield
    # Shutdown: close pooled connections cleanly
    await client.aclose()


app = FastAPI(title="LLM Spend Control Center", version="0.1.0", lifespan=lifespan)


@app.exception_handler(GatewayError)
async def gateway_error_handler(_: Request, exc: GatewayError):
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models(request: Request):
    return {"models": [m.to_dict() for m in request.app.state.registry.all()]}


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request):
    return await request.app.state.gateway.handle(body)