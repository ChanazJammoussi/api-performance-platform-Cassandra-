# Cassandra — Contexte du projet

## Objectif

Cassandra est un **banc de test d'observabilité et de détection d'anomalies** pour systèmes distribués. Il simule une stack microservices réaliste (avec trafic diurnal et pannes injectées) afin de :

1. Générer des métriques RED (Rate / Error / Duration) réalistes via OpenTelemetry.
2. Injecter des fautes contrôlées (latence, erreurs, saturation de pool, lenteur downstream) avec un log de vérité-terrain horodaté.
3. Servir de dataset d'entraînement / d'évaluation pour des algorithmes de détection d'anomalies.

---

## Architecture globale

```
k6 (load)
  │
  ▼
gateway :8000
  ├─► orders :8001 ──► TimescaleDB :5432
  └─► payments :8002 ──► orders :8001

Traces (OTLP gRPC) ──► otel-collector :4317
                              │
                    spanmetrics (traces → métriques RED)
                              │
                    Prometheus remote write ──► Prometheus :9090
                                                       │
                                               Grafana :3000
```

Tous les services tournent via **Docker Compose** (`docker-compose.yml`).

---

## Services

### `gateway` — port 8000
Point d'entrée unique. Proxy pur sans logique métier.

| Endpoint | Proxy vers |
|---|---|
| `GET /api/orders` | `orders /orders` |
| `GET /api/orders/{id}` | `orders /orders/{id}` |
| `POST /api/payments` | `payments /payments` |
| `GET /health` | local |

Pas de métriques OTel propres. Pas de fault injection.

### `orders` — port 8001
Service métier principal. Connecté à TimescaleDB via un pool psycopg2 (`minconn=1, maxconn=5`).

**Métriques OTel publiées (via OTLP gRPC → otel-collector) :**
- `orders.pool_wait_ms` — temps d'attente pour obtenir une connexion DB
- `orders.queue_depth` — profondeur de la file de travail interne (max 10)

**Endpoints métier :**
- `GET /orders` — liste 5 ordres fictifs
- `GET /orders/{id}` — retourne un ordre fictif + délai DB simulé (10–50ms)
- `POST /orders` — crée un ordre fictif

**Endpoints fault injection :**
- `POST /faults/latency_step` `{latency_ms}`
- `POST /faults/latency_creep` `{target_ms, duration_minutes}`
- `POST /faults/error_burst` `{error_rate}` (0.0–1.0)
- `POST /faults/pool_shrink` `{pool_max}`
- `POST /faults/reset`
- `GET /faults/state`

### `payments` — port 8002
Service de paiement. Appelle `orders` pour valider une commande avant de traiter le paiement.

**Métriques OTel :**
- `payments.queue_depth`
- `payments.processing_ms`

**Endpoints métier :**
- `POST /payments` `{order_id}` — valide l'ordre via orders, puis traite le paiement
- `GET /payments/{id}` — retourne un paiement fictif

**Fault injection :** même API que `orders`, plus `downstream_slow` (augmente le timeout vers `orders`).

---

## Fault Injection

### Mécanisme (`fault.py`)
Chaque service (`orders`, `payments`) embarque un `FaultState` thread-safe :

```python
{
  "latency_ms": 0,        # délai ajouté à chaque requête
  "error_rate": 0.0,      # probabilité de renvoyer HTTP 500
  "pool_max": 5,          # taille max du connection pool (orders seulement)
  "downstream_slow": 0,   # délai sur les appels sortants (payments seulement)
}
```

**Types de fautes :**

| Type | Description |
|---|---|
| `latency_step` | Ajoute immédiatement N ms à chaque requête |
| `latency_creep` | Monte graduellement jusqu'à `target_ms` sur `duration_minutes` (30 steps) |
| `error_burst` | Retourne HTTP 500 avec probabilité `error_rate` |
| `pool_shrink` | Réduit `pool_max` du connection pool (simule une saturation DB) |
| `downstream_slow` | Augmente le timeout vers orders (simule un réseau lent) |
| `reset` | Remet tous les paramètres à zéro |

---

## Scenario Runner (`scenario-runner/runner.py`)

CLI Python qui orchestre l'injection de fautes depuis des fichiers YAML.

```bash
python runner.py scenarios/bad_deploy.yaml --output ground_truth.json
```

**Format d'un scénario YAML :**
```yaml
id: mon_scenario
description: "..."
faults:
  - service: orders          # ou payments
    type: latency_step
    target_endpoint: "GET /orders/{order_id}"
    params:
      latency_ms: 500
    duration_seconds: 90     # durée de la faute
    wait_before_seconds: 15  # attente avant injection
```

