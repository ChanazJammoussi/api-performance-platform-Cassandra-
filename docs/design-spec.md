# Cassandra v2 — AI-Powered API Performance Analysis Platform

> **Codename:** Cassandra · **Status:** Design (v2.0) · **Type:** Technical design document (summer internship project) · **Last updated:** 2026-06-10
>
> This file is the **authoritative design reference** for the project. It is a faithful
> transcription of `cassandra-Chanaz-Jammoussi-design-spec.pdf`. Any implementation
> MUST conform to it. Where the current codebase diverges, the divergence is either a
> deliberate, documented adaptation (see `CLAUDE.md`) or a bug to fix — never a silent
> reinterpretation of the spec.

**Changelog (v2.0):** reframes the project from "predict degradation before it occurs" to
"detect, attribute, and explain API performance degradation, with early warning as a stretch
goal". Removes DTW clustering, per-cluster models, the supervised TTD regressor, Kafka,
multi-tenant, and gRPC. Adds the fault-injection demo environment as a core component and
evaluation methodology, replaces the supervised detector with an unsupervised Isolation Forest,
and adds an LLM explanation layer.

---

## 1. Summary

Cassandra is an observability platform that **detects** API endpoint performance degradation,
**attributes** it to deployment events, and **explains** it in plain language.

Instead of static threshold alerting, the system learns the normal behavioral signature of each
endpoint (a **seasonal baseline** conditioned on day-of-week and hour) and combines it with
**unsupervised anomaly detection** over engineered features. When a degradation is detected, a
correlation engine links it to recent deployment events, and an LLM explanation layer generates a
human-readable incident summary delivered to Slack: what degraded, by how much relative to the
expected baseline, which deploy is suspected, and what to check first.

The project is evaluated rigorously through a purpose-built demo environment with controlled
fault injection. **Every injected fault is a ground-truth label**, which makes detection rate,
false positive rate, and lead time directly measurable. The headline deliverable is a working
platform **and a quantified evaluation against a naive static-threshold baseline**.

Early warning (time-to-degradation estimation) is a **stretch goal** via robust trend
extrapolation, not a supervised model — supervised TTD needs months of real incident history that
does not exist in the internship timeframe. Stated honestly as future work.

## 2. Goals and non-goals

### 2.1 Goals
- Detect latency and error-rate degradation per endpoint with **materially fewer false positives
  than static thresholding, and prove it with numbers**.
- Attribute each detected degradation to a probable **deployment-level cause** (release, feature
  flag, migration, config change) via time-window correlation, with an **honest imputation score**.
- Generate plain-language, actionable incident explanations using an LLM, **triggered only on
  confirmed alerts**.
- Integrate via **OpenTelemetry** with no proprietary instrumentation.
- Provide a **reproducible evaluation methodology** based on injected faults with known ground truth.

### 2.2 Non-goals
- **No prediction guarantee.** Early warning is a stretch goal; core claim is fast, explained,
  attributed detection.
- No full APM replacement (no CPU profiling, no code-level RCA).
- No automatic remediation. The system alerts and explains, it does not act.
- No multi-tenancy, no horizontal scale beyond a single TimescaleDB instance.
- No support for untraced workloads (opaque batch jobs without spans).
- No custom frontend. Dashboards are Grafana.

## 3. System overview

Seven stages:
1. **Traffic generation** — demo microservices under synthetic load, injectable failure modes
   (dev/eval only; in prod the platform points at real services).
2. **Ingestion** — OTLP traces to an OpenTelemetry Collector; `spanmetrics` connector generates
   RED metrics over the full traffic.
3. **Feature computation** — TimescaleDB continuous aggregates maintain per-endpoint feature
   windows and the seasonal baseline.
4. **Detection** — a layered detector (baseline deviation + Isolation Forest) produces an anomaly
   score per endpoint per window.
5. **Correlation** — detected degradations matched against recent deployment events within a
   causal window.
