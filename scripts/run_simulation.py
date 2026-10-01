"""Run the labelled workload through the full pipeline in three experiment modes (Phase 6).

    python scripts/run_simulation.py                       # modes A, B, C on all 1,000 prompts
    python scripts/run_simulation.py --modes C --sample-rate 0.25
    python scripts/run_simulation.py --sweep 0.05,0.1,0.25,0.5   # extra C runs per sample rate
    python scripts/run_simulation.py --real --limit 30     # SMALL real-provider run (costs money)

Modes, all on the SAME dataset:
  A  baseline      every request explicitly sent to the strongest model (no routing)
  B  routing only  routing on; async verification and synchronous escalation off
  C  full system   routing + verification (worker drained at the end) + escalation

Each mode gets its OWN server process with a pinned config (a per-mode quality.yaml saved in
the report folder), its own team ids (sim-<run>-<mode>-<team>) so budgets don't interfere, and
a tight budget on the marketing team so warnings, downgrades and blocks appear. Requests are
sent by an async client with bounded concurrency, a token-bucket rate limit, and retries with
exponential backoff + jitter on retryable errors. Results go to reports/<run>/results.jsonl.gz;
scripts/build_report.py turns them into the report without needing the server.
"""
import argparse
import asyncio
import gzip
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATASET = ROOT / "data" / "workload.jsonl"
REPORTS = ROOT / "reports"
RETRYABLE_STATUS = {429, 502, 503, 504}


# ---------------------------------------------------------------- rate limiting

class TokenBucket:
    """Allows `rate` requests per second on average, with bursts up to `capacity`."""

    def __init__(self, rate: float, capacity: float | None = None):
        self.rate = rate
        self.capacity = capacity or max(1.0, rate)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


def backoff(attempt: int, base: float = 0.2, cap: float = 5.0) -> float:
    """Exponential backoff with full jitter: spreads retries so clients don't stampede."""
    return random.uniform(0, min(cap, base * 2 ** attempt))


# ---------------------------------------------------------------- server per mode

class ModeServer:
    """Starts `uvicorn app.main:app` with mode-specific environment and stops it afterwards."""

    def __init__(self, env: dict, port: int, log_path: Path):
        self.env, self.port, self.log_path = env, port, log_path
        self.proc: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.log = self.log_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(self.port)],
            cwd=ROOT, env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early; see {self.log_path}")
            try:
                if httpx.get(f"{self.url}/health", timeout=2).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"server did not become healthy; see {self.log_path}")

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()


# ---------------------------------------------------------------- one request

def body_for(record: dict, team_id: str, model: str | None) -> dict:
    body = {"team_id": team_id, "feature": record["feature"], "priority": record["priority"],
            "max_tokens": record["max_tokens"], "messages": record["messages"]}
    if model:
        body["model"] = model
    return body


def summarise_response(resp: httpx.Response) -> dict:
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code != 200:
        error = data.get("error", {}) if isinstance(data.get("error"), dict) else {}
        return {"status": "error", "error_code": error.get("code") or f"http_{resp.status_code}",
                "request_id": error.get("request_id"), "cost_usd": "0"}
    meta = data.get("metadata", {})
    routing = meta.get("routing") or {}
    escalation = meta.get("escalation") or {}
    attempts = escalation.get("attempts") or []
    classifier = routing.get("classifier") or {}
    return {
        "status": "ok", "error_code": None, "request_id": data["request_id"],
        "model": data["model"], "served_tier": routing.get("final_tier"),
        "decision_tier": routing.get("tier"), "classifier_tier": classifier.get("tier"),
        "classifier_confidence": classifier.get("confidence"),
        "route_source": routing.get("source"),
        "pre_escalated": bool((escalation.get("pre_call") or {}).get("applied")),
        "escalated": bool(escalation.get("escalated")),
        "first_check_failed": attempts[0].get("check_failed") if attempts else None,
        "final_check_failed": escalation.get("final_check_failed"),
        "downgraded": bool(routing.get("downgrades")),
        "budget_status": (meta.get("budget") or {}).get("status"),
        "cost_usd": data["cost_usd"], "baseline_cost_usd": routing.get("baseline_cost_usd"),
        "latency_ms_server": data.get("latency_ms"),
        "sampled": bool(((meta.get("quality") or {}).get("sampling") or {}).get("sampled")),
    }