**Ground truth produite (JSON) :**
```json
{
  "scenario_id": "...",
  "fault_type": "...",
  "target_service": "...",
  "target_endpoint": "...",
  "injected_at": "<ISO UTC>",
  "cleared_at": "<ISO UTC>",
  "magnitude": { ... }
}
```

### Scénarios disponibles

| Fichier | ID | Description |
|---|---|---|
| `quiet_baseline.yaml` | `quiet_baseline` | Aucune faute (mesure faux positifs) |
| `bad_deploy.yaml` | `bad_deploy` | Latency step 500ms sur orders (90s) |
| `latency_step_small.yaml` | `latency_step_small` | Latency step petite magnitude |
| `latency_step_large.yaml` | `latency_step_large` | Latency step 1000ms sur orders (60s) |
| `latency_creep_orders.yaml` | `latency_creep_orders` | Montée graduelle jusqu'à 800ms sur 2 min (simule memory leak) |
| `latency_creep_slow.yaml` | `latency_creep_slow` | Montée graduelle lente |
| `error_burst_low.yaml` | `error_burst_low` | Erreurs faible taux sur payments |
| `error_burst_high.yaml` | `error_burst_high` | Erreurs 90% sur payments (60s) |
| `error_burst_payments.yaml` | `error_burst_payments` | Burst d'erreurs sur payments |
| `pool_shrink_orders.yaml` | `pool_shrink_orders` | Réduction pool sur orders |
| `pool_shrink_heavy.yaml` | `pool_shrink_heavy` | Saturation totale pool=1 (120s) |
| `downstream_slow_payments.yaml` | `downstream_slow_payments` | 600ms extra sur appels payments→orders (90s) |
| `combined_latency_errors.yaml` | `combined_latency_errors` | Latency 400ms orders + erreurs 40% payments en simultané |

---

## Deploy Events API (`detection-service/deploy_api.py`)

Registre de déploiements (control plane) exposé sur le port **8090**. Une CI/CD —
ou le scenario runner pour les démos — enregistre un déploiement, que le
correlator attribue ensuite à une régression.

| Endpoint | Description |
|---|---|
| `POST /deploys` | `{service, version, deployed_at?, metadata?}` → crée un deploy, retourne `deploy_id` |
| `GET /deploys` | liste les déploiements récents (`?service=`, `?since_minutes=`) |
| `GET /health` | liveness |

Stockés dans la table `deploy_events`. Le `correlator.correlate_deploy()` cherche
un déploiement dans la fenêtre causale `[onset - 30min, onset]` (un deploy précède
la régression), privilégie le même service, et écrit `alerts.suspected_deploy_id`.
L'alerte Slack est enrichie de la ligne *Déploiement suspecté: service version*.

Un scénario YAML peut déclarer un bloc `deploy:` (voir `bad_deploy.yaml`) : le
runner l'enregistre via l'API juste avant d'injecter la faute.

## Détection en couches (spec 5.4 / 8.3)

Le détecteur (`detector.py`) combine trois couches par signal `p99_ms` :

| Couche | Fichier | Rôle |
|---|---|---|
| Layer 0 — static | `baseline_utils.ENDPOINT_SLOS` | seuils SLO fixes (baseline de comparaison) |
| Layer 1 — baseline | `baseline_utils` + `endpoint_baseline` | déviation vs bande saisonnière (dow×heure), donne la **direction** |
| Layer 2 — Isolation Forest | `ml_model.py` | anomalie multivariée non supervisée, **gatée par la direction** |

**Features dérivées** (`features.py`, math pure) — toutes **endpoint-relatives** (c'est ce qui
rend viable un seul modèle global) : déviations baseline signées p50/p95/p99, `error_rate_5xx`,
pente Theil-Sen de p99 normalisée, ratio p99/p50, `rps_delta` relatif. Fenêtre glissante de 10
cycles ; la fenêtre la plus récente incomplète est exclue (watermark).

**Direction gating** : la couche ML ne contribue qu'en cas de `degradation` (p99 > p90), jamais
sur une performance anormalement bonne (`improvement`).

**Combinaison** : `combined = baseline_norm + (1 - baseline_norm) * ml_gated`, plancher par le
dépassement SLO dur ; calibrée [0,1] contre la distribution trailing des scores. Déclenche si
dépassement SLO **ou** `combined ≥ 0.6` (seuil tuné, cf. `tune_contamination.py`, §9.2).

**Modèle** (`train_model.py`) : `IsolationForest` global entraîné sur l'historique
`endpoint_features` en **excluant les fenêtres d'injection** (test set, jamais en entraînement).
Artefact versionné par timestamp + pointeur `latest` (volume `models_data`), promotion soumise à
un **sanity-gate** (shift de distribution KS sur une fenêtre de référence fixe). Refit *nightly*
via le service `trainer`. Le `detector` recharge l'artefact quand `latest` change.

