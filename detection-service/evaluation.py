"""
evaluation.py -- Cassandra Phase 3 / Section 6.4
Calcule TP/FP/FN, precision, recall, F1, detection delay et attribution accuracy
en matchant les alertes FIRING de TimescaleDB contre les ground truth JSON.
"""

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Optional

import psycopg2
import psycopg2.extras
from tabulate import tabulate

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_DSN = "host=localhost port=5434 user=cassandra password=cassandra dbname=cassandra"
RESULTS_DIR = Path(__file__).parent.parent / "scenario-runner" / "results"
REPORT_PATH = Path(__file__).parent / "evaluation_report.json"

MATCH_BEFORE = timedelta(minutes=2)   # tolerance avant injected_at
MATCH_AFTER  = timedelta(minutes=5)   # tolerance apres cleared_at


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Injection:
    scenario_id: str
    fault_type: str
    target_endpoint: str
    injected_at: datetime
    cleared_at: datetime
    magnitude: dict
    source_file: str

@dataclass
class Alert:
    alert_id: str
    endpoint_id: str
    signal_type: str
    state: str
    opened_at: Optional[datetime]
    layer: Optional[str]
    suspected_fault: Optional[str]
    imputation_score: Optional[float]

@dataclass
class MatchResult:
    injection: Injection
    alert: Optional[Alert]        # None = FN
    is_tp: bool = False
    is_fn: bool = False
    detection_delay_s: Optional[float] = None
    attribution_correct: Optional[bool] = None  # None si FN

@dataclass
class UnmatchedAlert:
    alert: Alert                  # FP


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_ground_truth(results_dir: Path) -> list[Injection]:
    injections: list[Injection] = []
    files = sorted(results_dir.glob("*.json"))
    if not files:
        print(f"[WARN] Aucun fichier JSON dans {results_dir}", file=sys.stderr)
        return injections

    for path in files:
        try:
            with open(path) as f:
                records = json.load(f)
            if not isinstance(records, list):
                records = [records]
            for r in records:
                injections.append(Injection(
                    scenario_id=r["scenario_id"],
                    fault_type=r["fault_type"],
                    target_endpoint=r["target_endpoint"],
                    injected_at=datetime.fromisoformat(r["injected_at"]),
                    cleared_at=datetime.fromisoformat(r["cleared_at"]),
                    magnitude=r.get("magnitude", {}),
                    source_file=path.name,
                ))
        except Exception as e:
            print(f"[WARN] {path.name} : {e}", file=sys.stderr)

    return injections