async def send(client: httpx.AsyncClient, bucket: TokenBucket, sem: asyncio.Semaphore,
               record: dict, team_id: str, model: str | None, max_retries: int) -> dict:
    async with sem:
        for attempt in range(max_retries + 1):
            await bucket.acquire()
            start = time.perf_counter()
            try:
                resp = await client.post("/v1/chat", json=body_for(record, team_id, model))
                retryable = resp.status_code in RETRYABLE_STATUS and (
                    resp.json().get("error", {}).get("retryable", True)
                    if resp.headers.get("content-type", "").startswith("application/json") else True)
            except httpx.HTTPError as exc:
                resp, retryable = None, True
                error = type(exc).__name__
            latency = round((time.perf_counter() - start) * 1000, 2)
            if retryable and attempt < max_retries:
                await asyncio.sleep(backoff(attempt))
                continue
            result = summarise_response(resp) if resp is not None else {
                "status": "error", "error_code": f"client_{error}", "cost_usd": "0"}
            return {**result, "http_status": resp.status_code if resp is not None else None,
                    "latency_ms_client": latency, "client_attempts": attempt + 1}
    raise AssertionError("unreachable")


# ---------------------------------------------------------------- modes

def mode_quality_config(base: dict, mode: str, sample_rate: float, verifier_team: str) -> dict:
    cfg = json.loads(json.dumps(base))                      # deep copy
    cfg["escalation"]["enabled"] = mode == "C"
    if mode == "C":
        cfg["sampling"]["base_rate"] = sample_rate
        cfg["sampling"]["low_confidence_rate"] = max(sample_rate, min(1.0, sample_rate * 5))
    cfg["verification"]["budget"]["team_id"] = verifier_team
    return cfg


async def run_mode(args, run_id: str, run_dir: Path, records: list[dict], mode: str, label: str,
                   sample_rate: float, port: int) -> tuple[list[dict], dict]:
    base_quality = yaml.safe_load((ROOT / "config" / "quality.yaml").read_text(encoding="utf-8"))
    verifier_team = f"sim-{run_id}-{label}-verifier"
    quality_path = run_dir / "config" / f"quality-{label}.yaml"
    quality_path.parent.mkdir(parents=True, exist_ok=True)
    quality_path.write_text(yaml.safe_dump(mode_quality_config(base_quality, mode, sample_rate,
                                                               verifier_team)), encoding="utf-8")
    env = {**os.environ, "QUALITY_CONFIG_PATH": str(quality_path),
           "VERIFY_ENABLED": "true" if mode == "C" else "false",
           "ROUTING_PROFILE": "production" if args.real else "dev",
           "ANALYTICS_CACHE_TTL_S": "0"}
    env["ROUTING_CONFIG_PATH"] = str(pinned_routing_config(args, run_dir))
    team = lambda t: f"sim-{run_id}-{label}-{t}"                       # noqa: E731

    with ModeServer(env, port, run_dir / f"server-{label}.log") as server:
        async with httpx.AsyncClient(base_url=server.url, timeout=120) as client:
            routing = (await client.get("/v1/routing")).json()
            baseline_model = routing["baseline_model"]
            # Isolation check: a FEATURE budget applies to every team, so a pre-existing policy
            # on a workload feature would make results depend on unrelated traffic
            features = {r["feature"] for r in records}
            policies = (await client.get("/v1/budgets")).json()["policies"]
            interfering = [f"{p['scope']}:{p['scope_id']}" for p in policies
                           if p["enabled"] and p["scope"] == "feature" and p["scope_id"] in features]
            if interfering:
                print(f"  WARNING: budget policies on workload features affect this run: "
                      f"{interfering}", flush=True)
            # A tight budget on one team so warnings, downgrades and blocks appear
            await client.put(f"/v1/budgets/team/{team('marketing')}",
                             json={"daily_limit_usd": str(args.tight_budget)})
            if args.real:                                              # hard spend cap
                for t in {r["team_id"] for r in records}:
                    await client.put(f"/v1/budgets/team/{team(t)}",
                                     json={"daily_limit_usd": str(args.real_cap_per_team)})
                await client.put(f"/v1/budgets/team/{verifier_team}",
                                 json={"daily_limit_usd": str(args.real_cap_per_team)})
            bucket, sem = TokenBucket(args.rate), asyncio.Semaphore(args.concurrency)
            model = baseline_model if mode == "A" else None
            started = time.perf_counter()
            done = 0

            async def one(record):
                nonlocal done
                result = await send(client, bucket, sem, record, team(record["team_id"]), model,
                                    args.retries)
                done += 1
                if done % 100 == 0 or done == len(records):
                    print(f"  [{label}] {done}/{len(records)} "
                          f"({done / (time.perf_counter() - started):.0f} req/s)", flush=True)
                return {"run_id": run_id, "mode": mode, "label": label,
                        "sample_rate": sample_rate if mode == "C" else None,
                        "id": record["id"], "category": record["category"],
                        "true_tier": record["true_tier"], "split": record["split"],
                        "tags": record["tags"], "team_id": record["team_id"],
                        "feature": record["feature"], "priority": record["priority"], **result}

            results = await asyncio.gather(*(one(r) for r in records))
            elapsed = time.perf_counter() - started

        verification = {}
        if mode == "C":
            verification = await drain_and_collect(env, run_dir, label, results)
    return results, {"label": label, "mode": mode, "sample_rate": sample_rate if mode == "C" else None,
                     "baseline_model": baseline_model, "requests": len(results),
                     "elapsed_s": round(elapsed, 2), "verifier_team": verifier_team,
                     "interfering_policies": interfering,
                     "quality_config": str(quality_path.relative_to(ROOT)), **verification}