6. **Explanation** — an LLM assembles the anomaly context into a plain-language incident summary.
7. **Notification** — structured alerts with the explanation pushed to Slack; state and history
   exposed via a REST API and Grafana.

```
Demo env: k6 load ─┐                 FaultInjection API ─┐
                   ▼                                     ▼
              Demo microservices ──OTLP spans──► OTel Collector + spanmetrics
                                                          │ RED metrics
                                                          ▼
                                                    TimescaleDB ──cont. agg──► Feature windows + seasonal baseline
                                                          │                              │
                                                          │                              ▼
                                                          │                        Detection Service ──► Anomaly Store
                                                          │                              │                    │
                        Deploy Events API ──► Correlation Engine ◄─────────────────────┘                    │
                                                          │                                                   │
                                                          ▼                                                   ▼
                                              LLM Explanation Engine ──► Alerting & API ──► Slack        Grafana
```

## 4. Demo environment and fault injection harness

Core component, not a side artifact. Purposes: development traffic, end-of-project live demo, and
**source of ground-truth labels** for evaluation.

### 4.1 Demo microservices
3–4 small services with realistic call topology (e.g. `gateway -> orders -> inventory`,
`gateway -> payments -> bank-stub`), each OTel-instrumented, templated routes so `http.route` is
populated. Include dependencies to make saturation realistic: a **connection pool** to a small
PostgreSQL and an **internal work queue**.

### 4.2 Load generator
k6 scenarios producing **diurnal-shaped traffic** (compressed: a full "day" of seasonality replayed
over a configurable wall-clock period, e.g. 2 hours) so the seasonal baseline has structure to learn
within dev timescales. Steady load, ramp-ups, realistic per-endpoint skew.

### 4.3 Fault injection
Each service exposes an **internal-only fault control API** (never traced as user traffic).

| Fault | Shape | Simulates |
|---|---|---|
| `latency_creep` | gradual ramp of added latency over N minutes | memory leak, cache decay, slow resource exhaustion |
| `latency_step` | immediate fixed added latency | bad deploy, downstream regression |
| `pool_shrink` | reduce DB connection pool size | saturation under load |
| `error_burst` | inject 5xx at configurable rate | dependency failure, bad code path |
| `downstream_slow` | slow a dependency service | cascading degradation |
| `bad_deploy` | POST a deploy event to Cassandra, then trigger `latency_step` or `error_burst` after a short delay | the end-to-end attribution scenario |

### 4.4 Scenario runner
A CLI executing a declarative scenario file (YAML): sequence of faults with timing, magnitude, and
target endpoint. Records exact injection windows to a **ground-truth log** — the labels file consumed
by the evaluation pipeline (§9). A standard suite of **~20 scenarios** covering all fault types at
several magnitudes is versioned with the repo.

## 5. Detailed architecture

### 5.1 Ingestion and metrics generation
OTel Collector (gateway) receives OTLP spans. `spanmetrics` generates RED metrics from spans
**over the full traffic before any sampling**. Tail sampling keeps 100% error/slow spans + a
probabilistic fraction of the rest for debugging only; sampled spans are **not** a data dependency of
any detection feature.

Two decisions resolved up front:
- **Histogram strategy:** `spanmetrics` configured with **exponential histograms** (or a tuned
  explicit bucket layout per latency regime). Percentiles from coarse default buckets are too noisy
  to derive slope features from — treated as a **correctness requirement**, not tuning.
- **Cardinality control:** `endpoint_id = method + http.route` (templated). An attribute processor
  drops/rewrites spans lacking `http.route` to a **quarantine dimension**, plus a **hard cap** on
  tracked endpoint cardinality. Raw-URL cardinality explosion is the primary operational failure mode
  and is handled **at ingestion**, not downstream.
- **PII redaction:** attribute filter strips span attributes not on an **allowlist** before export.

