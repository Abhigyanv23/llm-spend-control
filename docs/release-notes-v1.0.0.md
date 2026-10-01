# v1.0.0: LLM Spend Control Center

A routing and budgeting gateway in front of LLM providers, built in six phases.

**Headline (simulation, 1,000 labelled prompts, mock models priced like real tiers):** spend reduced
by **40.3% net of verification** (63.7% gross) with a **97.4% verification pass rate**
(95% CI 93.4–99.0%) and **98.0% of requests served at or above their required tier**.
See [the case study](docs/case-study.md) for method and caveats.

## What's in it

| Phase | Version | Highlights |
|---|---|---|
| 1 | — | Unified `/v1/chat` gateway; adapters for OpenAI, Anthropic, Ollama and a mock; normalised errors |
| 2 | 0.2.0 | PostgreSQL audit log; exact money (`Decimal` / `NUMERIC` / nano-dollars); per-team/feature budgets with atomic **reserve-then-settle** in Redis (Lua); `402` blocks and audited overrides; reconciliation |
| 3 | 0.3.0 | 3-tier complexity routing with explainable decisions; per-feature rules; budget-aware downgrade; savings vs the strongest model |
| 4 | 0.4.0 | Async verification via Redis Streams + worker (at-least-once + idempotency); similarity and LLM judges; routing misses; synchronous cascade; quality API |
| 5 | 0.5.0 | Analytics columns + backfill; analytics API (projections, burn-down, gross vs net savings, Wilson intervals, latency percentiles); Streamlit dashboard |
| 6 | 1.0.0 | 1,000-prompt labelled workload; A/B/C simulation and reproducible reports; classifier `rules-v2` (held-out 83.4% → 92.4%); Docker compose profile; CI; case study |

## Upgrading from 0.5.0

```powershell
git pull
python -m pip install -r requirements.txt        # adds matplotlib, ruff
python -m alembic upgrade head                   # no new migration in 1.0.0 (still 0003)
```

- **Default classifier is now `rules-v2`.** Set `classifier.version: rules-v1` in
  `config/routing.yaml` to keep the old behaviour.
- **Verification spend is reconciled to the budget recorded on each verification.** Older
  verifications (no record) are no longer attributed to the current verifier team, so the
  `quality-verifier` counter may drop after the first restart: this corrects an over-count.
- New: `docker compose --profile full up -d --build` runs the whole system in containers.

## Breaking changes since 0.1 (summary)
- Money fields are fixed-point JSON strings (0.2.0).
- `team_id` / `feature` must match `^[A-Za-z0-9._-]+$`, max 64 (0.2.0).
- Requests without `model` are routed (0.3.0).
- `VERIFY_ENABLED=true` requires a usable tier-3 model at startup (0.4.0).

## Known limitations
No authentication; mock-model quality is simulated; synthetic self-labelled dataset; analytics on
the transactional database; single-instance reconciliation. Details in each phase's notes.