async def drain_and_collect(env: dict, run_dir: Path, label: str, results: list[dict]) -> dict:
    """Run the worker until this mode's sampled jobs are verified, then attach verdicts."""
    wanted = {r["request_id"] for r in results if r.get("sampled")}
    log = run_dir / f"worker-{label}.log"
    verdicts: dict[str, dict] = {}
    for _ in range(5):
        with log.open("a", encoding="utf-8") as fh:
            # In a thread: a blocking subprocess call must not freeze the event loop
            await asyncio.to_thread(
                subprocess.run, [sys.executable, "-m", "app.worker", "--once", "--concurrency", "8"],
                cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=900, check=False)
        verdicts = await fetch_verdicts(sorted(wanted))
        if len(verdicts) >= len(wanted):
            break
        await asyncio.sleep(2)
    total = Decimal(0)
    for r in results:
        v = verdicts.get(r.get("request_id"))
        r["verdict"] = v["verdict"] if v else None
        r["verification_cost_usd"] = v["cost"] if v else "0"
        total += Decimal(r["verification_cost_usd"])
    return {"sampled": len(wanted), "verified": len(verdicts),
            "verification_cost_usd": str(total)}


async def fetch_verdicts(request_ids: list[str]) -> dict[str, dict]:
    from sqlalchemy import select

    from app.config import Settings
    from app.db import Verification, create_engine, create_session_factory
    if not request_ids:
        return {}
    engine = create_engine(Settings().database_url)
    try:
        async with create_session_factory(engine)() as s:
            rows = (await s.scalars(select(Verification).where(
                Verification.request_id.in_([uuid.UUID(r) for r in request_ids])))).all()
        return {str(v.request_id): {"verdict": v.verdict, "cost": str(v.verification_cost_usd)}
                for v in rows}
    finally:
        await engine.dispose()


# ---------------------------------------------------------------- main

def pinned_routing_config(args, run_dir: Path) -> Path:
    """A copy of the routing policy saved with the run (optionally with another classifier
    version), so the exact routing rules used can always be inspected and re-used."""
    source = Path(args.routing_config) if args.routing_config else ROOT / "config" / "routing.yaml"
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    if args.classifier:
        data.setdefault("classifier", {})["version"] = args.classifier
    target = run_dir / "config" / "routing.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return target


def git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def load_records(args) -> list[dict]:
    records = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines()]
    if args.split != "all":
        records = [r for r in records if r["split"] == args.split]
    if args.real:
        records = [r for r in records if "simulated_failure" not in r["tags"]]
    return records[:args.limit] if args.limit else records


