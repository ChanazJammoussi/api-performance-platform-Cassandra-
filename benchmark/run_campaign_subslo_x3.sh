#!/usr/bin/env bash
#
# run_campaign_subslo_x3.sh -- Campagne FOCALISEE sous-SLO pour mesurer l'apport Layered.
#
# But : demontrer que Layered detecte des degradations SOUS le SLO absolu (que Static
# ne peut pas voir), a condition que la baseline saisonniere soit REELLE a l'instant
# d'injection (pas de fallback -- cause de l'echec du benchmark precedent).
#
# Reutilise STRICTEMENT les artefacts figes :
#   - baseline gelee (conteneur cassandra-baseline ARRETE, table = snapshot 132441)
#   - modele fige iforest_20260802_132542.joblib (SHA256 f2a5e3ca...)
#   Aucune modif : modele / seuils / scoring / logique detecteur inchanges.
#
# Specificites vs run_campaign_x3.sh :
#   - GATE BUCKET REEL : refuse d'injecter si l'heure courante (dow,hour) n'a pas de
#     bucket saisonnier realiste pour GET /orders (sinon fallback -> Layered neutralise).
#   - Tier CORE uniquement (300s) : le tier stress (60s) ne peut pas former d'onset (M=2).
#   - Scenarios sous-SLO : +100ms et +150ms sur orders (p99 ~199/~245 < SLO 300).
#   - Apres chaque campagne : evaluate_layered --persist + dump scores Layer 0/1/2 par
#     fenetre + preuve "TP Layered-only" (p99<SLO ET combined>=0.60).
#
# A EXECUTER DEPUIS WSL, a un creneau couvert (jeudi h0-h12 ou h14-h18 UTC, etc.) :
#   MSYS_NO_PATHCONV=1 wsl.exe -e bash /mnt/c/Users/chana/cassandra/benchmark/run_campaign_subslo_x3.sh
#
set -uo pipefail

# ------------------------------------------------------------------ Chemins
CASSANDRA=/mnt/c/Users/chana/cassandra
SUITE="$CASSANDRA/scenario-runner"
VENV_PY="$SUITE/venv/bin/python"
RUNNER="$SUITE/runner.py"
SCEN_DIR="$CASSANDRA/benchmark/scenarios_subslo"
RESULTS="$SUITE/results"

# --------------------------------------------------- Constantes du protocole
FROZEN_MODEL_SHA="f2a5e3caec0094935750c7a0558b3fe2b26e1e0e723372fcd66181f3b0a8413d"
FROZEN_MODEL_ARTIFACT="iforest_20260802_132542.joblib"
FROZEN_BASELINE_MD5="1b6865c12a617334399688c1325a32ac"
RESIDUE=300
GUARD_TIMEOUT=900
GUARD_POLL=30
REPS=3
GATE_EP="GET /orders/{order_id}"       # endpoint cible des scenarios -> a gater
GATE_P90_MAX=260                        # rejette les buckets pollues (h13=726, h21=733)
# Scenarios (tier core, 300s). Ordre = ordre d'injection dans chaque repetition.
SCENARIOS=(
  "$SCEN_DIR/subslo_step_orders_p100.yaml"
  "$SCEN_DIR/subslo_step_orders_p150.yaml"
)

PSQL="docker exec cassandra-timescaledb psql -U cassandra -d cassandra -tA"

log(){ echo "[$(date -u +%H:%M:%SZ)] $*"; }

# --------------------------------------------- Acces APIs (httpx, comme runner.py)
api_check() {
  "$VENV_PY" - <<'PY'
import sys, httpx
ok=True
for label,url in [("orders localhost:8001","http://localhost:8001/"),
                  ("deploy-api localhost:8090 /deploys","http://localhost:8090/deploys")]:
    try:
        r=httpx.get(url,timeout=5); print(f"    [OK] API {label}: HTTP {r.status_code}")
    except Exception as e:
        print(f"    [FAIL] API {label}: {type(e).__name__}: {e}"); ok=False
sys.exit(0 if ok else 1)
PY
}

