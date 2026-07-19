# CLAUDE.md — Cassandra project instructions

## Authoritative design reference

**`docs/design-spec.md` is the binding design spec** (transcribed from
`cassandra-Chanaz-Jammoussi-design-spec.pdf`). Read it before implementing any phase.
Any code MUST conform to it. If code and spec disagree, either the code is a **documented
deliberate adaptation** (listed below) or it is a bug — never silently reinterpret the spec.
When a task is ambiguous, the spec wins; if the spec is silent, ask.

## Deliberate adaptations (current code vs spec — do NOT "fix" these blindly)

- **Feature computation:** `detection-service/scraper.py` polls Prometheus every 60 s into the
  `endpoint_features` hypertable, instead of TimescaleDB **continuous aggregates** (§5.2). Same
  data shape (p50/p95/p99, 4xx/5xx, rps per endpoint per minute). Baseline (`baseline_job.py` →
  `endpoint_baseline`) matches §5.3/6.2 (dow × hour, p10/p50/p90, cold-start fallback).
- **LLM provider:** `explainer.py` uses **Gemini (`google-genai`)**, not the Anthropic client of
  §10. The provider-agnostic contract (strict JSON `summary`/`suspected_cause`/`checks`, deterministic
  fallback) still holds. Do not swap providers unless asked.
- **Serving:** the detector is a plain 60 s loop (`detector.py`), not a FastAPI service. Only
  `deploy_api.py` (deploy events, port 8090) is FastAPI so far.
- **DB port:** host-mapped **5434** → container 5432. Default `DATABASE_URL` in code targets
  `localhost:5434`; in containers it is overridden to `timescaledb:5432` (see `docker-compose.yml`).
- **`deploy_events`:** `deploy_id` is UUID (spec §6.5 says text); no `kind` column yet, so
  correlation uses temporal proximity only (no event-kind priors §5.6 yet).

## Phase 3 implementation contract (ML detection) — MUST follow

The spec parts that constrain Phase 3 are §5.2, §5.3, §5.4, §6.3, §8.1, §8.3. Non-negotiables:

1. **Endpoint-relative features only.** The Isolation Forest is **one global model** over all
   endpoints — this is only valid because features are endpoint-relative. Use **baseline deviations
   per metric** (p50/p95/p99 + 5xx), **latency slope** (robust Theil-Sen), **p99/p50 ratio**,
   **short-window deltas**, **RPS delta**, optional saturation (absent here). **Never** feed raw
   absolute ms levels (`p99_ms` in ms) as model features — that breaks the global model.
2. **Direction gating.** Compute a `direction ∈ {degradation, improvement}` from layer 1 (observed
   vs seasonal band). **Layer 2 (iforest) contributes to an alert ONLY when direction = degradation.**
   Never alert on anomalously *good* performance.
3. **Injected faults are the TEST SET, never training data.** The training job MUST exclude
   ground-truth injection windows (`scenario-runner/results/*.json`) from the trailing history it
   fits on. Training on injected faults is a correctness violation.
4. **Score combination = calibrated blend of layer 1 & layer 2**, calibrated to **[0,1] against the
   trailing score distribution**. Layer 1 provides direction + a floor; layer 2 adds multivariate
   sensitivity.
5. **Artifact lifecycle:** model artifact **versioned by timestamp**, **previous artifact retained**,
   a `latest` pointer for the detector to load. Promotion is gated by a **sanity gate: score-
   distribution shift check on a fixed reference window** (reject promotion if the new model's score
   distribution on the reference window drifts beyond a threshold). Retrain cadence: nightly.
6. **Anomaly store (§6.3).** Add an `anomalies` table: `endpoint_id, detected_at, window_start,
   score float[0,1], layer enum(static|baseline|iforest|combined), direction enum(degradation|
   improvement), contributing_features jsonb`. Write one record per scored (endpoint, window).
7. **Attribution:** expose **top-3** contributing features. For the iforest, use **per-feature
   path-depth attribution**; for baseline, the raw normalized deviations.
8. **Watermark (§5.2):** exclude the most recent incomplete window from scoring.

Keep each layer independently demoable (§5.4). Do not regress the existing static/baseline alerting
or the state machine, correlation, deploy, or explanation wiring already in `detector.py`.

## Working conventions

- **Comments and docs in French** (accent-free ASCII in code comments, matching existing files).
- **Best-effort wiring:** correlation / deploy / LLM / ML steps must never block or crash the alert
  path — wrap in try/except and log, as the existing `detector.py` does.
- **Env-parametrized:** every service reads `DATABASE_URL` / `PROMETHEUS_URL` / etc. from the
  environment with localhost defaults, so it runs both on the host and in a container.
- **Do not run a host `detector.py` and the `detector` container at the same time** (double writes).
- **Tests:** the `detection-service/venv` is a broken Linux venv on Windows — run Python tests inside
  the `cassandra-detector` image with the source mounted, or via `docker exec` against the live DB.

## Git / commits

- Author is **Chanaz Jammoussi** `<chanazjammoussi@outlook.com>`. **Never** commit as Claude or as
  Yassine. **Never** add a `Co-Authored-By: Claude` trailer.
- Commit and push **only when asked**. Commit messages in French.
