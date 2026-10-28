#!/usr/bin/env bash
#
# run_campaign_x3.sh -- Benchmark Cassandra, Option 3 (injection directe).
#
# Lance 3 repetitions (campagnes) de la meme grille de scenarios via runner.py,
# puis evalue chaque campagne hors-ligne avec evaluate_layered.py --persist.
#
# INVARIANTS DU BENCHMARK GELE (verifies au pre-vol, abandon si non respecte) :
#   - baseline gelee   : conteneur cassandra-baseline ARRETE + table = snapshot
#                        endpoint_baseline_snapshot_20260802_132441.sql (md5 trie)
#   - modele gele      : iforest_latest.joblib == artefact iforest_20260802_132542.joblib
#                        (SHA256 f2a5e3ca...)
#   - aucun service modifie : detector / scraper / baseline / modele / seuils / scoring
#     restent tels quels. Le script n'INJECTE que des fautes (action nominale du bench)
#     et ne fait que LIRE la DB.
#
# Garde-fous integres (demandes) :
#   - garde DB   : aucune alerte FIRING/PENDING/RESOLVING avant chaque run (a fortiori
#                  avant chaque campagne). Intra-campagne AUSSI, car l'espacement entre
#                  scenarios est requis pour la validite du ground-truth.
#   - residu Prometheus : attente de 300 s apres chaque run + attente explicite de 300 s
#                  entre campagnes.
#
# A EXECUTER DEPUIS WSL (Ubuntu), ou tourne le venv Linux et ou localhost atteint
# les ports publies par Docker Desktop (8001/8002/8090/5434) :
#     wsl.exe -e bash /mnt/c/Users/chana/cassandra/benchmark/run_campaign_x3.sh
#
set -euo pipefail

# ------------------------------------------------------------------ Chemins
CASSANDRA=/mnt/c/Users/chana/cassandra
SUITE="$CASSANDRA/scenario-runner"
VENV_PY="$SUITE/venv/bin/python"
RUNNER="$SUITE/runner.py"
SCEN_DIR="$SUITE/scenarios"
RESULTS="$SUITE/results"

# --------------------------------------------------- Constantes du protocole
FROZEN_MODEL_SHA="f2a5e3caec0094935750c7a0558b3fe2b26e1e0e723372fcd66181f3b0a8413d"
FROZEN_MODEL_ARTIFACT="iforest_20260802_132542.joblib"
FROZEN_BASELINE_MD5="1b6865c12a617334399688c1325a32ac"   # md5 des donnees COPY triees
MIN_CORE=300          # duree minimale du tier core (= min_core_duration de la suite)
RESIDUE=300           # fenetre residu Prometheus (= PROMETHEUS_RESIDUE_WINDOW)
GUARD_TIMEOUT=900     # abandon si la DB ne se calme pas
GUARD_POLL=30
REPS=3

PSQL="docker exec cassandra-timescaledb psql -U cassandra -d cassandra -tA"

# --------------------------------------------- Acces APIs (via httpx, comme runner.py)
# Verifie la joignabilite des APIs que runner.py utilise (orders/payments/deploy-api)
# DEPUIS le meme contexte (WSL -> localhost publie). Requetes NON mutantes uniquement :
# GET sur la racine (pas de POST /faults, donc aucune injection).
api_check() {
  "$VENV_PY" - <<'PY'
import sys, httpx
targets = [
    ("orders    localhost:8001",           "http://localhost:8001/"),
    ("payments  localhost:8002",           "http://localhost:8002/"),
    ("deploy-api localhost:8090 /deploys", "http://localhost:8090/deploys"),
]
ok = True
for label, url in targets:
    try:
        r = httpx.get(url, timeout=5)
        print(f"    [OK] API {label}: HTTP {r.status_code}")
    except Exception as e:
        print(f"    [FAIL] API {label}: {type(e).__name__}: {e}")
        ok = False
sys.exit(0 if ok else 1)
PY
}

