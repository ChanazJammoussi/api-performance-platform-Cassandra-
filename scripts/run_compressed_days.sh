#!/usr/bin/env bash
#
# run_compressed_days.sh -- lance N journees simulees de k6/load.js (INCHANGE),
# chacune a une heure paire UTC, pour le mode saisonnier "compresse"
# (detection-service/season.py : 1 journee simulee = 2 h reelles, debut a chaque
# heure paire UTC ; 1 heure simulee = 5 min reelles).
#
# Pourquoi aligner : la baseline range chaque mesure dans l'heure simulee de
# l'horloge UTC. Relancer k6 des qu'il se termine ferait deriver la journee
# simulee de quelques secondes a chaque cycle ; ici chaque journee demarre sur
# son heure paire (retard <= LATE_MAX_S, non cumulatif).
#
# Usage :
#   bash scripts/run_compressed_days.sh N              # N journees (ex. 7)
#   bash scripts/run_compressed_days.sh N --dry-run    # affiche le planning, ne lance rien
#
# Variables optionnelles :
#   K6_RUNNER    ou tourne k6 : hote (defaut, k6.exe sous Windows) ou docker.
#                Le mode compresse exige hote : la campagne refuse un conteneur k6
#                (docs/mode_compresse.md).
#                Le mode hote evite l'horloge de la VM Docker/WSL2, ralentie sur
#                cette machine (les etapes de load.js y durent ~5 % de trop).
#   K6_BIN       mode hote : chemin de k6 (defaut k6 dans le PATH),
#                ex. /c/Users/chana/tools/k6/k6.exe
#   K6_NETWORK   mode docker : reseau du projet (defaut cassandra_fresh_default)
#   K6_BASE_URL  URL du gateway (defaut http://gateway:8000 en mode docker,
#                http://localhost:8000 en mode hote)
#   K6_IMAGE     mode docker : image k6 locale, jamais telechargee (defaut grafana/k6:latest)
#   JOURNAL      journal TSV (defaut scenario-runner/results/compresse/journal_journees.tsv)
#   STOP_FLAG    fichier d'arret (defaut <JOURNAL>.stop)
#   PID_FILE     mode hote : PID de la journee k6 en cours (defaut <JOURNAL>.k6.pid ;
#                "pid_bash pid_windows", supprime en fin de journee)
#   FAKE_NOW     (tests, --dry-run uniquement) instant epoch UTC simule
#
# Journal (lu par detection-service/baseline_readiness.py), une ligne par journee :
#   cycle <TAB> heure_paire <TAB> debut <TAB> fin <TAB> code_de_sortie_k6
#
# Arret propre : touch "$STOP_FLAG" -> aucune nouvelle journee n'est lancee
# (la journee en cours va a son terme). Interruption de la journee en cours :
#   mode docker : docker stop cassandra-k6-compresse
#   mode hote   : taskkill //PID <pid_windows de PID_FILE>
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CYCLE_S=7200          # = season.CYCLE_MINUTES * 60
LATE_MAX_S=60         # retard toleres apres l'heure paire pour demarrer une journee
NAME=cassandra-k6-compresse
NET="${K6_NETWORK:-cassandra_fresh_default}"
IMAGE="${K6_IMAGE:-grafana/k6:latest}"
JOURNAL="${JOURNAL:-$REPO_DIR/scenario-runner/results/compresse/journal_journees.tsv}"
STOP_FLAG="${STOP_FLAG:-$JOURNAL.stop}"
K6_RUNNER="${K6_RUNNER:-hote}"
K6_BIN="${K6_BIN:-k6}"
PID_FILE="${PID_FILE:-$JOURNAL.k6.pid}"

usage() { echo "usage : $0 N [--dry-run]" >&2; exit 2; }
[ $# -ge 1 ] || usage
N="$1"; shift
[[ "$N" =~ ^[1-9][0-9]*$ ]] || usage
DRY=0
if [ $# -ge 1 ]; then
  [ "$1" = "--dry-run" ] || usage
  DRY=1
fi

case "$K6_RUNNER" in
  docker) BASE_URL="${K6_BASE_URL:-http://gateway:8000}" ;;
  hote)   BASE_URL="${K6_BASE_URL:-http://localhost:8000}" ;;
  *) echo "K6_RUNNER invalide : '$K6_RUNNER' (attendu : docker ou hote)" >&2; exit 2 ;;
esac

# Chemin d'un fichier du depot tel que k6.exe (Windows) l'attend :
# Git Bash -> pwd -W ; WSL -> wslpath -w ; sinon chemin inchange.
host_path() {
  local d b w
  d="$(cd "$(dirname "$1")" && pwd)"; b="$(basename "$1")"
  if w="$(cd "$d" && pwd -W 2>/dev/null)" && [ -n "$w" ]; then echo "$w/$b"
  elif command -v wslpath >/dev/null 2>&1; then wslpath -w "$d/$b"
  else echo "$d/$b"; fi
}
LOAD_JS_HOST="$(host_path "$REPO_DIR/k6/load.js")"