async def main_async(args) -> int:
    if args.real and not (os.environ.get("ANTHROPIC_API_KEY") or _env_file_has("ANTHROPIC_API_KEY")):
        print("--real needs ANTHROPIC_API_KEY (and ideally OPENAI_API_KEY): refusing to start.")
        return 2
    run_id = args.run_id or uuid.uuid4().hex[:6]
    run_dir = REPORTS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    records = load_records(args)
    plan = [(m, m, args.sample_rate) for m in args.modes]
    plan += [("C", f"C{rate:g}", rate) for rate in args.sweep]
    print(f"Run {run_id}: {len(records)} prompts, modes {[p[1] for p in plan]}"
          + (" (REAL providers)" if args.real else " (dev profile, mock models)"))

    all_results, modes = [], []
    for i, (mode, label, rate) in enumerate(plan):
        print(f"- mode {label}")
        results, info = await run_mode(args, run_id, run_dir, records, mode, label, rate,
                                       args.port + i)
        all_results += results
        modes.append(info)
        print(f"  done in {info['elapsed_s']} s"
              + (f"; verified {info.get('verified')}/{info.get('sampled')}" if mode == "C" else ""))

    # gzip: 6,000 result rows are ~5 MB as text, ~0.5 MB compressed (small enough to commit)
    with gzip.open(run_dir / "results.jsonl.gz", "wt", encoding="utf-8", newline="\n") as fh:
        for row in all_results:
            fh.write(json.dumps(row) + "\n")
    run_info = {"run_id": run_id, "created_at": datetime.now(UTC).isoformat(),
                "git_commit": git_commit(), "real_providers": args.real,
                "dataset": str(DATASET.relative_to(ROOT)),
                "dataset_sha256": hashlib.sha256(DATASET.read_bytes()).hexdigest(),
                "split": args.split, "prompts": len(records), "tight_budget_usd": str(args.tight_budget),
                "concurrency": args.concurrency, "rate_per_s": args.rate,
                "routing_config": args.routing_config or "config/routing.yaml",
                "classifier": args.classifier or "(routing.yaml default)", "modes": modes}
    (run_dir / "run.json").write_text(json.dumps(run_info, indent=2), encoding="utf-8")
    print(f"\nResults: {run_dir / 'results.jsonl.gz'}\nNext:    python scripts/build_report.py {run_id}")
    return 0


def _env_file_has(key: str) -> bool:
    env = ROOT / ".env"
    return env.exists() and any(line.startswith(f"{key}=") and line.strip() != f"{key}="
                                for line in env.read_text(encoding="utf-8").splitlines())


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Run the labelled workload in experiment modes A/B/C.")
    p.add_argument("--modes", type=lambda s: [m.strip().upper() for m in s.split(",") if m.strip()],
                   default=["A", "B", "C"])
    p.add_argument("--sample-rate", type=float, default=0.1,
                   help="mode C base sample rate (low-confidence rate = 5x, max 1)")
    p.add_argument("--sweep", type=lambda s: [float(x) for x in s.split(",") if x],
                   default=[], help="extra mode-C runs, one per sample rate")
    p.add_argument("--split", choices=["all", "train", "test"], default="all")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--concurrency", type=int, default=16)
    p.add_argument("--rate", type=float, default=100.0, help="requests per second (token bucket)")
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--tight-budget", type=Decimal, default=Decimal("0.03"),
                   help="daily limit (USD) for the marketing team in every mode")
    p.add_argument("--routing-config", default=None, help="alternative routing.yaml for this run")
    p.add_argument("--classifier", choices=["rules-v1", "rules-v2"], default=None,
                   help="override classifier.version for this run (pinned copy in the run folder)")
    p.add_argument("--port", type=int, default=8010)
    p.add_argument("--run-id", default=None)
    p.add_argument("--real", action="store_true",
                   help="production profile, real providers: use with --limit 30")
    p.add_argument("--real-cap-per-team", type=Decimal, default=Decimal("0.10"))
    args = p.parse_args(argv)
    for m in args.modes:
        if m not in ("A", "B", "C"):
            p.error(f"unknown mode {m}")
    return args


def main(argv=None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
