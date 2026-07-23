# Cassandra — plateforme d'analyse de performance API pilotée par l'IA

Cassandra **détecte**, **attribue** et **explique** les dégradations de performance des
endpoints API. Au lieu d'un seuillage statique, la plateforme apprend la signature normale de
chaque endpoint (baseline saisonnière dow×heure), la combine avec une détection d'anomalies
non supervisée (Isolation Forest), corrèle chaque dégradation à un déploiement récent, et
génère une explication en langage naturel poussée sur Slack.

Le tout est évalué rigoureusement via un banc de **fault injection** : chaque faute injectée
est un label de vérité-terrain, ce qui rend le taux de détection, les faux positifs et le délai
directement mesurables — et comparables à un détecteur à seuils statiques.

## Démarrage rapide

```bash
docker compose up -d --build          # toute la stack
k6 run k6/load.js                     # trafic (journée compressée en 2 h)

# Démo bout-en-bout scriptée (trafic → deploy → injection → détection → attribution)
bash scripts/demo.sh

# Injecter un scénario manuellement (depuis scenario-runner/)
python runner.py scenarios/bad_deploy.yaml

# Interfaces
#   Grafana    http://localhost:3000  (admin/admin)
#   Prometheus http://localhost:9090
#   Deploy API http://localhost:8090/docs
```

## Détection en couches

| Couche | Rôle |
|---|---|
| **Layer 0 — static** | Seuils SLO (baseline de comparaison) |
| **Layer 1 — baseline** | Déviation vs bande saisonnière → **direction** |
| **Layer 2 — Isolation Forest** | Anomalie multivariée, **gatée par la direction** |

Score combiné calibré [0,1] ; déclenche sur dépassement SLO **ou** score combiné ≥ 0.6 (tuné).
State machine anti-flapping (OK→PENDING→FIRING→RESOLVING). Voir
[**docs/architecture.md**](docs/architecture.md) pour les diagrammes.

## Résultats mesurés (campagne d'évaluation)

| Métrique | Static | Layered |
|---|---|---|
| Detection rate | 65 % | **71 %** |
| Faux positifs / heure | 1.13 | 1.29 |

- Qualité des explications LLM : **5.00 / 6**
- Supervisé (gradient boosting) vs non-supervisé : **PR-AUC égale** → justifie le non-supervisé
- Alerte précoce **TTD** (extrapolation Theil-Sen, advisory)

## Documentation

| Document | Contenu |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Diagrammes (flux, couches, state machine, séquence, ML, données) |
| [`docs/design-spec.md`](docs/design-spec.md) | Spec de conception (référence contraignante) + décisions |
| [`docs/security.md`](docs/security.md) | Audit de sécurité (§12) |
| [`context.md`](context.md) | Documentation technique détaillée |
| [`CLAUDE.md`](CLAUDE.md) | Conventions + contrat d'implémentation |

## Tests

```bash
docker compose run --rm -v "$PWD/detection-service:/app" trainer \
  sh -c "pip install -q pytest && cd /app && python -m pytest"
```

68 tests unitaires (logique pure) ; CI GitHub Actions à chaque push/PR.

## Stack

Python (FastAPI) · k6 · OpenTelemetry Collector (spanmetrics) · Prometheus · TimescaleDB ·
scikit-learn (Isolation Forest) · Gemini (explications LLM) · Grafana · Slack · Docker Compose.