Collector exports directly to TimescaleDB (Prometheus Remote Write or OTLP exporter path). **No
message bus.** Collector queue/retry are the only buffering; queue saturation is monitored (§11).

### 5.2 Feature computation (continuous aggregates)
Feature windows via TimescaleDB continuous aggregates, refreshed incrementally:
- **1-minute aggregate per endpoint:** p50/p95/p99 latency, 4xx rate, 5xx rate, RPS.
- **5-minute aggregate per endpoint:** same metrics, smoother, used for slope and ratio features.
- **Derived features, computed at read time by the detection service** from the aggregates:
  **latency slope** (robust linear fit over the last K windows), **p99/p50 ratio** (distribution
  spread signal), **short-window deltas**, and the **baseline deviation features** (§5.3).

**Freshness contract:** aggregate refresh lag ≤ 1 min; detection cadence 1 min; alert pipeline
≤ 30 s; target **under 3 minutes event-to-Slack**. A **watermark rule excludes the most recent
incomplete window** from scoring (never act on partial data).

**Saturation features** (`pool_wait_ms`, queue depth) require application metrics beyond spans. Demo
services export them via the OTel metrics SDK. These features are **optional inputs** — present only
when the monitored application exposes them; the detector treats them as optional.

### 5.3 Seasonal baseline
For each endpoint and each core metric, a continuous aggregate maintains **conditional quantiles
(p10, p50, p90) keyed on `(day_of_week, hour_bucket)`** over a sliding multi-week window. In the
demo the "week" is the compressed seasonality cycle of the load generator.

Outputs per (endpoint, metric, window):
- **expected** (conditional p50) and **band** (p10..p90),
- **`baseline_deviation`:** observed value's position relative to the band, **normalized (0 inside
  the band, scaled distance outside)**.

**Cold start:** an endpoint whose (day × hour) bucket has insufficient samples falls back to its own
all-hours quantiles, then to global per-metric quantiles, until enough history accumulates. No
clustering layer needed.

### 5.4 Detection service
A **FastAPI** service running detection on a **1-minute cadence**. Layered design, each layer usable
and demoable on its own:

- **Layer 0 — static thresholds:** classic SLO checks (e.g. p99 above X ms sustained N windows).
  The comparison baseline for evaluation and the v0 alerting path.
- **Layer 1 — baseline deviation:** rule-based scoring on `baseline_deviation` (sustained excursions
  beyond the band). Seasonal-aware, zero training. **Provides direction and a floor.**
- **Layer 2 — Isolation Forest:** unsupervised scikit-learn `IsolationForest` over the feature
  vector (**baseline deviations across metrics, latency slope, p99/p50 ratio, 5xx rate, RPS delta,
  optional saturation features**). **One global model**, retrained **nightly** on trailing feature
  history with standard contamination parameterization. Unsupervised detection eliminates the
  labeled-incident scarcity problem entirely; **injected faults are reserved as the test set, never
  training data.** Provides sensitivity to multivariate drift.

**Final anomaly score = calibrated combination of layers 1 and 2**, with **layer 2 score GATED BY
layer 1 DIRECTION** — the system only alerts on **degradation**, not on anomalously good
performance. Score, per-feature contributions (feature deviations for layer 1, feature-level anomaly
attribution for layer 2), and **layer provenance** are written to the anomaly store.

A documented comparison of layer 2 vs a supervised alternative (XGBoost on a held-out half of
injected-fault history) is a **stretch** deliverable (§13/stretch).

### 5.5 Alert state machine
Detections flap; alerts must not. Per `(endpoint, signal)`:
```
OK        -> PENDING     score above threshold for 1 window
PENDING   -> FIRING      sustained for M consecutive windows (hysteresis up)
FIRING    -> RESOLVING   score below clear-threshold (lower than fire-threshold)
RESOLVING -> OK          sustained clear for R windows (hysteresis down)
any       -> SILENCED    manual or scheduled silence window
```
**Dedup key:** `(endpoint_id, signal_type)`. A FIRING alert **updates in place** (severity, score,
attribution) rather than re-notifying; escalation re-notifies only on **severity increase**.
Fire/clear thresholds and M/R are configuration with sane defaults.

