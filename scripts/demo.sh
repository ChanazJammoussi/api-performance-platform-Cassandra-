#!/usr/bin/env bash
#
# demo.sh -- demonstration bout-en-bout de Cassandra (scenario bad_deploy).
#
# Enchaine : trafic k6 -> enregistrement d'un deploiement -> injection d'une
# regression de latence -> detection en couches -> attribution au deploiement ->
# affichage de l'alerte enrichie. Reproductible pour la soutenance.
#
# Prerequis : la stack doit tourner ("docker compose up -d --build").
# Usage      : bash scripts/demo.sh
# Variables  : LATENCY_MS (700), WARMUP (150s), DETECT_WAIT (240s), NETWORK.
#
set -eu

NETWORK="${NETWORK:-cassandra_default}"
GATEWAY_INTERNAL="http://gateway:8000"
ORDERS="http://localhost:8001"
DEPLOY_API="http://localhost:8090"
TARGET="GET /orders/{order_id}"
SERVICE="orders"
VERSION="v2.5.0-$(date +%H%M%S)"
LATENCY_MS="${LATENCY_MS:-700}"
WARMUP="${WARMUP:-150}"
DETECT_WAIT="${DETECT_WAIT:-240}"
K6_NAME="cassandra-demo-k6"
PSQL="docker exec cassandra-timescaledb psql -U cassandra -d cassandra -t"
PSQLC="docker exec cassandra-timescaledb psql -U cassandra -d cassandra"

say()  { printf "\n\033[1;36m========== %s ==========\033[0m\n" "$*"; }
info() { printf "  %s\n" "$*"; }

cleanup() {
  docker stop "$K6_NAME" >/dev/null 2>&1 || true
  curl -s -X POST "$ORDERS/faults/reset" >/dev/null 2>&1 || true
  info "Nettoyage : faute reset, trafic arrete."
}
trap cleanup EXIT

# --- 0. Verification de la stack -------------------------------------------
say "0. Verification de la stack"
for c in cassandra-gateway cassandra-orders cassandra-detector cassandra-scraper cassandra-deploy-api cassandra-timescaledb; do
  if ! docker ps --format '{{.Names}}' | grep -q "^${c}$"; then
    echo "  [ERREUR] $c n'est pas demarre. Lance : docker compose up -d --build" >&2
    exit 1
  fi
done
info "Tous les services requis tournent."

# Repart d'un etat d'alerte propre : d'anciennes alertes peuvent etre restees
# "firing" (trafic arrete -> p99 NaN -> jamais resolues). On les remet a OK pour
# prouver une vraie transition OK -> FIRING pendant cette demo.
$PSQL -c "UPDATE alerts SET state='ok', pending_count=0, resolving_count=0, opened_at=NULL, resolved_at=now() WHERE state <> 'ok'" >/dev/null 2>&1 || true
info "Etat d'alerte reinitialise (slate propre)."

# --- 1. Trafic k6 en arriere-plan ------------------------------------------
say "1. Generation de trafic (k6, ${WARMUP}s de prechauffe)"
K6_SCRIPT="$(mktemp)"
cat > "$K6_SCRIPT" <<'JS'
import http from 'k6/http'; import { sleep } from 'k6';
export const options = { vus: 15, duration: '12m' };
export default function () {
  http.get(`${__ENV.BASE_URL}/api/orders/1`);
  http.get(`${__ENV.BASE_URL}/api/orders`);
  sleep(0.3 + Math.random() * 0.3);
}
JS
docker run --rm -i --name "$K6_NAME" --network "$NETWORK" \
  -e BASE_URL="$GATEWAY_INTERNAL" grafana/k6 run - < "$K6_SCRIPT" >/dev/null 2>&1 &
info "Trafic lance. Prechauffe pour remplir la fenetre 5min de Prometheus..."
sleep "$WARMUP"
info "Prechauffe terminee."

# --- 2. Enregistrement du deploiement --------------------------------------
say "2. Enregistrement d'un deploiement (control plane)"
DEPLOY_RESP=$(curl -s -X POST "$DEPLOY_API/deploys" -H 'Content-Type: application/json' \
  -d "{\"service\":\"$SERVICE\",\"version\":\"$VERSION\",\"metadata\":{\"commit\":\"demo\",\"author\":\"chanaz\"}}")
info "Deploiement enregistre : $SERVICE $VERSION"
info "$DEPLOY_RESP"

# --- 3. Injection de la regression -----------------------------------------
say "3. Injection d'une regression de latence (+${LATENCY_MS}ms sur $SERVICE)"
# Horodatage de reference : seules les alertes ouvertes APRES ce point comptent
# (preuve d'une detection fraiche, pas d'un etat residuel).
DEMO_START=$($PSQL -c "SELECT now()" | sed 's/^ *//;s/ *$//')
info "Debut de fenetre de detection : $DEMO_START"
curl -s -X POST "$ORDERS/faults/latency_step" -H 'Content-Type: application/json' \
  -d "{\"latency_ms\":$LATENCY_MS}" >/dev/null
info "Faute injectee sur l'endpoint cible : $TARGET (SLO p99 = 300ms)."

# --- 4. Attente de la detection (PENDING -> FIRING) ------------------------
say "4. Attente de la detection (max ${DETECT_WAIT}s, cadence detecteur 60s)"
deadline=$(( $(date +%s) + DETECT_WAIT ))
fired=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  n=$($PSQL -c "SELECT count(*) FROM alerts WHERE state='firing' AND opened_at > '$DEMO_START'" | tr -d '[:space:]' || echo 0)
  remaining=$(( deadline - $(date +%s) ))
  info "alertes FIRING (fraiches) = ${n}   (${remaining}s restants)"
  if [ "${n:-0}" -ge 1 ]; then fired=1; break; fi
  sleep 20
done

if [ "$fired" -ne 1 ]; then
  echo "  [WARN] Aucune alerte FIRING dans le delai. (Trafic insuffisant ? baseline absente ?)" >&2
  exit 2
fi

# --- 5. Resultat : alertes + attribution deploiement -----------------------
say "5. Alertes FIRING + attribution du deploiement"
$PSQLC -c "
SELECT a.endpoint_id,
       a.severity,
       round(a.score::numeric,2)            AS score,
       a.layer,
       COALESCE(d.service||' '||d.version,'-') AS deploiement_suspecte,
       round(a.imputation_score::numeric,2) AS score_imput
FROM alerts a
LEFT JOIN deploy_events d ON d.deploy_id::text = a.suspected_deploy_id
WHERE a.state='firing' AND a.opened_at > '$DEMO_START'
ORDER BY a.endpoint_id;"

say "6. Detail de l'anomalie sur l'endpoint cible (breakdown par couche)"
$PSQLC -x -c "
SELECT direction,
       contributing_features->'layers'       AS couches,
       contributing_features->'top_features' AS top3,
       contributing_features->'ttd'          AS alerte_precoce,
       contributing_features->'baseline'     AS baseline
FROM anomalies
WHERE endpoint_id = '$TARGET'
ORDER BY detected_at DESC
LIMIT 1;"

# --- 7. Liens ---------------------------------------------------------------
say "7. A visualiser"
info "Grafana (sante)      : http://localhost:3000  (admin/admin)"
info "Grafana (evaluation) : dashboard 'Cassandra - Evaluation'"
info "Deploy API           : http://localhost:8090/docs"
info ""
info "Demo terminee. Nettoyage automatique en sortie."