# ------------------------------------------------------------------ Pre-vol
preflight() {
  echo "=================================================================="
  echo "  PRE-VOL : gel + environnement (aucune injection ici)"
  echo "=================================================================="
  local st
  st=$(docker inspect -f '{{.State.Status}}' cassandra-baseline 2>/dev/null || echo absent)
  [ "$st" = "exited" ] || { echo "[FAIL] cassandra-baseline doit etre 'exited' (actuel: $st)"; exit 1; }
  echo "[OK] cassandra-baseline exited -> baseline non recalculee"

  local md5
  md5=$(docker exec cassandra-timescaledb pg_dump -U cassandra -d cassandra -t public.endpoint_baseline \
        | awk '/^COPY /{f=1;next} /^\\\.$/{f=0} f' | sort | md5sum | cut -d' ' -f1)
  [ "$md5" = "$FROZEN_BASELINE_MD5" ] \
    || { echo "[FAIL] baseline DB != snapshot (md5=$md5)"; exit 1; }
  echo "[OK] baseline DB = snapshot ($md5)"

  local sha
  sha=$(docker exec cassandra-detector sha256sum /app/models/iforest_latest.joblib | cut -d' ' -f1)
  [ "$sha" = "$FROZEN_MODEL_SHA" ] || { echo "[FAIL] modele latest != fige ($sha)"; exit 1; }
  echo "[OK] modele fige = $FROZEN_MODEL_ARTIFACT ($sha)"

  local c s
  for c in cassandra-timescaledb cassandra-scraper cassandra-detector cassandra-orders cassandra-demo-k6; do
    s=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo absent)
    [ "$s" = "running" ] || { echo "[FAIL] $c doit etre running (actuel: $s)"; exit 1; }
  done
  echo "[OK] services requis running (scraper/detector/orders/k6/db)"

  local active
  active=$($PSQL -c "SELECT count(*) FROM alerts WHERE state IN ('firing','pending','resolving') OR pending_count>0;")
  [ "$active" = "0" ] || { echo "[FAIL] $active alerte(s) active(s)"; exit 1; }
  echo "[OK] aucune alerte active"

  echo "    verification acces APIs..."
  api_check
  echo "[OK] APIs joignables"
  echo
}