**Only the `OK -> FIRING` transition triggers the correlation engine and the LLM explanation**
(cost and noise control).

### 5.6 Correlation engine
Receives deployment events (CI/CD webhooks, feature-flag annotations, manual POST) via the API. On
a new FIRING alert, searches deployment events affecting the alerting service within a **causal
window (default: 30 minutes before anomaly onset**, where onset is the **first PENDING window**).

**Imputation score** is a **transparent function of temporal proximity and event-kind priors** (a
release immediately preceding onset scores higher than a config change 25 min prior). Always exposed
as a **suspicion, never asserted as cause**. Trace-topology propagation is explicit future work.

### 5.7 LLM explanation engine
Triggered **once per `OK -> FIRING` transition**. Context assembler builds a structured payload:
- endpoint, metric(s) in violation, observed vs expected baseline values and deviation magnitude,
- **top contributing features** from the detector,
- suspected deploy event(s) with imputation score, commit SHA, deploy kind,
- recent alert history for the same endpoint (recurrence signal).

Rendered into a **fixed prompt template** asking for: a two-sentence summary, the most likely cause
framed with the imputation score's uncertainty, and **two or three concrete first checks**. Output
contract is **JSON `(summary, suspected_cause, checks[])`, validated before use**; on validation
failure or API error the alert **falls back to a deterministic template** so alerting never depends on
LLM availability. Calls go through a thin **provider-agnostic client (Anthropic API first)**. Token
cost bounded by construction: explanations fire only on alert transitions, never per prediction.

### 5.8 Alerting and API layer
REST API (FastAPI, OpenAPI-documented) exposing **anomalies, alerts, deployments, baselines**. Slack
via **Block Kit** messages with the LLM summary, key numbers, suspected deploy, and a Grafana deep
link. Per-channel routing by service.

### 5.9 Dashboards
Grafana on TimescaleDB: per-endpoint latency/error panels overlaid with the **seasonal expected
band**, anomaly-score timeline, alert annotations, deploy-event annotations, and the **evaluation
dashboard** (§9). No custom frontend.

## 6. Data model

### 6.1 Feature window (continuous aggregate)
`endpoint_id text (METHOD + templated route)`, `window_start timestamptz`, `p50_ms/p95_ms/p99_ms
float`, `error_rate_4xx/error_rate_5xx float`, `rps float`, `pool_wait_ms float nullable (optional
saturation signal)`.

### 6.2 Seasonal baseline (continuous aggregate)
`endpoint_id text`, `metric text`, `dow int`, `hour_bucket int`, `p10/p50/p90 float`,
`sample_count int (cold-start fallback decision)`.

### 6.3 Anomaly record  *(the "anomaly store")*
| Field | Type | Description |
|---|---|---|
| `endpoint_id` | text | endpoint |
| `detected_at` | timestamptz | detection timestamp |
| `window_start` | timestamptz | scored window |
| `score` | float | **combined anomaly score [0,1]** |
| `layer` | enum | **`static`, `baseline`, `iforest`, `combined`** |
| `direction` | enum | **`degradation`, `improvement`** |
| `contributing_features` | jsonb | **top feature deviations (top 3)** |

### 6.4 Alert
`alert_id uuid`, `endpoint_id/signal_type text (dedup key)`, `state enum (ok, pending, firing,
resolving, silenced)`, `opened_at/resolved_at timestamptz`, `severity enum (warning, critical)`,
`suspected_deploy_id text nullable`, `imputation_score float nullable [0,1]`, `explanation jsonb
(LLM output summary/suspected_cause/checks, or fallback template)`.

### 6.5 Deployment event
`deploy_id text`, `service_id text`, `commit_sha text`, `deployed_at timestamptz`, `kind enum
(release, feature_flag, migration, config)`.

