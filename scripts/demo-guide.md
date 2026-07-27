# Guide de démonstration Cassandra — de 0 à l'alerte

Objectif : montrer la chaîne complète en ~20 min.  
Chaque étape indique ce qu'on capture en screenshot.

---

## Prérequis (1 min)

```powershell
# Vérifier que Docker Desktop tourne
docker version

# Se placer dans le projet
cd C:\Users\chana\cassandra
```

---

## Étape 1 — Démarrer la stack (3 min)

```powershell
docker compose up -d --build
```

Attendre ~30 s puis vérifier les **13 services** :

```powershell
docker compose ps
```

**Screenshot 1** : tous les services Up dans le terminal.

```
NAME                        STATUS    PORTS
cassandra-timescaledb       Up        5434->5432
cassandra-prometheus        Up        9090->9090
cassandra-otel-collector    Up        4317->4317, 4318->4318, 8888->8888
cassandra-grafana           Up        3000->3000
cassandra-detector          Up        9101->9101
cassandra-scraper           Up
cassandra-baseline          Up
cassandra-trainer           Up
cassandra-model-monitor     Up
cassandra-deploy-api        Up        8090->8090
cassandra-gateway           Up        8000->8000
cassandra-orders            Up        8001->8001
cassandra-payments          Up        8002->8002
```

---

## Étape 2 — Vérifier la DB initiale (30 s)

```powershell
docker exec cassandra-timescaledb psql -U cassandra -d cassandra -c `
  "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;"
```

**Screenshot 2** : les 9 tables —
`alerts`, `alerts_history`, `anomalies`, `deploy_events`, `endpoint_baseline`,
`endpoint_features`, `endpoint_relationships`, `eval_runs`, `model_health`.

---

## Étape 3 — Générer du trafic de fond (5 min de préchauffage)

Ouvrir un second terminal et lancer k6 :

```powershell
docker run --rm --name cassandra-k6 --network cassandra_default `
  -e BASE_URL=http://gateway:8000 `
  grafana/k6 run - <<'JS'
import http from 'k6/http'; import { sleep } from 'k6';
export const options = { vus: 15, duration: '15m' };
export default function () {
  http.get(`${__ENV.BASE_URL}/api/orders/1`);
  http.get(`${__ENV.BASE_URL}/api/orders`);
  sleep(0.3 + Math.random() * 0.3);
}
JS
```

Pendant la préchauffage (~2 min), passer à l'étape suivante en parallèle.

---

## Étape 4 — Grafana : baseline et trafic normal

Ouvrir http://localhost:3000 (admin / admin).

- Dashboard **"Cassandra — API Performance"** → latence p50/p95/p99 visibles
- Dashboard **"Cassandra — Self-Observability"** → cycles/s, endpoints scorés

**Screenshot 3** : Grafana — trafic normal, pas d'alerte.

---

## Étape 5 — Prometheus /metrics du détecteur (30 s)

Ouvrir http://localhost:9101/metrics dans le navigateur.

**Screenshot 4** : métriques natives — `cassandra_detector_cycle_seconds_bucket`, `cassandra_alerts_total{state="ok"}`.

---

## Étape 6 — API de déploiements (swagger)

Ouvrir http://localhost:8090/docs.

**Screenshot 5** : Swagger UI POST /deploys, GET /deploys, GET /health.

---

## Étape 7 — Scénario bad_deploy (la vraie démo)

Revenir au premier terminal. Attendre que la préchauffage k6 soit à ≥2 min, puis :

### Option A — script automatique (tout-en-un)

```bash
bash scripts/demo.sh
```

### Option B — contrôle manuel étape par étape

**Étape 7a : enregistrer le déploiement**

```powershell
curl -s -X POST http://localhost:8090/deploys `
  -H "Content-Type: application/json" `
  -d '{"service":"orders","version":"v2.5.0-demo","metadata":{"commit":"abc1234","author":"chanaz"}}'
```

**Screenshot 6** : réponse `{"deploy_id": "...", "service": "orders", "version": "v2.5.0-demo"}`.

**Étape 7b : injecter la régression (+700ms sur orders)**

```powershell
$DEMO_START = docker exec cassandra-timescaledb psql -U cassandra -d cassandra -t -c "SELECT now()" |
  ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
echo "Fenetre de detection : $DEMO_START"

curl -s -X POST http://localhost:8001/faults/latency_step `
  -H "Content-Type: application/json" `
  -d '{"latency_ms": 700}'
```

