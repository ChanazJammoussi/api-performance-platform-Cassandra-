#!/usr/bin/env bash
#
# keep_k6_alive.sh -- maintient le trafic nominal k6 pendant tout le benchmark (~12h).
#
# Relance /scripts/load.js (INCHANGE) a chaque fin de cycle (~2h, le scenario
# ramping-vus dure 120 min puis k6 sort). Produit UNIQUEMENT du trafic de fond
# via l'API gateway. Ne touche NI la baseline gelee, NI le modele fige, NI la
# config k6 : c'est exactement la meme commande docker run que le lancement initial.
#
# Lancement (dans WSL, en arriere-plan) :
#   nohup bash /mnt/c/Users/chana/cassandra/benchmark/keep_k6_alive.sh \
#     > /mnt/c/Users/chana/cassandra/benchmark/keep_k6_alive.log 2>&1 &
#
# Arret propre (a la FIN du benchmark) :
#   touch /mnt/c/Users/chana/cassandra/benchmark/.k6_keepalive.stop   # stoppe la relance
#   docker stop cassandra-demo-k6                                     # termine le cycle en cours
#
set -uo pipefail

K6_DIR=/mnt/c/Users/chana/cassandra/k6
STOP_FLAG=/mnt/c/Users/chana/cassandra/benchmark/.k6_keepalive.stop
NAME=cassandra-demo-k6
NET=cassandra_default
BASE_URL=http://gateway:8000
MAX_HOURS=14            # garde-fou : ne tourne jamais au-dela (couvre les ~12h + marge)

log(){ echo "[$(date -u +%H:%M:%SZ)] $*"; }

# Attend que le nom de conteneur soit libre (l'ancien cycle se supprime via --rm).
ensure_name_free(){
  local i
  for i in $(seq 1 12); do
    [ -z "$(docker ps -aq -f name=^/${NAME}$)" ] && return 0
    sleep 5
  done
  docker rm -f "$NAME" >/dev/null 2>&1 || true
}

start_ts=$(date -u +%s)
deadline=$(( start_ts + MAX_HOURS*3600 ))
rm -f "$STOP_FLAG"

log "keepalive k6 demarre (max ${MAX_HOURS}h ; arret via $STOP_FLAG)"
cycle=0
while true; do
  [ -f "$STOP_FLAG" ]                     && { log "STOP_FLAG present -> arret";     break; }
  [ "$(date -u +%s)" -ge "$deadline" ]    && { log "MAX_HOURS atteint -> arret";     break; }

  cycle=$((cycle+1))
  ensure_name_free
  log "cycle #$cycle : lancement k6 load.js (~2h)"
  docker run --rm -i --name "$NAME" --network "$NET" \
    -e BASE_URL="$BASE_URL" \
    -v "$K6_DIR":/scripts \
    grafana/k6 run /scripts/load.js \
    || log "cycle #$cycle : k6 sorti (code $?) -- relance apres pause"
  log "cycle #$cycle termine"
  sleep 3
done
log "keepalive k6 arrete apres $cycle cycle(s)"