def _has_history_data(conn, window_start: datetime, window_end: datetime) -> bool:
    query = """
        SELECT 1 FROM alerts_history
        WHERE opened_at IS NOT NULL
          AND opened_at >= %s
          AND opened_at <= %s
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(query, (window_start, window_end))
        return cur.fetchone() is not None


def load_firing_alerts(conn, window_start: datetime, window_end: datetime) -> list[Alert]:
    use_history = _has_history_data(conn, window_start, window_end)

    if use_history:
        print("[INFO] Source : alerts_history (trigger actif)")
        query = """
            SELECT history_id AS alert_id,
                   endpoint_id, signal_type,
                   'FIRING'   AS state,
                   opened_at, layer, suspected_fault, imputation_score
            FROM alerts_history
            WHERE opened_at IS NOT NULL
              AND opened_at >= %s
              AND opened_at <= %s
            ORDER BY opened_at ASC
        """
    else:
        print("[INFO] Source : alerts (fallback -- pas de donnees dans alerts_history)")
        query = """
            SELECT alert_id, endpoint_id, signal_type, state,
                   opened_at, layer, suspected_fault, imputation_score
            FROM alerts
            WHERE state IN ('FIRING', 'ok')
              AND opened_at IS NOT NULL
              AND opened_at >= %s
              AND opened_at <= %s
            ORDER BY opened_at ASC
        """

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, (window_start, window_end))
        rows = cur.fetchall()

    return [
        Alert(
            alert_id=str(r["alert_id"]),
            endpoint_id=r["endpoint_id"],
            signal_type=r["signal_type"],
            state=r["state"],
            opened_at=r["opened_at"],
            layer=r["layer"],
            suspected_fault=r["suspected_fault"],
            imputation_score=r["imputation_score"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def attribution_correct(fault_type: str, suspected_fault: Optional[str]) -> bool:
    if not suspected_fault:
        return False
    return fault_type in suspected_fault


def match_alerts(
    injections: list[Injection],
    alerts: list[Alert],
) -> tuple[list[MatchResult], list[UnmatchedAlert]]:
    """
    Pour chaque injection, cherche la premiere alerte FIRING dont :
    - endpoint_id == target_endpoint (exact)
    - opened_at dans [injected_at - MATCH_BEFORE, cleared_at + MATCH_AFTER]

    Une alerte ne peut matcher qu'une seule injection.
    Alertes restantes sans match = FP.
    """
    available = list(alerts)  # copie pour consommation
    results: list[MatchResult] = []

    for inj in injections:
        window_start = inj.injected_at - MATCH_BEFORE
        window_end   = inj.cleared_at  + MATCH_AFTER

        matched: Optional[Alert] = None
        for alert in available:
            if alert.endpoint_id != inj.target_endpoint:
                continue
            if alert.opened_at is None:
                continue
            if window_start <= alert.opened_at <= window_end:
                matched = alert
                break

        if matched is not None:
            available.remove(matched)
            delay = (matched.opened_at - inj.injected_at).total_seconds()
            results.append(MatchResult(
                injection=inj,
                alert=matched,
                is_tp=True,
                is_fn=False,
                detection_delay_s=delay,
                attribution_correct=attribution_correct(inj.fault_type, matched.suspected_fault),
            ))
        else:
            results.append(MatchResult(
                injection=inj,
                alert=None,
                is_tp=False,
                is_fn=True,
                detection_delay_s=None,
                attribution_correct=None,
            ))

    unmatched = [UnmatchedAlert(alert=a) for a in available]
    return results, unmatched


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def safe_div(num: float, den: float) -> float:
    return round(num / den, 4) if den > 0 else 0.0


def f1(precision: float, recall: float) -> float:
    denom = precision + recall
    return round(2 * precision * recall / denom, 4) if denom > 0 else 0.0


@dataclass
class GroupMetrics:
    label: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    delays_s: list[float] = field(default_factory=list)
    attribution_hits: int = 0
    attribution_total: int = 0  # TP seulement

    @property
    def precision(self) -> float:
        return safe_div(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float:
        return safe_div(self.tp, self.tp + self.fn)

    @property
    def f1_score(self) -> float:
        return f1(self.precision, self.recall)

    @property
    def median_delay_s(self) -> Optional[float]:
        return round(median(self.delays_s), 1) if self.delays_s else None

    @property
    def attribution_accuracy(self) -> Optional[float]:
        return safe_div(self.attribution_hits, self.attribution_total) if self.attribution_total > 0 else None

    def to_dict(self) -> dict:
        return {
            "TP": self.tp,
            "FP": self.fp,
            "FN": self.fn,
            "precision": self.precision,
            "recall": self.recall,
            "F1": self.f1_score,
            "median_delay_s": self.median_delay_s,
            "attribution_accuracy": self.attribution_accuracy,
        }


def compute_metrics(
    match_results: list[MatchResult],
    unmatched: list[UnmatchedAlert],
) -> dict:
    """
    Retourne un dict avec :
    - summary (global)
    - by_scenario
    - by_fault_type
    - by_layer
    """
    global_m = GroupMetrics(label="global")
    by_scenario: dict[str, GroupMetrics] = {}
    by_fault: dict[str, GroupMetrics] = {}
    by_layer: dict[str, GroupMetrics] = {}

    def get_or_create(store: dict, key: str) -> GroupMetrics:
        if key not in store:
            store[key] = GroupMetrics(label=key)
        return store[key]

    for r in match_results:
        inj = r.injection
        groups = [
            global_m,
            get_or_create(by_scenario, inj.scenario_id),
            get_or_create(by_fault, inj.fault_type),
        ]

        if r.is_tp:
            layer_key = r.alert.layer or "unknown"
            layer_m = get_or_create(by_layer, layer_key)
            all_groups = groups + [layer_m]

            for g in all_groups:
                g.tp += 1
                if r.detection_delay_s is not None:
                    g.delays_s.append(r.detection_delay_s)
                g.attribution_total += 1
                if r.attribution_correct:
                    g.attribution_hits += 1
        else:
            for g in groups:
                g.fn += 1

    for u in unmatched:
        layer_key = u.alert.layer or "unknown"
        layer_m = get_or_create(by_layer, layer_key)
        for g in [global_m, layer_m]:
            g.fp += 1

    return {
        "summary": global_m.to_dict(),
        "by_scenario": {k: v.to_dict() for k, v in sorted(by_scenario.items())},
        "by_fault_type": {k: v.to_dict() for k, v in sorted(by_fault.items())},
        "by_layer": {k: v.to_dict() for k, v in sorted(by_layer.items())},
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

METRIC_COLS = ["TP", "FP", "FN", "precision", "recall", "F1", "median_delay_s", "attribution_accuracy"]

def metrics_table(data: dict[str, dict], title: str) -> str:
    rows = []
    for label, m in data.items():
        rows.append([label] + [m.get(c, "-") for c in METRIC_COLS])
    headers = ["group"] + METRIC_COLS
    return f"\n=== {title} ===\n" + tabulate(rows, headers=headers, tablefmt="simple")


def print_report(report: dict) -> None:
    s = report["summary"]
    print("\n=== GLOBAL SUMMARY ===")
    print(tabulate(
        [[s.get(c, "-") for c in METRIC_COLS]],
        headers=METRIC_COLS,
        tablefmt="simple",
    ))
    print(metrics_table(report["by_scenario"],  "BY SCENARIO"))
    print(metrics_table(report["by_fault_type"], "BY FAULT TYPE"))
    print(metrics_table(report["by_layer"],      "BY LAYER"))


def save_report(report: dict, path: Path) -> None:
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n[INFO] Rapport JSON ecrit dans {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    injections = load_ground_truth(RESULTS_DIR)
    if not injections:
        print("[ERROR] Aucune injection trouvee, arret.", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] {len(injections)} injection(s) chargee(s) depuis {RESULTS_DIR}")

    # Fenetre temporelle globale du test
    window_start = min(i.injected_at for i in injections) - MATCH_BEFORE
    window_end   = max(i.cleared_at  for i in injections) + MATCH_AFTER

    print(f"[INFO] Fenetre d'evaluation : {window_start} -> {window_end}")

    try:
        conn = psycopg2.connect(DB_DSN)
    except Exception as e:
        print(f"[ERROR] Connexion DB : {e}", file=sys.stderr)
        sys.exit(1)

    with conn:
        alerts = load_firing_alerts(conn, window_start, window_end)

    conn.close()
    print(f"[INFO] {len(alerts)} alerte(s) FIRING dans la fenetre")

    match_results, unmatched = match_alerts(injections, alerts)

    tp_count = sum(1 for r in match_results if r.is_tp)
    fn_count = sum(1 for r in match_results if r.is_fn)
    print(f"[INFO] TP={tp_count}  FN={fn_count}  FP={len(unmatched)}")

    report = compute_metrics(match_results, unmatched)
    print_report(report)
    save_report(report, REPORT_PATH)


if __name__ == "__main__":
    main()