# ------------------------------------------------------------------ Pre-vol
preflight() {
  echo "=================================================================="
  echo "  PRE-VOL : verification du gel (aucune injection ici)"
  echo "=================================================================="

  # 1. baseline gelee : conteneur arrete
  local st
  st=$(docker inspect -f '{{.State.Status}}' cassandra-baseline 2>/dev/null || echo absent)
  [ "$st" = "exited" ] || { echo "[FAIL] cassandra-baseline doit etre 'exited' (actuel: $st)"; exit 1; }
  echo "[OK] cassandra-baseline exited -> baseline non recalculee"

  # 2. baseline = snapshot (md5 des donnees triees)
  local md5
  md5=$(docker exec cassandra-timescaledb pg_dump -U cassandra -d cassandra -t public.endpoint_baseline \
        | awk '/^COPY /{f=1;next} /^\\\.$/{f=0} f' | sort | md5sum | cut -d' ' -f1)
  [ "$md5" = "$FROZEN_BASELINE_MD5" ] \
    || { echo "[FAIL] baseline DB != snapshot (md5=$md5, attendu $FROZEN_BASELINE_MD5)"; exit 1; }
  echo "[OK] baseline DB = snapshot ($md5)"

  # 3. modele gele : latest == artefact fige
  local sha
  sha=$(docker exec cassandra-trainer sha256sum /app/models/iforest_latest.joblib | cut -d' ' -f1)
  [ "$sha" = "$FROZEN_MODEL_SHA" ] \
    || { echo "[FAIL] iforest_latest != modele fige (sha=$sha)"; exit 1; }
  echo "[OK] modele latest = $FROZEN_MODEL_ARTIFACT ($sha)"

  # 4. services requis en marche (detector requis pour que la garde DB progresse)
  local c s
  for c in cassandra-timescaledb cassandra-scraper cassandra-detector \
           cassandra-orders cassandra-payments cassandra-demo-k6; do
    s=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo absent)
    [ "$s" = "running" ] || { echo "[FAIL] $c doit etre running (actuel: $s)"; exit 1; }
  done
  echo "[OK] services requis running (scraper/detector/orders/payments/k6/db)"

  # 5. aucune alerte active
  local active
  active=$($PSQL -c "SELECT count(*) FROM alerts WHERE state IN ('firing','pending','resolving') OR pending_count>0;")
  [ "$active" = "0" ] || { echo "[FAIL] $active alerte(s) active(s) (FIRING/PENDING/RESOLVING)"; exit 1; }
  echo "[OK] aucune alerte active (FIRING/PENDING/RESOLVING = 0)"

  # 6. acces aux APIs necessaires (non mutant : GET seulement, aucune injection)
  echo "    verification acces APIs (httpx, comme runner.py)..."
  api_check
  echo "[OK] APIs necessaires joignables"
  echo
}

# --------------------------------------------------------------- Garde DB
db_guard() {
  local start=$SECONDS dirty
  while true; do
    dirty=$($PSQL -c "SELECT count(*) FROM alerts WHERE state IN ('firing','pending','resolving') OR pending_count>0;")
    [ "$dirty" = "0" ] && { echo "    [OK] garde DB : aucune alerte active"; return 0; }
    if [ $((SECONDS - start)) -ge $GUARD_TIMEOUT ]; then
      echo "    [FAIL] garde DB : timeout ${GUARD_TIMEOUT}s (dirty=$dirty)"; exit 1
    fi
    echo "    [WAIT] garde DB : $dirty alerte(s) active(s), reprise dans ${GUARD_POLL}s"
    sleep $GUARD_POLL
  done
}

residue_wait() {
  echo "    [WAIT] residu Prometheus : ${RESIDUE}s"
  sleep "$RESIDUE"
}

# -------------------------------------------------- Duree native d'un YAML
yaml_max_duration() {
  "$VENV_PY" -c "import yaml,sys; d=yaml.safe_load(open(sys.argv[1])); \
print(max((f.get('duration_seconds',60) for f in d.get('faults',[])), default=60))" "$1"
}

# ----------------------------------------------------------- Un run runner.py
run_tier() {
  local scn="$1" tier="$2" ts="$3" gt_dir="$4" override_arg="$5"
  local name; name=$(basename "$scn" .yaml)
  local gt="$gt_dir/${name}_${tier}_${ts}.json"
  echo "  >> ${name} / ${tier}   (${override_arg:-duree YAML})"
  db_guard
  "$VENV_PY" "$RUNNER" "$scn" --output "$gt" $override_arg
  residue_wait
}