# Commande lancee pour chaque journee (affichee par --dry-run sur la sortie d'erreur).
if [ "$K6_RUNNER" = docker ]; then
  K6_CMD_DESC="docker run --rm --pull=never --name $NAME --network $NET -e BASE_URL=$BASE_URL -v <k6>:/scripts:ro $IMAGE run --quiet /scripts/load.js"
else
  K6_CMD_DESC="BASE_URL=$BASE_URL $K6_BIN run --quiet --no-usage-report $LOAD_JS_HOST"
fi

now_s() { if [ "$DRY" = 1 ] && [ -n "${FAKE_NOW:-}" ]; then echo "$FAKE_NOW"; else date -u +%s; fi; }
iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%S+00:00; }
log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

# Heure paire de la prochaine journee : celle qui vient de passer si on a moins de
# LATE_MAX_S de retard, sinon la suivante ; toujours apres la derniere utilisee.
next_boundary() {
  local now="$1" last="$2" b
  b=$(( now / CYCLE_S * CYCLE_S ))
  if (( now - b > LATE_MAX_S )); then b=$(( b + CYCLE_S )); fi
  if (( last > 0 && b <= last )); then b=$(( last + CYCLE_S )); fi
  echo "$b"
}

if [ "$DRY" = 1 ]; then
  echo "# mode=$K6_RUNNER base_url=$BASE_URL commande : $K6_CMD_DESC" >&2
  t=$(now_s); last=0
  for c in $(seq 1 "$N"); do
    b=$(next_boundary "$t" "$last")
    echo -e "$c\t$(iso "$b")"
    last=$b; t=$(( b + CYCLE_S + 30 ))    # k6 dure 120 min + ~30 s d'arret
  done
  exit 0
fi

# Mode hote : k6 doit etre executable AVANT toute ecriture (journal, dossier).
if [ "$K6_RUNNER" = hote ]; then
  if ! command -v "$K6_BIN" >/dev/null 2>&1 && [ ! -x "$K6_BIN" ]; then
    echo "k6 introuvable ou non executable : $K6_BIN (definir K6_BIN)" >&2; exit 2
  fi
  [ -f "$REPO_DIR/k6/load.js" ] || { echo "k6/load.js introuvable" >&2; exit 2; }
fi

# Chemin du dossier k6 pour docker -v (Git Bash sous Windows : chemin Windows).
K6_DIR="$(cd "$REPO_DIR/k6" && (pwd -W 2>/dev/null || pwd))"
mkdir -p "$(dirname "$JOURNAL")"
[ -f "$JOURNAL" ] || printf '# cycle\theure_paire\tdebut\tfin\tcode\n' > "$JOURNAL"
rm -f "$STOP_FLAG"
log "journees compressees : $N prevue(s) ; mode k6 $K6_RUNNER ($BASE_URL) ; journal $JOURNAL ; arret : touch $STOP_FLAG"

last=0
for c in $(seq 1 "$N"); do
  [ -f "$STOP_FLAG" ] && { log "STOP_FLAG present -> arret avant la journee $c"; break; }
  b=$(next_boundary "$(now_s)" "$last")
  while (( $(now_s) < b )); do
    [ -f "$STOP_FLAG" ] && { log "STOP_FLAG present -> arret"; exit 0; }
    sleep $(( b - $(now_s) > 30 ? 30 : b - $(now_s) ))
  done
  start=$(now_s)
  log "journee $c : heure paire $(iso "$b"), demarrage a +$(( start - b )) s"
  if [ "$K6_RUNNER" = docker ]; then
  MSYS_NO_PATHCONV=1 docker run --rm --pull=never --name "$NAME" --network "$NET" \
    -e BASE_URL="$BASE_URL" -v "$K6_DIR":/scripts:ro \
    "$IMAGE" run --quiet /scripts/load.js
  code=$?
  else
    # k6.exe sous Windows (horloge de l'hote). WSLENV transmet BASE_URL si le script
    # tourne dans WSL ; sans effet sous Git Bash. Pas de statistiques d'usage envoyees.
    BASE_URL="$BASE_URL" WSLENV="BASE_URL${WSLENV:+:$WSLENV}" \
      "$K6_BIN" run --quiet --no-usage-report "$LOAD_JS_HOST" &
    k6pid=$!
    printf '%s %s\n' "$k6pid" "$(cat "/proc/$k6pid/winpid" 2>/dev/null || echo '?')" > "$PID_FILE"
    wait "$k6pid"
    code=$?
    rm -f "$PID_FILE"
  fi
  end=$(now_s)
  printf '%s\t%s\t%s\t%s\t%s\n' "$c" "$(iso "$b")" "$(iso "$start")" "$(iso "$end")" "$code" >> "$JOURNAL"
  log "journee $c terminee (code $code, $(( end - start )) s)"
  last=$b
done
log "fin des journees compressees"