**Screenshot 7** : appel curl `{"applied": "latency_step", "latency_ms": 700}` + timestamp de début.

---

## Étape 8 — Observer la détection en temps réel (~3 min)

Surveiller les logs du détecteur dans un terminal séparé :

```powershell
docker logs -f cassandra-detector 2>&1 | Select-String "FIRING|PENDING|score|anomal" -SimpleMatch
```

Ou interroger la DB toutes les 20 s :

```powershell
# A lancer en boucle (Ctrl+C pour arreter)
while ($true) {
  $n = docker exec cassandra-timescaledb psql -U cassandra -d cassandra -t `
    -c "SELECT count(*) FROM alerts WHERE state='firing'"
  Write-Host "$(Get-Date -Format 'HH:mm:ss')  FIRING=$($n.Trim())"
  Start-Sleep 20
}
```

**Screenshot 8** : transitions PENDING puis FIRING dans les logs.

---

## Étape 9 — Résultat : alertes + attribution

Une fois au moins 1 alerte FIRING :

```powershell
docker exec cassandra-timescaledb psql -U cassandra -d cassandra -c "
SELECT a.endpoint_id,
       a.severity,
       round(a.score::numeric, 2)               AS score,
       a.layer,
       COALESCE(d.service||' '||d.version, '-') AS deploiement_suspecte,
       round(a.imputation_score::numeric, 2)    AS score_imput
FROM alerts a
LEFT JOIN deploy_events d ON d.deploy_id::text = a.suspected_deploy_id
WHERE a.state = 'firing'
ORDER BY a.score DESC;"
```

**Screenshot 9** : alerte `GET /orders/{order_id}`, score ~0.75, deploy `orders v2.5.0-demo` attribué.

---

## Étape 10 — Détail de l'anomalie (attribution ML)

```powershell
docker exec cassandra-timescaledb psql -U cassandra -d cassandra -x -c "
SELECT direction,
       contributing_features->'layers'       AS couches,
       contributing_features->'top_features' AS top3_features,
       contributing_features->'ttd'          AS alerte_precoce,
       contributing_features->'baseline'     AS deviation_baseline
FROM anomalies
WHERE endpoint_id = 'GET /orders/{order_id}'
ORDER BY detected_at DESC
LIMIT 1;"
```

**Screenshot 10** : `direction=degradation`, `top_features=[baseline_dev_p99, latency_slope, p99_over_p50]`, TTD si détecté.

---

## Étape 11 — Grafana après injection

Retourner sur http://localhost:3000 → dashboard **"Cassandra — API Performance"**.

**Screenshot 11** : spike de latence p99 bien visible, alerte active sur le panel d'état.

---

## Étape 12 — Rapport d'évaluation (résultats chiffrés)

```powershell
docker exec cassandra-detector python evaluate_layered.py --persist 2>&1 | Select-Object -Last 30
```

Sortie attendue :

```
=== Evaluation Detection par Couches ===
Fenetres d'injection  : 14  (grace 120s)
Detectee (layered)    : 10 / 14  recall=0.71
Detectee (static)     :  9 / 14  recall=0.65
Faux positifs (layered): 2.h   FP/h=1.29
Amelioration recall   : +6 pp
```

**Screenshot 12** : résultats 65%→71%, proof que le ML apporte quelque chose.

---

## Étape 13 — Nettoyage (fin de démo)

```powershell
# Stopper k6
docker stop cassandra-k6

# Réinitialiser la faute
curl -s -X POST http://localhost:8001/faults/reset

# (Optionnel) Stopper la stack
docker compose down
```

---

## Récapitulatif des screenshots à capturer

| #  | Ce qu'on capture |
|----|-----------------|
| 1  | `docker compose ps` — 13 services Up |
| 2  | Tables DB initiales (9 tables) |
| 3  | Grafana — trafic normal, aucune alerte |
| 4  | `/metrics` Prometheus du détecteur |
| 5  | Swagger deploy_api (http://localhost:8090/docs) |
| 6  | Réponse POST /deploys — enregistrement du déploiement |
| 7  | POST /faults/latency_step — injection confirmée |
| 8  | Logs détecteur — transition PENDING → FIRING |
| 9  | Table alerts — score + attribution déploiement |
| 10 | anomalies — top-3 features + TTD |
| 11 | Grafana — spike p99 visible post-injection |
| 12 | evaluate_layered.py — recall 65%→71% |

Durée totale estimée : 15–20 min (majoritairement en attente de préchauffage k6 + cycles du détecteur).