### 6.6 Ground-truth injection log (evaluation only)
`scenario_id text`, `fault_type enum (§4.3)`, `target_endpoint text`, `injected_at/cleared_at
timestamptz`, `magnitude jsonb`.

## 7. API specification

- **`GET /v1/endpoints/{id}/anomaly`** — latest anomaly assessment:
  ```json
  {
    "endpoint_id": "GET /orders/{id}",
    "detected_at": "2026-08-20T14:32:00Z",
    "score": 0.87,
    "direction": "degradation",
    "contributing_features": {
      "baseline_deviation_p99": 0.52,
      "latency_slope": 0.28,
      "error_rate_5xx": 0.11
    },
    "baseline": { "metric": "p99_ms", "observed": 412.0, "expected": 130.0, "band": [95.0, 180.0] }
  }
  ```
- **`GET /v1/alerts`** — active/historical alerts, filterable by service, state, severity, time
  range. Each embeds the §6.4 record incl. explanation and suspected deploy.
- **`POST /v1/deployments`** — registers a deployment event (called by CI/CD). Body per §6.5 minus
  `deploy_id`; returns 201 with created `deploy_id`.
- **`POST /v1/alerts/{id}/silence`** — silences an alert for a duration.
- **Internal: `POST /faults/{type}`** — fault injection control (demo services only), not part of
  the platform API surface.

## 8. AI approach

### 8.1 Problem framing
**Unsupervised anomaly detection over engineered tabular features per (endpoint, window)**, with
seasonality handled **explicitly by the conditional baseline** rather than learned implicitly. The
sequential aspect is carried by **feature engineering (slopes, deltas, rolling statistics)** rather
than sequence models — preserving the interpretability required by `contributing_features` and the
Slack explanation. Deliberate consequence: **no dependency on labeled incidents for training**;
labeled data (injected faults) is reserved entirely for evaluation.

### 8.2 Why not supervised, stated explicitly
Sustained SLO breaches are rare; no multi-month incident corpus in an internship; training on
injected faults would consume the test set and overfit synthetic shapes. The supervised comparison
(§13/stretch) exists to demonstrate awareness and measure the gap, **not to ship it**.

### 8.3 Model details
- **Isolation Forest (scikit-learn):** **one global model** over all endpoints' feature vectors;
  features are already **endpoint-relative** (baseline deviations, ratios, slopes) — which is what
  makes a global model viable without per-endpoint or per-cluster training. **Nightly refit** on
  trailing history; **model artifact versioned with a timestamp, previous artifact retained**, and a
  **sanity gate (score-distribution shift check on a fixed reference window) before promotion**.
- **Score combination:** layer 1 (baseline rules) provides **direction and a floor**; layer 2
  provides **sensitivity to multivariate drift**. Combined score **calibrated to [0,1] against the
  trailing score distribution**.
- **Attribution:** per-feature contributions from baseline deviations directly, and from the
  Isolation Forest via **per-feature path-depth attribution**; **top three exposed**.

### 8.4 Stretch: early-warning TTD heuristic
For endpoints in PENDING/FIRING with a positive latency trend, a **robust linear fit (Theil-Sen)**
over the recent p99 series is extrapolated to the SLO threshold, yielding `ttd_minutes` with an
interval. **Advisory only**, clearly labeled as trend extrapolation. No supervised TTD in this
version.

## 9. Evaluation methodology
Central claim is **relative**: fewer false positives and earlier detection than static thresholding.

### 9.1 Protocol
1. Run the standard scenario suite (~20, §4.4) against the full platform with **both the layer-0
   static detector and the layered detector active in parallel**.
2. Match alerts to ground-truth injection windows: a **true positive** is a FIRING alert on the
   target endpoint overlapping (or within a grace period after) the injection window; alerts outside
   any injection window are **false positives**.
3. Repeat at multiple fault magnitudes to produce **sensitivity curves**.

