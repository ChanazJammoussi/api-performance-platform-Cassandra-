#!/usr/bin/env python3
"""
evaluation.py - Section 6.4 / spec 9.1-9.3

Matche les alertes de la table `alerts` contre les fenetres d'injection
ground-truth (scenario-runner/results/*.json) et calcule :
  - detection rate (TP / (TP + FN)) par scenario et par fault_type
  - false positive rate (FP total, FP/heure)
  - detection delay median (opened_at - injected_at)
  - attribution accuracy sur les scenarios bad_deploy

Usage:
    python3 evaluation.py                                  # scanne tout results/
    python3 evaluation.py --input results/bad_deploy_eval.json
    python3 evaluation.py --grace-seconds 120 --output report.json
    python3 evaluation.py --dedup-score-threshold 0.0   # comportement pre-seuil
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
from dotenv import find_dotenv, load_dotenv

DEFAULT_RESULTS_DIR = Path("scenario-runner/results")
DEFAULT_GRACE_SECONDS = 120
DEDUP_SCORE_THRESHOLD = 0.90  # seuil imputation_score pour ignorer les alertes siblings
TIME_MARGIN = timedelta(minutes=5)  # marge autour de la fenetre du fichier pour la requete SQL


@dataclass
class InjectionWindow:
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
    opened_at: datetime
    resolved_at: Optional[datetime]
    severity: str
    layer: str
    suspected_fault: Optional[str]
    imputation_score: Optional[float]


@dataclass
class MatchResult:
    window: InjectionWindow
    alert: Optional[Alert]
    delay_seconds: Optional[float]
    attribution_correct: Optional[bool]


@dataclass
class ScenarioReport:
    source_file: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    delays: list = field(default_factory=list)
    attribution_correct: int = 0
    attribution_total: int = 0
    duration_hours: float = 0.0


def parse_timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_injection_windows(path: Path) -> list[InjectionWindow]:
    with open(path) as f:
        data = json.load(f)

    # Accepte soit une liste plate de fautes, soit {"scenario_id": ..., "faults": [...]}.
    if isinstance(data, dict) and "faults" in data:
        scenario_id_default = data.get("scenario_id", path.stem)
        raw_faults = data["faults"]
    elif isinstance(data, list):
        scenario_id_default = path.stem
        raw_faults = data
    else:
        raise ValueError(f"structure JSON non reconnue dans {path.name}")

    windows = []
    for raw in raw_faults:
        windows.append(
            InjectionWindow(
                scenario_id=raw.get("scenario_id", scenario_id_default),
                fault_type=raw["fault_type"],
                target_endpoint=raw["target_endpoint"],
                injected_at=parse_timestamp(raw["injected_at"]),
                cleared_at=parse_timestamp(raw["cleared_at"]),
                magnitude=raw.get("magnitude", {}),
                source_file=path.name,
            )
        )
    return windows


def discover_input_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        return sorted(input_path.glob("*.json"))
    raise FileNotFoundError(f"{input_path} introuvable")


def fetch_alerts(conn, since: datetime, until: datetime) -> list[Alert]:
    # opened_at marque la transition PENDING -> FIRING (spec 5.5) : toute alerte
    # avec opened_at non nul a firing a un moment donne, quel que soit son etat actuel.
    query = """
        SELECT alert_id, endpoint_id, signal_type, state, opened_at,
               resolved_at, severity, layer, suspected_fault, imputation_score
        FROM alerts
        WHERE opened_at IS NOT NULL
          AND opened_at BETWEEN %s AND %s
        ORDER BY opened_at
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, (since, until))
        rows = cur.fetchall()
    return [Alert(**row) for row in rows]


def match_windows_to_alerts(
    windows: list[InjectionWindow], alerts: list[Alert], grace: timedelta,
    dedup_score_threshold: float = DEDUP_SCORE_THRESHOLD,
) -> tuple[list[MatchResult], list[Alert]]:
    unmatched = list(alerts)
    results = []

    for window in sorted(windows, key=lambda w: w.injected_at):
        candidates = [
            a
            for a in unmatched
            if a.endpoint_id == window.target_endpoint
            and window.injected_at <= a.opened_at <= window.cleared_at + grace
        ]
        if not candidates:
            results.append(MatchResult(window, None, None, None))
            continue

        first = min(candidates, key=lambda a: a.opened_at)
        for c in candidates:
            unmatched.remove(c)  # toutes les alertes de la fenetre sont consommees (doublons ignores)

        delay = (first.opened_at - window.injected_at).total_seconds()

        attribution_correct = None
        if window.fault_type == "bad_deploy":
            expected_suspected_fault = f"{window.scenario_id}:{window.fault_type}:{window.target_endpoint}"
            attribution_correct = first.suspected_fault == expected_suspected_fault

        results.append(MatchResult(window, first, delay, attribution_correct))

    # Une alerte sibling (suspected_fault pointe vers une fenetre connue) est ignoree
    # uniquement si son imputation_score >= dedup_score_threshold. Si le seuil est 0.0,
    # tout suspected_fault connu suffit (comportement pre-seuil). Si le score est None
    # ou insuffisant, l'alerte reste comptee comme FP (attribution peu fiable).
    known_faults = {
        f"{w.scenario_id}:{w.fault_type}:{w.target_endpoint}"
        for w in windows
    }
    fp_alerts = [
        a for a in unmatched
        if not (
            a.suspected_fault in known_faults
            and (
                dedup_score_threshold == 0.0
                or (a.imputation_score is not None and a.imputation_score >= dedup_score_threshold)
            )
        )
    ]

    return results, fp_alerts


