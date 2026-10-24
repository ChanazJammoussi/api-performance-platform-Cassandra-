# lib_ground_truth.sh -- enregistrement de la ground truth d'une panne injectee
# hors scenario-runner (demo.sh), au format exact de scenario-runner/runner.py.
#
# Sans ce fichier, la panne est invisible pour les exclusions (baseline_job.py,
# train_model.py) et pollue la baseline / l'entrainement.
#
# Usage (apres "source") :
#   gt_start FICHIER SCENARIO_ID FAULT_TYPE SERVICE ENDPOINT MAGNITUDE_JSON PROVISOIRE_S
#       -> ecrit l'entree avec un cleared_at PROVISOIRE (now + PROVISOIRE_S), comme
#          l'entree partielle de runner.py : une interruption brutale ne laisse
#          jamais une panne sans fin.
#   gt_end
#       -> remplace cleared_at par l'heure reelle du retablissement.
#          Sans effet si gt_start n'a pas ete appele.

GT_FILE=""
GT_SCENARIO_ID=""
GT_FAULT_TYPE=""
GT_SERVICE=""
GT_ENDPOINT=""
GT_MAGNITUDE=""
GT_INJECTED_AT=""

# Horodatage UTC ISO 8601 (microsecondes, +00:00), lisible par datetime.fromisoformat.
_gt_now() {
  date -u +%Y-%m-%dT%H:%M:%S.%6N+00:00
}

_gt_in() {
  date -u -d "+$1 seconds" +%Y-%m-%dT%H:%M:%S.%6N+00:00
}

# Ecrit le fichier complet (meme structure que runner._write_output).
_gt_write() {
  local cleared_at="$1"
  printf '{\n  "duration_override": null,\n  "faults": [\n    {\n      "scenario_id": "%s",\n      "fault_type": "%s",\n      "target_service": "%s",\n      "target_endpoint": "%s",\n      "injected_at": "%s",\n      "cleared_at": "%s",\n      "magnitude": %s\n    }\n  ]\n}\n' \
    "$GT_SCENARIO_ID" "$GT_FAULT_TYPE" "$GT_SERVICE" "$GT_ENDPOINT" \
    "$GT_INJECTED_AT" "$cleared_at" "$GT_MAGNITUDE" >| "$GT_FILE"
}

gt_start() {
  if [ -e "$1" ]; then
    echo "  [ERREUR] ground truth deja present : $1 (non ecrase)" >&2
    return 1
  fi
  mkdir -p "$(dirname "$1")"
  GT_FILE="$1"
  GT_SCENARIO_ID="$2"
  GT_FAULT_TYPE="$3"
  GT_SERVICE="$4"
  GT_ENDPOINT="$5"
  GT_MAGNITUDE="$6"
  GT_INJECTED_AT="$(_gt_now)"
  _gt_write "$(_gt_in "$7")"
}

gt_end() {
  if [ -z "$GT_FILE" ] || [ ! -e "$GT_FILE" ]; then
    return 0
  fi
  _gt_write "$(_gt_now)"
}