### 9.2 Metrics
- **Detection rate** per fault type and magnitude.
- **False positives per hour** under fault-free load (quiet scenarios).
- **Median detection delay** (injection start → FIRING) and, for gradual faults, **lead time vs
  static threshold** (how much earlier the layered detector fires than layer 0).
- **Attribution accuracy** on `bad_deploy`: fraction where the suspected deploy is the injected one.
- **Explanation quality:** small rubric-scored sample (correctness of cited numbers, actionability of
  checks), acknowledging subjectivity.

### 9.3 Reporting
All metrics in a Grafana evaluation dashboard + final report. The **comparison table (layered vs
static across fault types)** is the headline artifact.

## 10. Technical stack
Demo services **Python FastAPI**; load **k6**; instrumentation **OpenTelemetry SDK**; ingestion
**OTel Collector + spanmetrics**; storage/features/baseline **TimescaleDB (hypertables + continuous
aggregates)**; detection ML **scikit-learn (IsolationForest, preprocessing pipelines)**; stretch
comparison **XGBoost**; serving **FastAPI**; LLM **Anthropic API via thin provider-agnostic client**
(structured JSON + deterministic fallback); dashboards **Grafana**; alerting **Slack (Block Kit)**;
orchestration **Docker Compose** (entire platform reproducible with one command).

Removed vs v1.3: Kafka, Redis, ClickHouse, tslearn/DTW, StatsForecast, gRPC, PagerDuty, multi-tenant.

## 11. Self-observability
- Collector queue size, drop count, effective sampling rate, endpoint cardinality gauge (vs hard cap).
- Continuous aggregate refresh lag (alert if it threatens the §5.2 freshness contract).
- Detection service latency, error rate, cadence adherence.
- LLM call latency, failure rate, fallback rate, token spend.
- Rolling alert-quality panel: alerts/day, FIRING durations, silence usage.

## 12. Security
- PII redaction by attribute allowlist at the collector (§5.1); no payload data ingested.
- API authenticated with scoped API keys; deployments endpoint uses a dedicated CI key.
- Fault injection API bound to the internal network only, never on the platform API.
- Secrets (Slack, LLM API key) via environment injection, never in the repo.

## 13. Risks and mitigations
| Risk | Impact | Mitigation |
|---|---|---|
| Compressed seasonality unrealistic | baseline quality overstated | document compression factor; period-agnostic baseline keys; validate on ≥1 slow real-time run |
| Isolation Forest sensitivity to traffic-shape novelty (not degradation) | false positives on legitimate traffic changes | **direction gating by layer 1**; RPS-delta as a feature; contamination tuning on quiet scenarios |
| Endpoint cardinality explosion | pipeline overload | collector-level templating enforcement, quarantine dimension, hard cap with alert |
| Aggregate refresh / ingestion lag | stale detection | freshness watermark, lag alerting, budget in §5.2 |
| LLM unavailability or malformed output | alert pipeline stalls | strict JSON validation + deterministic fallback; alerting never blocks on LLM |
| Wrong deploy imputation | false root cause communicated | score always exposed as suspicion with magnitude; never phrased as certainty |
| Scope creep into stretch goals | core unfinished | **hard gate: stretch items start only after §9 evaluation runs end to end** |

## 14. Timeline
Sequential phases, ~52 core days. Each phase ends with a demoable checkpoint.

- **Phase 1 — Demo environment and ingestion (10 d).** Demo services, k6 diurnal load, fault
  injection framework, scenario runner + ground-truth logging, OTel Collector (spanmetrics,
  exponential histograms, cardinality + PII processors, TimescaleDB export). *Checkpoint: RED metrics
  from injected faults visible in raw hypertables.*
- **Phase 2 — Features, baseline, alerting v0 (10 d).** Continuous aggregates (1 min & 5 min),
  seasonal baseline + cold-start fallback, static threshold detector (layer 0), alert state machine
  (states, hysteresis, dedup, silence), Slack notifications (deterministic template), end-to-end v0
  test. *Checkpoint: a fault injection produces a deduplicated Slack alert.*
