#!/usr/bin/env bash
#
# keep_k6_light.sh -- maintient un trafic nominal LEGER et CONSTANT (8 VUs) pour la campagne.
#
# Remplace le profil diurne (keep_k6_alive.sh, rampe jusqu'a 50 VUs) qui saturait le gateway
# mono-thread et faisait exploser la p99 nominale. Relance load_light.js (8 VUs, 4h) en boucle.
# Meme conteneur (cassandra-demo-k6) pour rester compatible avec le preflight.
#
# Lancement (WSL, arriere-plan) :
#   nohup bash /mnt/c/Users/chana/cassandra/benchmark/keep_k6_light.sh \
#     > /mnt/c/Users/chana/cassandra/benchmark/keep_k6_light.log 2>&1 &
#
# Arret : touch /mnt/c/Users/chana/cassandra/benchmark/.k6_light.stop ; docker stop cassandra-demo-k6
#
set -uo pipefail

K6_DIR=/mnt/c/Users/chana/cassandra/k6
STOP_FLAG=/mnt/c/Users/chana/cassandra/benchmark/.k6_light.stop
NAME=cassandra-demo-k6
NET=cassandra_default
BASE_URL=http://gateway:8000
MAX_HOURS=8

log(){ echo "[$(date -u +%H:%M:%SZ)] $*"; }

ensure_name_free(){
  local i
  for i in $(seq 1 12); do
    [ -z "$(docker ps -aq -f name=^/${NAME}$)" ] && return 0
    sleep 5
  done
  docker rm -f "$NAME" >/dev/null 2>&1 || true
}

start_ts=$(date -u +%s); deadline=$(( start_ts + MAX_HOURS*3600 ))
rm -f "$STOP_FLAG"
log "keepalive k6 LEGER (8 VUs constants) demarre (max ${MAX_HOURS}h ; arret via $STOP_FLAG)"
cycle=0
while true; do
  [ -f "$STOP_FLAG" ]                  && { log "STOP_FLAG present -> arret"; break; }
  [ "$(date -u +%s)" -ge "$deadline" ] && { log "MAX_HOURS atteint -> arret"; break; }
  cycle=$((cycle+1)); ensure_name_free
  log "cycle #$cycle : lancement k6 load_light.js (8 VUs, 4h)"
  docker run --rm -i --name "$NAME" --network "$NET" \
    -e BASE_URL="$BASE_URL" \
    -v "$K6_DIR":/scripts \
    grafana/k6 run /scripts/load_light.js \
    || log "cycle #$cycle : k6 sorti (code $?) -- relance"
  log "cycle #$cycle termine"; sleep 3
done
log "keepalive k6 leger arrete apres $cycle cycle(s)"
