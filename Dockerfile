# One image for the gateway, the verification worker and the dashboard: they share all code
# and dependencies, so one build serves all three (each compose service just runs a different
# command). Used by `docker compose --profile full up`.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt changes
COPY requirements.txt .
RUN python -m pip install -r requirements.txt

# Then the code (changes often, rebuilds fast)
COPY alembic.ini pyproject.toml ./
COPY app ./app
COPY config ./config
COPY dashboard ./dashboard
COPY migrations ./migrations
COPY scripts ./scripts

# Run as an unprivileged user
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000 8501
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