# ------------------------------------------------- Evaluation offline (gelee)
run_eval() {
  local cid="$1"
  echo "=================================================================="
  echo "  EVALUATION OFFLINE (modele+baseline figes) : $cid"
  echo "=================================================================="
  # evaluate_layered.py tourne DANS le conteneur trainer :
  #   MODEL_DIR=/app/models -> charge iforest_latest.joblib (= modele fige)
  #   DATABASE_URL=timescaledb:5432 -> lit endpoint_baseline (= snapshot gele)
  #   GROUND_TRUTH monte sous /data/results (bind scenario-runner/results)
  docker exec cassandra-trainer python evaluate_layered.py \
    --input "/data/results/${cid}/ground_truth" --persist
}

write_manifest() {
  local cid="$1" rep="$2" dir="$3"
  local sha; sha=$(git -C "$CASSANDRA" rev-parse HEAD 2>/dev/null || echo unknown)
  cat > "$dir/manifest.json" <<EOF
{
  "campaign_id": "$cid",
  "repetition": $rep,
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "commit_sha": "$sha",
  "frozen_model_artifact": "$FROZEN_MODEL_ARTIFACT",
  "frozen_model_sha256": "$FROZEN_MODEL_SHA",
  "frozen_baseline_snapshot": "endpoint_baseline_snapshot_20260802_132441.sql",
  "frozen_baseline_md5": "$FROZEN_BASELINE_MD5",
  "pending_windows": 2,
  "scrape_interval": 60,
  "min_core_duration": $MIN_CORE,
  "prometheus_residue_window": $RESIDUE,
  "fire_threshold": 0.60,
  "grace_seconds": 120
}
EOF
}

# ------------------------------------------------------------------ Campagne
run_campaign() {
  local rep="$1"
  local ts; ts=$(date -u +%Y%m%d_%H%M)
  local cid="campaign_${ts}"
  local dir="$RESULTS/$cid"
  local gt_dir="$dir/ground_truth"
  mkdir -p "$gt_dir" "$dir/logs"
  write_manifest "$cid" "$rep" "$dir"

  echo
  echo "##################################################################"
  echo "#  REPETITION $rep/$REPS   ->   $cid"
  echo "##################################################################"

  local scn name dur core_ovr
  for scn in $(ls "$SCEN_DIR"/*.yaml | grep -v 'quiet_baseline' | sort); do
    name=$(basename "$scn" .yaml)
    dur=$(yaml_max_duration "$scn")
    # tier core : force a MIN_CORE si la duree native est plus courte
    if [ "$dur" -lt "$MIN_CORE" ]; then core_ovr="--duration-override $MIN_CORE"; else core_ovr=""; fi
    run_tier "$scn" "core"   "$ts" "$gt_dir" "$core_ovr"
    # tier stress : duree native du YAML (aucun override)
    run_tier "$scn" "stress" "$ts" "$gt_dir" ""
  done

  run_eval "$cid"
  echo "$cid" >> "$RESULTS/.last_campaigns_x3"
}

# ---------------------------------------------------------------------- Main
main() {
  # Mode --preflight-only : verifie tous les invariants puis quitte SANS injection.
  if [ "${1:-}" = "--preflight-only" ]; then
    preflight
    echo "=================================================================="
    echo "  PREFLIGHT-ONLY : TOUS LES CONTROLES OK -- aucune injection lancee."
    echo "=================================================================="
    exit 0
  fi

  preflight
  local rep
  for rep in $(seq 1 $REPS); do
    run_campaign "$rep"
    if [ "$rep" -lt "$REPS" ]; then
      echo
      echo "  [WAIT] attente inter-campagne : ${RESIDUE}s (residu Prometheus)"
      sleep "$RESIDUE"
    fi
  done
  echo
  echo "=================================================================="
  echo "  TERMINE : $REPS campagnes evaluees et persistees dans eval_runs"
  echo "  Campagnes : $(tr '\n' ' ' < "$RESULTS/.last_campaigns_x3")"
  echo "  Ne pas oublier : docker start cassandra-baseline  (fin de benchmark)"
  echo "=================================================================="
}

main "$@"