def evaluate_file(conn, path: Path, grace: timedelta, dedup_score_threshold: float = DEDUP_SCORE_THRESHOLD) -> tuple[ScenarioReport, list[MatchResult]]:
    windows = load_injection_windows(path)
    if not windows:
        return ScenarioReport(source_file=path.name), []

    since = min(w.injected_at for w in windows) - TIME_MARGIN
    until = max(w.cleared_at for w in windows) + grace + TIME_MARGIN
    alerts = fetch_alerts(conn, since, until)

    matches, fp_alerts = match_windows_to_alerts(windows, alerts, grace, dedup_score_threshold)

    report = ScenarioReport(source_file=path.name)
    report.duration_hours = (until - since).total_seconds() / 3600
    report.fp = len(fp_alerts)

    for m in matches:
        if m.alert is not None:
            report.tp += 1
            report.delays.append(m.delay_seconds)
            if m.attribution_correct is not None:
                report.attribution_total += 1
                if m.attribution_correct:
                    report.attribution_correct += 1
        else:
            report.fn += 1

    return report, matches


def summarize(reports: list[ScenarioReport]) -> dict:
    tp = sum(r.tp for r in reports)
    fp = sum(r.fp for r in reports)
    fn = sum(r.fn for r in reports)
    hours = sum(r.duration_hours for r in reports)
    delays = [d for r in reports for d in r.delays]
    attr_correct = sum(r.attribution_correct for r in reports)
    attr_total = sum(r.attribution_total for r in reports)

    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall else None

    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_positives_per_hour": (fp / hours) if hours else None,
        "median_detection_delay_seconds": statistics.median(delays) if delays else None,
        "attribution_accuracy": (attr_correct / attr_total) if attr_total else None,
        "attribution_sample_size": attr_total,
    }


def print_report(reports: list[ScenarioReport], overall: dict) -> None:
    print(f"{'scenario':<35} {'TP':>4} {'FP':>4} {'FN':>4} {'delay_med_s':>12}")
    for r in reports:
        delay_med = f"{statistics.median(r.delays):.1f}" if r.delays else "-"
        print(f"{r.source_file:<35} {r.tp:>4} {r.fp:>4} {r.fn:>4} {delay_med:>12}")

    print()
    print(f"precision                : {fmt(overall['precision'])}")
    print(f"recall                   : {fmt(overall['recall'])}")
    print(f"f1                       : {fmt(overall['f1'])}")
    print(f"FP / heure               : {fmt(overall['false_positives_per_hour'])}")
    print(f"delai detection median   : {fmt(overall['median_detection_delay_seconds'])}s")
    if overall["attribution_sample_size"]:
        print(
            f"attribution accuracy    : {fmt(overall['attribution_accuracy'])} "
            f"(n={overall['attribution_sample_size']})"
        )


def fmt(value) -> str:
    return f"{value:.3f}" if isinstance(value, float) else "-"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_RESULTS_DIR,
        help="Fichier ground-truth unique ou dossier a scanner (defaut: scenario-runner/results/)",
    )
    parser.add_argument(
        "--grace-seconds",
        type=int,
        default=DEFAULT_GRACE_SECONDS,
        help="Fenetre de grace apres cleared_at pour compter un match (defaut: 120s)",
    )
    parser.add_argument("--output", type=Path, help="Chemin JSON optionnel pour le rapport complet")
    parser.add_argument(
        "--dedup-score-threshold",
        type=float,
        default=DEDUP_SCORE_THRESHOLD,
        metavar="SCORE",
        help=(
            "Seuil imputation_score pour ignorer les alertes siblings (defaut: 0.90). "
            "0.0 = desactive le seuil (comportement pre-seuil : tout suspected_fault connu suffit)."
        ),
    )
    args = parser.parse_args()

    load_dotenv(find_dotenv())
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL absent de l'environnement (.env)", file=sys.stderr)
        return 1

    files = discover_input_files(args.input)
    if not files:
        print(f"aucun fichier ground-truth trouve dans {args.input}", file=sys.stderr)
        return 1

    grace = timedelta(seconds=args.grace_seconds)
    reports = []
    all_matches = []

    conn = psycopg2.connect(database_url)
    try:
        for path in files:
            try:
                report, matches = evaluate_file(conn, path, grace, args.dedup_score_threshold)
            except (KeyError, ValueError) as exc:
                print(f"skip {path.name}: {exc}", file=sys.stderr)
                continue
            reports.append(report)
            all_matches.extend(matches)
    finally:
        conn.close()

    overall = summarize(reports)
    print_report(reports, overall)

    if args.output:
        payload = {
            "grace_seconds": args.grace_seconds,
            "overall": overall,
            "per_scenario": [
                {
                    "source_file": r.source_file,
                    "tp": r.tp,
                    "fp": r.fp,
                    "fn": r.fn,
                    "median_delay_seconds": statistics.median(r.delays) if r.delays else None,
                    "attribution_accuracy": (
                        r.attribution_correct / r.attribution_total if r.attribution_total else None
                    ),
                }
                for r in reports
            ],
        }
        args.output.write_text(json.dumps(payload, indent=2))
        print(f"\nrapport ecrit dans {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