# --------------------------- GATE BUCKET REEL (le point cle de cette campagne)
# Refuse l'injection si l'heure courante (dow,hour) n'a pas de bucket saisonnier
# realiste pour l'endpoint cible -> evite le fallback qui neutralise Layered.
bucket_gate() {
  local p90
  p90=$($PSQL -c "SELECT p90 FROM endpoint_baseline
                  WHERE endpoint_id='$GATE_EP' AND metric='p99_ms'
                    AND dow=EXTRACT(DOW FROM now())::int AND hour_bucket=EXTRACT(HOUR FROM now())::int;")
  if [ -z "$p90" ]; then
    echo "    [FAIL] GATE BUCKET : aucun bucket saisonnier a l'heure courante (dow/hour) -> FALLBACK. Injection REFUSEE."
    echo "           -> attends un creneau couvert (jeudi h0-h12 ou h14-h18 UTC)."
    exit 1
  fi
  if ! awk -v x="$p90" -v m="$GATE_P90_MAX" 'BEGIN{exit !(x+0<m+0)}'; then
    echo "    [FAIL] GATE BUCKET : bucket pollue (p90=$p90 >= $GATE_P90_MAX, type h13/h21). Injection REFUSEE."
    exit 1
  fi
  echo "    [OK] GATE BUCKET : $GATE_EP p99 p90=$p90 (bucket saisonnier reel, realiste)"
}

db_guard() {
  local start=$SECONDS dirty
  while true; do
    dirty=$($PSQL -c "SELECT count(*) FROM alerts WHERE state IN ('firing','pending','resolving') OR pending_count>0;")
    [ "$dirty" = "0" ] && { echo "    [OK] garde DB : aucune alerte active"; return 0; }
    if [ $((SECONDS - start)) -ge $GUARD_TIMEOUT ]; then
      echo "    [FAIL] garde DB : timeout ${GUARD_TIMEOUT}s (dirty=$dirty)"; exit 1
    fi
    echo "    [WAIT] garde DB : $dirty alerte(s), reprise dans ${GUARD_POLL}s"; sleep $GUARD_POLL
  done
}

residue_wait() { echo "    [WAIT] residu Prometheus : ${RESIDUE}s"; sleep "$RESIDUE"; }

# ------------------------------------------------- Evaluation offline (gelee)
run_eval() {
  local cid="$1"
  echo "=================================================================="
  echo "  EVALUATION OFFLINE (modele+baseline figes) : $cid"
  echo "=================================================================="
  docker exec cassandra-detector python evaluate_layered.py \
    --input "/data/results/${cid}/ground_truth" --persist
}

# ---- Dump scores Layer 0/1/2 par fenetre + preuve TP Layered-only (read-only) ----
run_layer_analysis() {
  local cid="$1"
  echo "------------------------------------------------------------------"
  echo "  ANALYSE COUCHES (read-only) : $cid"
  echo "------------------------------------------------------------------"
  docker exec -i -e CID="$cid" cassandra-detector python - <<'PYEOF' || echo "    [WARN] analyse couches echouee (non bloquant)"
import os, csv
from pathlib import Path
import psycopg2
import ml_model
from features import compute_features, vectorize, direction_of, WINDOW_SIZE, MIN_WINDOW
from baseline_utils import compute_deviation, ENDPOINT_SLOS, DEFAULT_SLOS, pg_dow
from train_model import load_baseline_lookup, baseline_for
import evaluate_layered as EL

CID=os.environ["CID"]; GTDIR=f"/data/results/{CID}/ground_truth"; OUT=f"/data/results/{CID}/layer_scores.csv"
FT=EL.FIRE_THRESHOLD
model=ml_model.load_latest()
conn=psycopg2.connect(os.environ["DATABASE_URL"]); exact,fallback=load_baseline_lookup(conn)
wins=EL.load_windows(Path(GTDIR))
since=min(w.injected_at for w in wins)-EL.PAD; until=max(w.cleared_at for w in wins)+EL.GRACE+EL.PAD
by=EL.load_features_span(conn,since,until); onsets=EL.build_onsets(by,exact,fallback,model)

print(f"\n  {'fault_type':22s} {'endpoint':26s} {'static':7s} {'layered':7s}")
lonly=[]
for w in wins:
    ep=onsets.get(w.target_endpoint,{"static":[],"layered":[]})
    s=EL.match_window(ep["static"],w) is not None; l=EL.match_window(ep["layered"],w) is not None
    print(f"  {w.fault_type:22s} {w.target_endpoint:26s} {str(s):7s} {str(l):7s}")
    if l and not s: lonly.append(w)

rows=[]
for ep_id,frows in by.items():
    for r in frows: r["endpoint_id"]=ep_id
    usable=frows[:-1] if len(frows)>1 else frows
    slo=ENDPOINT_SLOS.get(ep_id,DEFAULT_SLOS)
    for i in range(len(usable)):
        t=usable[i]["time"]; w=usable[max(0,i-WINDOW_SIZE+1):i+1]
        src="bucket" if (ep_id,'p99_ms',pg_dow(t),t.hour) in exact else "fallback"
        bl=baseline_for(exact,fallback,ep_id,t); b99=bl.get("p99_ms")
        p10,p50b,p90=(b99[0],b99[1],b99[2]) if b99 else (None,None,None)
        p=w[-1]; p99=p["p99_ms"]
        dev=compute_deviation(p99,p10,p90); direction=direction_of(p99,p10,p90)
        sb=p99>slo["p99_ms"]; snorm=min(1.0,(p99-slo["p99_ms"])/(slo["p99_ms"]+1e-6)) if sb else 0.0
        bnorm=min(1.0,dev/2.0); mn=0.0
        if model and len(w)>=MIN_WINDOW:
            x=vectorize(compute_features(w,bl)); raw=ml_model.raw_anomaly(model["model"],x)[0]
            mn=float(ml_model.calibrate(raw,model["meta"]["calibration"]))
        mg=mn if direction=="degradation" else 0.0; c=bnorm+(1-bnorm)*mg
        if sb: c=max(c,snorm)
        eb=(p.get("error_rate_5xx") or 0)>slo["error_rate_5xx"]
        in_inj=any(w2.target_endpoint==ep_id and w2.injected_at<=t<=w2.cleared_at for w2 in wins)
        rows.append([ep_id,t.isoformat(),("INJ" if in_inj else "-"),round(p99,1),
                     round(p.get("p50_ms") or 0,1),round(p.get("rps") or 0,2),round(p.get("error_rate_5xx") or 0,2),
                     (round(p90,1) if p90 else ""),src,round(bnorm,3),direction,round(mn,3),round(mg,3),
                     round(snorm,3),round(c,3),(sb or eb),(sb or eb or c>=FT)])
with open(OUT,"w",newline="") as f:
    wtr=csv.writer(f)
    wtr.writerow(["endpoint","time","phase","p99","p50","rps","err","bl_p90","source",
                  "baseline_norm","direction","ml_norm","ml_gated","static_norm","combined","static_fire","layered_fire"])
    wtr.writerows(rows)
print(f"\n  scores par fenetre -> {OUT} ({len(rows)} lignes)")

print(f"\n  --- PREUVE TP Layered-only ({len(lonly)}) : p99<SLO ET combined>=0.60 ---")
for w in lonly:
    slo=ENDPOINT_SLOS.get(w.target_endpoint,DEFAULT_SLOS)["p99_ms"]; best=None
    for r in rows:
        if r[0]==w.target_endpoint and r[2]=="INJ" and (best is None or r[14]>best[14]): best=r
    if best:
        print(f"    {w.fault_type} {w.target_endpoint} | p99={best[3]}<SLO{slo} | bl_p90={best[7]} src={best[8]} "
              f"| ml_norm={best[11]} ml_gated={best[12]} combined={best[14]}")
if not lonly:
    print("    (aucune -- Layered n'a rien capte que Static n'ait capte : verifier gate bucket / magnitudes)")
conn.close()
PYEOF
}

write_manifest() {
  local cid="$1" rep="$2" dir="$3" p90="$4"
  local sha; sha=$(git -C "$CASSANDRA" rev-parse HEAD 2>/dev/null || echo unknown)
  cat > "$dir/manifest.json" <<EOF
{
  "campaign_id": "$cid",
  "type": "subslo_layered_advantage",
  "repetition": $rep,
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "commit_sha": "$sha",
  "frozen_model_artifact": "$FROZEN_MODEL_ARTIFACT",
  "frozen_model_sha256": "$FROZEN_MODEL_SHA",
  "frozen_baseline_md5": "$FROZEN_BASELINE_MD5",
  "gate_endpoint": "$GATE_EP",
  "gate_bucket_p90_at_start": $p90,
  "baseline_source": "seasonal_bucket (gated, non-fallback)",
  "tier": "core_only_300s",
  "fire_threshold": 0.60,
  "scenarios": ["subslo_step_orders_p100 (+100ms)", "subslo_step_orders_p150 (+150ms)"]
}
EOF
}

# ------------------------------------------------------------------ Campagne
run_campaign() {
  local rep="$1"
  local ts; ts=$(date -u +%Y%m%d_%H%M)
  local cid="campaign_subslo_${ts}"
  local dir="$RESULTS/$cid"; local gt_dir="$dir/ground_truth"
  mkdir -p "$gt_dir" "$dir/logs"
  local p90start
  p90start=$($PSQL -c "SELECT COALESCE(p90,0) FROM endpoint_baseline WHERE endpoint_id='$GATE_EP' AND metric='p99_ms' AND dow=EXTRACT(DOW FROM now())::int AND hour_bucket=EXTRACT(HOUR FROM now())::int;")
  [ -z "$p90start" ] && p90start=0
  write_manifest "$cid" "$rep" "$dir" "$p90start"

  echo; echo "##################################################################"
  echo "#  REPETITION $rep/$REPS   ->   $cid"
  echo "##################################################################"

  local scn name gt
  for scn in "${SCENARIOS[@]}"; do
    name=$(basename "$scn" .yaml)
    gt="$gt_dir/${name}_core_${ts}.json"
    echo "  >> ${name} / core (300s)"
    bucket_gate          # refuse si fallback / bucket pollue a l'heure courante
    db_guard
    "$VENV_PY" "$RUNNER" "$scn" --output "$gt"
    residue_wait
  done

  run_eval "$cid"
  run_layer_analysis "$cid"
  echo "$cid" >> "$RESULTS/.last_subslo_campaigns"
}

main() {
  if [ "${1:-}" = "--preflight-only" ]; then
    preflight; bucket_gate
    echo "=================================================================="
    echo "  PREFLIGHT-ONLY OK -- creneau couvert, aucune injection lancee."
    echo "=================================================================="
    exit 0
  fi
  preflight
  local rep
  for rep in $(seq 1 $REPS); do
    run_campaign "$rep"
    if [ "$rep" -lt "$REPS" ]; then
      echo; echo "  [WAIT] attente inter-campagne : ${RESIDUE}s"; sleep "$RESIDUE"
    fi
  done
  echo; echo "=================================================================="
  echo "  TERMINE : $REPS campagnes sous-SLO evaluees (eval_runs + layer_scores.csv)"
  echo "  Campagnes : $(tr '\n' ' ' < "$RESULTS/.last_subslo_campaigns" 2>/dev/null)"
  echo "  Rappel fin de benchmark : docker start cassandra-baseline"
  echo "=================================================================="
}

main "$@"