- **Phase 3 — ML detection and evaluation (11 d).**
  - Read-time derived features (robust slope, ratios, deltas, baseline deviation vector) — §5.2/5.3 — 2 d
  - Layer 1 baseline-deviation detector + **direction gating** — §5.4 — 1 d
  - Isolation Forest: **training job, artifact versioning, sanity gate, inference integration** — §5.4/8.3 — 3 d
  - **Score combination and calibration** — §8.3 — 1 d
  - Evaluation pipeline: scenario suite execution, ground-truth matching, metric computation — §9.1/9.2 — 3 d
  - Threshold and contamination tuning on quiet scenarios — §9.2 — 1 d
  - *Checkpoint: first comparison table, layered detector vs static thresholds.*
- **Phase 4 — Deployment correlation (7 d).** Deploy events API + storage, correlation engine
  (causal window, imputation scoring), bad_deploy end-to-end + attribution accuracy, alert enrichment
  (API + Slack). *Checkpoint: alert names the injected commit within minutes.*
- **Phase 5 — LLM explanations and dashboards (8 d).** Context assembler, prompt template + JSON
  contract + validation + fallback, LLM client + cost/latency instrumentation, Grafana dashboards
  (baseline band, alert/deploy annotations), Grafana evaluation dashboard. *Checkpoint: full demo
  script runs end to end with LLM-written Slack alerts.*
- **Phase 6 — Evaluation campaign and hardening (6 d).** Full campaign across magnitudes + sensitivity
  curves, explanation quality rubric, self-observability panels + lag alerts, security pass, demo
  rehearsal. *Checkpoint: final numbers frozen; demo reproducible from a clean `docker compose up`.*
- **Stretch (only after Phase 6).** TTD trend-extrapolation heuristic (Theil-Sen, advisory, Slack
  surfacing) — §8.4 — 3 d. Supervised XGBoost comparison on held-out injected-fault history — §8.2 — 2 d.

## 15. Open questions
- **Compression factor** for synthetic seasonality — resolve empirically in Phase 2.
- **Default SLO thresholds** per endpoint — derive from clean-load distributions (e.g. p99 of clean
  traffic × margin) or fix manually? Decide before Phase 3 evaluation.
- **Imputation prior weights per deploy kind** — uniform vs opinionated defaults
  (release > migration > config > flag)? Revisit after first attribution measurements.

---

## Décisions d'implémentation (résout §15)

> Décisions prises pendant l'implémentation. Ne modifie pas la transcription du spec ci-dessus ;
> fige les choix réellement retenus (utile pour la Phase 3 et le rapport de stage).

- **Seuils SLO par endpoint (§15, §5.4) → fixés manuellement.** Définis en dur par endpoint dans
  `detection-service/baseline_utils.py` (`ENDPOINT_SLOS`, avec `DEFAULT_SLOS` en repli). Choix
  assumé pour la reproductibilité de l'évaluation : des seuils dérivés du trafic propre bougeraient
  d'un run à l'autre. À réviser si l'évaluation §9 montre des seuils mal calibrés.
- **Facteur de compression saisonnière (§15, §4.2) → une « journée » rejouée en 2 h.** Courbe
  sinusoïdale de `k6/load.js` (5→50 VUs). Les clés de baseline (`dow`, `hour_bucket`) restent
  period-agnostic, donc réutilisables sur un run temps-réel lent (mitigation §13).
- **Priors d'imputation par `kind` de déploiement (§15, §5.6) → uniformes pour l'instant.** La table
  `deploy_events` n'a pas encore de colonne `kind` ; la corrélation n'utilise que la proximité
  temporelle (+ bonus service). Les priors par kind seront ajoutés quand `kind` sera introduit,
  après les premières mesures d'attribution.