**Anomaly store** : la table `anomalies` reçoit un enregistrement par cycle scoré
(`score`, `layer`, `direction`, `contributing_features` top-3) — alimente la timeline Grafana.

**Alerte précoce TTD** (`ttd.py`, spec §8.4, stretch) : quand la tendance p99 est
haussière, extrapolation Theil-Sen vers le SLO → `ttd_minutes` avec intervalle
(dispersion des pentes). **Advisory uniquement** (extrapolation de tendance, jamais
garantie). Exposé dans `contributing_features.ttd`, en ligne Slack et sur un stat Grafana.

**Monitoring du drift** (`model_monitor.py`, service `model-monitor`) : le sanity-gate
compare les distributions à l'entraînement ; ce moniteur surveille la dérive **après
déploiement**. Il compare la distribution des scores ML **live** (`anomalies.ml_norm`,
24 h) à la **référence** figée (`reference_window`) via KS, écrit dans `model_health`
(KS, taux d'anomalie live, flag), et l'expose sur le dashboard self-observabilité
(KS ≥ 0.30 = drift → signal de réentraînement). Boucle toutes les 30 min.

```bash
# Entraîner un modèle manuellement (run-once)
docker compose run --rm trainer python train_model.py
```

## Évaluation layered vs static (spec 9)

`evaluation.py` matche les alertes live (table `alerts`) au ground-truth :
detection rate, FP/h, délai médian, attribution.

`evaluate_layered.py` produit le **livrable phare** (§9.3) : il **rejoue hors-ligne**
les deux détecteurs sur l'historique `endpoint_features` — *static* (seuils SLO,
layer 0) et *layered* (baseline + Isolation Forest + direction gating) — avec la
même logique de score que `detector.py`, applique l'hystérésis, puis matche le
ground-truth. Sortie : table de comparaison par type de faute (detection rate,
délai, lead time) + FP/h. Un onset n'est FP que si aucune injection n'est active
(les cascades ne sont pas des faux positifs).

```bash
# Table de comparaison sur la campagne structuree (defaut)
docker compose run --rm trainer python evaluate_layered.py
docker compose run --rm trainer python evaluate_layered.py --input /data/results/<...> --output rapport.json
# --persist ecrit les resultats dans la table eval_runs (dashboard Grafana d'evaluation)
docker compose run --rm trainer python evaluate_layered.py --persist
```

`explanation_rubric.py` (spec §9.2) score un échantillon d'explications
(`alerts.explanation`) sur une rubrique (structure, chiffres cohérents, actionnabilité
des checks, cadrage de l'incertitude) — proxys objectifs, l'appréciation finale restant
subjective. `docker compose run --rm trainer python explanation_rubric.py`.

`compare_supervised.py` (spec §8.2, stretch) compare un modèle **supervisé** (gradient
boosting sklearn, équivalent XGBoost sans dépendance lourde) au **non-supervisé**
(Isolation Forest) sur un split temporel 50/50 des features étiquetées par le ground-truth.
Résultat mesuré : PR-AUC quasi identique (0.164 vs 0.167) — le supervisé n'apporte pas
d'avantage décisif, ce qui justifie le choix non-supervisé (labels rares, sur-apprentissage
des fautes synthétiques, consommation du jeu de test). Expérience documentée, non shippée.

## Dashboards Grafana (spec 5.9 / 9.3)

Datasource TimescaleDB (provisionnée). Deux dashboards dans
`grafana/provisioning/dashboards/` :

- **Cassandra - API Performance & Alerting** (`cassandra-main-v1`) : santé par endpoint
  (latence p99/p50 + bande baseline saisonnière, erreurs, RPS), table des alertes,
  historique FIRING + explication LLM, **score anomalie par couche** (static/baseline/iforest
  depuis la table `anomalies`), annotations **alertes** (FIRING/RESOLVED) et **déploiements**.
- **Cassandra - Evaluation (layered vs static)** (`cassandra-eval-v1`) : lit `eval_runs`
  (dernière campagne) — detection rate static vs layered par type de faute (barchart + table),
  **courbe de sensibilité** (par magnitude core/stress), FP/h, délais. Alimenté par
  `evaluate_layered.py --persist`.
- **Cassandra - Self-observability** (`cassandra-selfobs-v1`, spec §11) : fraîcheur scraper /
  détecteur (lag alerting visuel par seuils), cadence des cycles (~60s), taux de fallback LLM,
  qualité d'alerte (transitions FIRING 24h, durée FIRING médiane, historique par couche).

## Tests (`detection-service/tests/`)

Suite **pytest** sur la logique pure (sans DB) : features (Theil-Sen, déviations,
direction), TTD, calibration/KS/sanity-gate/attribution ML, baseline_utils (pg_dow,
déviation), scoring de corrélation, et logique d'évaluation (hystérésis, matching,
magnitude). **68 tests**. CI GitHub Actions (`.github/workflows/ci.yml`) à chaque push/PR.

```bash
# Localement (deps ML dans l'image trainer)
docker compose run --rm -v "$PWD/detection-service:/app" trainer \
  sh -c "pip install -q pytest && cd /app && python -m pytest"
```

## Load Testing (`k6/load.js`)

Simule une **journée compressée en 2 heures** avec courbe sinusoïdale (5 à 50 VUs).

**Stages :**
```
0–20min  : montée 5→25 VUs   (matin)
20–60min : montée 25→50 VUs  (midi / pic)
60–80min : maintien 50 VUs   (après-midi)
80–110min: descente 50→15 VUs (soir)
110–120min: descente 15→5 VUs (nuit)
```

**Distribution du trafic :**
- 40% `GET /api/orders`
- 30% `GET /api/orders/1`
- 15% `GET /api/orders/2`
- 15% `POST /api/payments` `{order_id: 1}`

**Seuils :** p95 < 2s, error rate < 10%.

---

## Stack d'observabilité

### OTel Collector (`otel-collector/config.yaml`)
- Reçoit les traces OTLP (gRPC :4317, HTTP :4318)
- **Processor PII** : supprime `http.url`, `http.target`, `net.peer.ip`, `enduser.id`, `user.id`
- **Connector `spanmetrics`** : dérive les métriques RED depuis les traces
  - Histogramme : buckets de 2ms à 15s
  - Dimensions : `http.method`, `http.route`, `http.status_code`
- Exporte vers Prometheus via remote write

### Prometheus (`:9090`)
- Reçoit les métriques via remote write depuis l'OTel Collector
- Scrape aussi l'OTel Collector lui-même (`otel-collector:8888`)

### TimescaleDB (`:5432`)
- PostgreSQL + extension TimescaleDB
- Table `metrics(time, endpoint_id, metric_name, value)` en hypertable
- Credentials : `cassandra / cassandra / cassandra`
- **Compression + rétention** (`init.sql`) sur les hypertables append-only
  `endpoint_features` et `anomalies` : compression des chunks > 7 j
  (segmentby `endpoint_id`), purge des chunks > 90 j. Sans ces politiques les tables
  (~7 200 lignes/jour chacune) croissaient sans limite. Ratio mesuré : **~6×**.

### Grafana (`:3000`)
- Source de données : Prometheus
- Credentials : `admin / admin`

---

## Structure des fichiers

```
cassandra/
├── docker-compose.yml
├── context.md
├── k6/
│   └── load.js                    # script de charge k6
├── otel-collector/
│   └── config.yaml                # pipeline OTel (traces → spanmetrics → prometheus)
├── prometheus/
│   └── prometheus.yml             # scrape config
├── timescaledb/
│   └── init.sql                   # hypertable metrics
├── services/
│   ├── gateway/
│   │   ├── main.py                # proxy FastAPI
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── orders/
│   │   ├── main.py                # service + métriques OTel + fault control API
│   │   ├── fault.py               # FaultState + apply_* helpers
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── payments/
│       ├── main.py                # service + métriques OTel + fault control API
│       ├── fault.py               # même interface que orders
│       ├── Dockerfile
│       └── requirements.txt
└── scenario-runner/
    ├── runner.py                  # CLI d'injection de scénarios
    ├── ground_truth_test.json     # exemple de ground truth
    └── scenarios/
        ├── quiet_baseline.yaml
        ├── bad_deploy.yaml
        ├── latency_step_small.yaml
        ├── latency_step_large.yaml
        ├── latency_creep_orders.yaml
        ├── latency_creep_slow.yaml
        ├── error_burst_low.yaml
        ├── error_burst_high.yaml
        ├── error_burst_payments.yaml
        ├── pool_shrink_orders.yaml
        ├── pool_shrink_heavy.yaml
        ├── downstream_slow_payments.yaml
        └── combined_latency_errors.yaml
```

---

## Démarrage rapide

```bash
# Démarrer toute la stack
docker compose up -d --build

# Lancer la charge k6 (depuis la racine du projet)
k6 run k6/load.js

# Injecter un scénario (services exposés sur localhost)
cd scenario-runner
python runner.py scenarios/bad_deploy.yaml --output ground_truth.json

# Interfaces
# Grafana   : http://localhost:3000  (admin/admin)
# Prometheus: http://localhost:9090
# Gateway   : http://localhost:8000
```
