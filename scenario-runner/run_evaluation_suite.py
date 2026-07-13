#!/usr/bin/env python3
"""
run_evaluation_suite.py — Framework de benchmarking multi-scénarios.

Génère une campagne dans results/campaign_<YYYYMMDD_HHMM>/ avec :
  manifest.json, summary.json, summary.csv, benchmark_summary.md
  logs/<scenario>_core.log, <scenario>_stress.log
  ground_truth/<scenario>_core_<ts>.json, <scenario>_stress_<ts>.json

Usage:
    python run_evaluation_suite.py [options]
    python run_evaluation_suite.py --resume
    python run_evaluation_suite.py --continue-on-error
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import psycopg2
import yaml
from dotenv import find_dotenv, load_dotenv

# ── Chemins de base ──────────────────────────────────────────────────────────
SUITE_DIR     = Path(__file__).parent.resolve()
CASSANDRA_DIR = SUITE_DIR.parent
DETECTION_DIR = CASSANDRA_DIR / "detection-service"
RUNNER        = SUITE_DIR / "runner.py"
EVALUATION    = DETECTION_DIR / "evaluation.py"

sys.path.insert(0, str(SUITE_DIR))
from runner import PROMETHEUS_RESIDUE_WINDOW  # noqa: E402  (source de vérité unique)

GUARD_POLL    = 30   # secondes entre sondages de la garde DB
GUARD_TIMEOUT = 600  # secondes max avant abandon de la garde DB


# ── Utilitaires de base ──────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"[{utc_now().strftime('%H:%M:%S UTC')}] {msg}", flush=True)


def iso(dt: datetime) -> str:
    return dt.isoformat()


# ── Config depuis cassandra/.env ─────────────────────────────────────────────

def load_config() -> dict:
    env_file = CASSANDRA_DIR / ".env"
    if env_file.exists():
        load_dotenv(env_file)
    else:
        load_dotenv(find_dotenv())

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERREUR : DATABASE_URL absent du .env", file=sys.stderr)
        sys.exit(1)

    pw = os.environ.get("PENDING_WINDOWS")
    if pw is None:
        print("AVERTISSEMENT : PENDING_WINDOWS absent du .env → défaut 2", file=sys.stderr)
        pending_windows = 2
    else:
        pending_windows = int(pw)

    si = os.environ.get("SCRAPE_INTERVAL")
    if si is None:
        print("AVERTISSEMENT : SCRAPE_INTERVAL absent du .env → défaut 60s", file=sys.stderr)
        scrape_interval = 60
    else:
        scrape_interval = int(si)

    return {
        "database_url": db_url,
        "pending_windows": pending_windows,
        "scrape_interval": scrape_interval,
    }


def db_conn(config: dict):
    return psycopg2.connect(config["database_url"])


# ── Découverte des scénarios ─────────────────────────────────────────────────

def discover_scenarios(scenarios_dir: Path) -> list[Path]:
    """Fichiers .yaml triés alphabétiquement, quiet_baseline exclu."""
    return [p for p in sorted(scenarios_dir.glob("*.yaml")) if p.stem != "quiet_baseline"]


def yaml_max_duration(scenario_path: Path) -> int:
    """Durée max (en secondes) sur toutes les faults du YAML."""
    with open(scenario_path) as f:
        data = yaml.safe_load(f)
    durations = [fault.get("duration_seconds", 60) for fault in data.get("faults", [])]
    return max(durations) if durations else 60


# ── Vérification des processus ───────────────────────────────────────────────

def check_processes() -> bool:
    result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    out = result.stdout
    all_ok = True
    for name in ["scraper.py", "detector.py", "k6"]:
        ok = name in out
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}")
        if not ok:
            all_ok = False
    return all_ok


# ── Garde DB ─────────────────────────────────────────────────────────────────

def get_dirty_rows(config: dict) -> list[tuple]:
    """Retourne les lignes error_rate_5xx qui ne sont pas au repos (ok, pending_count=0)."""
    conn = db_conn(config)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT endpoint_id, signal_type, state, pending_count
            FROM alerts
            WHERE signal_type = 'error_rate_5xx'
              AND (state IN ('firing', 'pending', 'resolving') OR pending_count > 0)
            ORDER BY endpoint_id
        """)
        return cur.fetchall()
    finally:
        conn.close()


def wait_db_clean(config: dict) -> bool:
    deadline = time.time() + GUARD_TIMEOUT
    while time.time() < deadline:
        dirty = get_dirty_rows(config)
        if not dirty:
            return True
        desc = "  ".join(f"{ep}/{sig}={st}(p={pc})" for ep, sig, st, pc in dirty)
        log(f"  [WAIT] DB : {desc}")
        time.sleep(GUARD_POLL)
    log(f"  [FAIL] DB guard timeout après {GUARD_TIMEOUT}s")
    return False


# ── Préconditions ────────────────────────────────────────────────────────────

def check_preconditions(
    config: dict,
    last_cleared_at: Optional[datetime],
    prometheus_window: int,
) -> bool:
    """
    Vérifie et affiche toutes les préconditions avant un run.
    Attend automatiquement pour les conditions temporelles.
    Retourne False uniquement si les processus manquent ou si la garde DB expire.
    """
    all_ok = True

    # Processus
    if not check_processes():
        all_ok = False

    # Fenêtre Prometheus
    if last_cleared_at is None:
        print("  [OK  ] Prometheus window (pas de run précédent dans cette campagne)")
    else:
        wait_until = last_cleared_at + timedelta(seconds=prometheus_window)
        remaining = (wait_until - utc_now()).total_seconds()
        if remaining <= 0:
            elapsed = abs(remaining)
            print(f"  [OK  ] Prometheus window expirée ({elapsed:.0f}s de marge)")
        else:
            cleared_hms = last_cleared_at.strftime("%H:%M:%S")
            wait_hms = wait_until.strftime("%H:%M:%S")
            log(f"  [WAIT] Prometheus window : {remaining:.0f}s restantes "
                f"(cleared_at={cleared_hms} + {prometheus_window}s = {wait_hms} UTC)")
            time.sleep(remaining)
            print("  [OK  ] Prometheus window expirée")

    # Garde DB
    dirty = get_dirty_rows(config)
    if not dirty:
        print("  [OK  ] DB : aucune alerte FIRING/PENDING/RESOLVING")
    else:
        desc = "  ".join(f"{ep}/{sig}={st}(p={pc})" for ep, sig, st, pc in dirty)
        log(f"  [WAIT] DB : {desc}")
        if wait_db_clean(config):
            log("  [OK  ] DB propre")
        else:
            all_ok = False

    return all_ok


# ── Runner ───────────────────────────────────────────────────────────────────

def run_runner(
    scenario_path: Path,
    output_file: Path,
    duration_override: Optional[int],
    timeout: int,
) -> tuple[bool, str]:
    cmd = [sys.executable, str(RUNNER), str(scenario_path), "--output", str(output_file)]
    if duration_override is not None:
        cmd += ["--duration-override", str(duration_override)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        stderr_section = ("\n--- STDERR ---\n" + result.stderr) if result.stderr.strip() else ""
        return result.returncode == 0, result.stdout + stderr_section
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT après {timeout}s\n"


# ── Ground truth ─────────────────────────────────────────────────────────────

def verify_ground_truth(gt_file: Path) -> tuple[bool, str]:
    if not gt_file.exists():
        return False, f"fichier absent : {gt_file.name}"
    try:
        with open(gt_file) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return False, f"JSON invalide : {e}"
    faults = data.get("faults", []) if isinstance(data, dict) else data
    if not faults:
        return False, "aucune fault enregistrée"
    missing = [i for i, fault in enumerate(faults) if "cleared_at" not in fault]
    if missing:
        return False, f"cleared_at absent sur fault(s) {missing}"
    return True, f"{len(faults)} fault(s) vérifiée(s)"


def cleared_at_from_gt(gt_file: Path) -> Optional[datetime]:
    with open(gt_file) as f:
        data = json.load(f)
    faults = data.get("faults", []) if isinstance(data, dict) else data
    times = [
        datetime.fromisoformat(f["cleared_at"])
        for f in faults
        if "cleared_at" in f
    ]
    return max(times) if times else None


# ── Évaluation ───────────────────────────────────────────────────────────────

def run_evaluation(
    gt_file: Path,
    eval_json: Path,
) -> tuple[Optional[dict], str]:
    cmd = [
        sys.executable, str(EVALUATION),
        "--input", str(gt_file),
        "--output", str(eval_json),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        cwd=str(DETECTION_DIR),
    )
    stderr_section = ("\n--- STDERR ---\n" + result.stderr) if result.stderr.strip() else ""
    content = result.stdout + stderr_section
    print(content, end="", flush=True)

    if not eval_json.exists():
        return None, content
    try:
        with open(eval_json) as f:
            return json.load(f), content
    except Exception:
        return None, content


# ── Commit SHA ───────────────────────────────────────────────────────────────

def get_commit_sha() -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True,
            cwd=str(CASSANDRA_DIR),
        )
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


# ── Sorties ───────────────────────────────────────────────────────────────────

_CSV_FIELDS = [
    "scenario", "tier", "duration_yaml_s", "duration_effective_s",
    "tp", "fp", "fn", "precision", "recall", "f1",
    "fp_per_hour", "delay_median_s", "status", "error",
]


def _fmt(v) -> str:
    if v is None:
        return "-"
    return f"{v:.3f}" if isinstance(v, float) else str(v)


def write_summary_json(campaign_dir: Path, entries: list[dict]) -> None:
    (campaign_dir / "summary.json").write_text(json.dumps(entries, indent=2))


def write_summary_csv(campaign_dir: Path, entries: list[dict]) -> None:
    with open(campaign_dir / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for e in entries:
            w.writerow({k: e.get(k, "") for k in _CSV_FIELDS})


def write_benchmark_md(
    campaign_dir: Path,
    campaign_id: str,
    config: dict,
    args: argparse.Namespace,
    entries: list[dict],
) -> None:
    lines = [
        f"# Benchmark Campaign — {campaign_id}",
        "",
        f"**Config**: PENDING_WINDOWS={config['pending_windows']}, "
        f"SCRAPE_INTERVAL={config['scrape_interval']}s, "
        f"min_core_duration={args.min_core_duration}s, "
        f"prometheus_window={args.prometheus_window}s",
        "",
        "| Scenario | Tier | Duration (s) | TP | FP | FN | Precision | Recall | F1 | FP/h | Delay (s) | Status |",
        "|----------|------|-------------|----|----|----|-----------|----|-------|------|-----------|--------|",
    ]
    for e in entries:
        if e.get("status") == "error":
            lines.append(
                f"| {e.get('scenario','')} | {e.get('tier','')} "
                "| - | - | - | - | - | - | - | - | - | ERROR |"
            )
        else:
            lines.append(
                f"| {e.get('scenario','')} | {e.get('tier','')} "
                f"| {e.get('duration_effective_s', '-')} "
                f"| {e.get('tp', '-')} | {e.get('fp', '-')} | {e.get('fn', '-')} "
                f"| {_fmt(e.get('precision'))} | {_fmt(e.get('recall'))} "
                f"| {_fmt(e.get('f1'))} | {_fmt(e.get('fp_per_hour'))} "
                f"| {_fmt(e.get('delay_median_s'))} | {e.get('status', '?')} |"
            )
    lines.append("")
    (campaign_dir / "benchmark_summary.md").write_text("\n".join(lines))


def flush_summaries(
    campaign_dir: Path,
    campaign_id: str,
    config: dict,
    args: argparse.Namespace,
    entries: list[dict],
) -> None:
    write_summary_json(campaign_dir, entries)
    write_summary_csv(campaign_dir, entries)
    write_benchmark_md(campaign_dir, campaign_id, config, args, entries)


# ── Suite principale ──────────────────────────────────────────────────────────

class BenchmarkSuite:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config = load_config()
        self.ts = utc_now().strftime("%Y%m%d_%H%M")
        self.campaign_id = f"campaign_{self.ts}"
        self.results_dir = Path(args.results_dir).resolve()
        self.campaign_dir = self.results_dir / self.campaign_id
        self.entries: list[dict] = []
        self.last_cleared_at: Optional[datetime] = None

    # ── Setup ──────────────────────────────────────────────────────────────────

    def _setup_dirs(self) -> None:
        for sub in ("", "logs", "ground_truth"):
            (self.campaign_dir / sub).mkdir(parents=True, exist_ok=True)

    def _load_resume(self) -> None:
        existing = sorted(self.results_dir.glob("campaign_*/summary.json"), reverse=True)
        if not existing:
            log("[RESUME] Aucune campagne existante — démarrage à zéro")
            return
        latest_json = existing[0]
        with open(latest_json) as f:
            self.entries = json.load(f)
        self.campaign_dir = latest_json.parent
        self.campaign_id = self.campaign_dir.name
        ok_count = sum(1 for e in self.entries if e.get("status") in ("ok", "ok_no_metrics"))
        log(f"[RESUME] Reprise de {self.campaign_id} "
            f"({len(self.entries)} entrées, {ok_count} succès)")

    def _write_manifest(self, scenarios: list[Path]) -> dict:
        manifest = {
            "campaign_id": self.campaign_id,
            "started_at": iso(utc_now()),
            "commit_sha": get_commit_sha(),
            "pending_windows": self.config["pending_windows"],
            "scrape_interval": self.config["scrape_interval"],
            "min_core_duration": self.args.min_core_duration,
            "prometheus_window": self.args.prometheus_window,
            "scenarios": [p.stem for p in scenarios],
        }
        (self.campaign_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        return manifest

    # ── Exécution d'un tier (core ou stress) ───────────────────────────────────

    def _already_done(self, scenario_name: str, tier: str) -> bool:
        return any(
            e["scenario"] == scenario_name
            and e["tier"] == tier
            and e.get("status") in ("ok", "ok_no_metrics")
            for e in self.entries
        )

    def _run_tier(self, scenario_path: Path, tier: str, duration_yaml_s: int) -> None:
        name = scenario_path.stem
        is_core = tier == "core"
        prom_window = self.args.prometheus_window

        duration_effective = (
            max(duration_yaml_s, self.args.min_core_duration) if is_core else duration_yaml_s
        )
        duration_override: Optional[int] = (
            duration_effective if (is_core and duration_yaml_s < self.args.min_core_duration) else None
        )

        tier_label = (
            f"{duration_effective}s (override depuis {duration_yaml_s}s)"
            if duration_override else f"{duration_yaml_s}s (YAML)"
        )

        print()
        print(f"{'─'*60}")
        print(f"  {name.upper()}  ·  {tier.upper()}  ·  {tier_label}")
        print(f"{'─'*60}")

        # ── Préconditions ────────────────────────────────────────────────────
        print()
        log("Préconditions :")
        prec_ok = check_preconditions(self.config, self.last_cleared_at, prom_window)
        if not prec_ok:
            msg = f"préconditions échouées pour {name}/{tier}"
            log(f"[FAIL] {msg}")
            self._record_error(name, tier, duration_yaml_s, duration_effective, msg)
            if not self.args.continue_on_error:
                raise RuntimeError(msg)
            return

        # ── Chemins ──────────────────────────────────────────────────────────
        gt_file   = self.campaign_dir / "ground_truth" / f"{name}_{tier}_{self.ts}.json"
        log_file  = self.campaign_dir / "logs" / f"{name}_{tier}.log"
        eval_json = self.campaign_dir / "logs" / f"{name}_{tier}_eval.json"

        started_at = utc_now()
        log_parts: list[str] = []

        # ── Runner ───────────────────────────────────────────────────────────
        print()
        override_note = f" --duration-override {duration_override}" if duration_override else ""
        log(f"runner.py {scenario_path.name}{override_note} → {gt_file.name}")
        success, runner_log = run_runner(scenario_path, gt_file, duration_override, self.args.timeout)
        log_parts.append(runner_log)

        if not success:
            msg = f"runner.py a échoué (voir {log_file.name})"
            log(f"[FAIL] {msg}")
            log_file.write_text("".join(log_parts))
            self._record_error(name, tier, duration_yaml_s, duration_effective, msg, started_at)
            if not self.args.continue_on_error:
                raise RuntimeError(msg)
            return

        # ── Vérification JSON ─────────────────────────────────────────────
        gt_ok, gt_msg = verify_ground_truth(gt_file)
        log(f"  [{'OK  ' if gt_ok else 'FAIL'}] ground_truth : {gt_msg}")
        if not gt_ok:
            log_file.write_text("".join(log_parts))
            self._record_error(name, tier, duration_yaml_s, duration_effective, f"ground_truth : {gt_msg}", started_at)
            if not self.args.continue_on_error:
                raise RuntimeError(gt_msg)
            return

        cleared_at = cleared_at_from_gt(gt_file)

        # ── Évaluation IMMÉDIATE ─────────────────────────────────────────────
        print()
        log(f"evaluation.py --input {gt_file.name}")
        metrics, eval_log = run_evaluation(gt_file, eval_json)
        log_parts.append("\n--- EVALUATION ---\n" + eval_log)
        log_file.write_text("".join(log_parts))

        # ── Entrée summary ────────────────────────────────────────────────────
        entry: dict = {
            "scenario": name,
            "tier": tier,
            "duration_yaml_s": duration_yaml_s,
            "duration_effective_s": duration_effective,
            "ground_truth_file": gt_file.name,
            "started_at": iso(started_at),
            "cleared_at": iso(cleared_at) if cleared_at else None,
            "evaluated_at": iso(utc_now()),
            "status": "ok",
        }
        if metrics:
            ov = metrics.get("overall", {})
            entry.update({
                "tp":             ov.get("tp"),
                "fp":             ov.get("fp"),
                "fn":             ov.get("fn"),
                "precision":      ov.get("precision"),
                "recall":         ov.get("recall"),
                "f1":             ov.get("f1"),
                "fp_per_hour":    ov.get("false_positives_per_hour"),
                "delay_median_s": ov.get("median_detection_delay_seconds"),
            })
        else:
            log("[WARN] métriques d'évaluation non disponibles")
            entry["status"] = "ok_no_metrics"

        self.entries.append(entry)
        flush_summaries(self.campaign_dir, self.campaign_id, self.config, self.args, self.entries)

        if cleared_at:
            self.last_cleared_at = cleared_at

    def _record_error(
        self,
        name: str,
        tier: str,
        duration_yaml_s: int,
        duration_effective_s: int,
        msg: str,
        started_at: Optional[datetime] = None,
    ) -> None:
        self.entries.append({
            "scenario": name,
            "tier": tier,
            "duration_yaml_s": duration_yaml_s,
            "duration_effective_s": duration_effective_s,
            "started_at": iso(started_at or utc_now()),
            "status": "error",
            "error": msg,
        })
        flush_summaries(self.campaign_dir, self.campaign_id, self.config, self.args, self.entries)

    # ── Exécution d'un scénario (core puis stress) ────────────────────────────

    def _run_scenario(self, scenario_path: Path) -> None:
        name = scenario_path.stem
        duration_yaml = yaml_max_duration(scenario_path)

        for tier in ("core", "stress"):
            if self.args.resume and self._already_done(name, tier):
                log(f"[SKIP] {name}/{tier} déjà complété (--resume)")
                continue
            self._run_tier(scenario_path, tier, duration_yaml)

    # ── Point d'entrée ────────────────────────────────────────────────────────

    def run(self) -> None:
        if self.args.resume:
            self._load_resume()

        self._setup_dirs()
        scenarios = discover_scenarios(Path(self.args.scenarios_dir))

        manifest_path = self.campaign_dir / "manifest.json"
        if manifest_path.exists():
            with open(manifest_path) as f:
                manifest = json.load(f)
        else:
            manifest = self._write_manifest(scenarios)

        print()
        print("=" * 65)
        print(f"  CAMPAGNE    {self.campaign_id}")
        print(f"  commit      {manifest['commit_sha'][:12]}")
        print(f"  scénarios   {len(scenarios)} (quiet_baseline exclu)")
        print(f"  config      PENDING_WINDOWS={self.config['pending_windows']}  "
              f"SCRAPE_INTERVAL={self.config['scrape_interval']}s")
        print(f"  params      min_core={self.args.min_core_duration}s  "
              f"prom_window={self.args.prometheus_window}s  "
              f"timeout={self.args.timeout}s")
        print(f"  dossier     {self.campaign_dir}")
        print("=" * 65)

        for scenario_path in scenarios:
            try:
                self._run_scenario(scenario_path)
            except RuntimeError as exc:
                log(f"[ABORT] {exc}")
                sys.exit(1)

        self._print_final_verification(scenarios)

    # ── Vérification finale ───────────────────────────────────────────────────

    def _print_final_verification(self, scenarios: list[Path]) -> None:
        expected = len(scenarios) * 2
        ok_entries   = [e for e in self.entries if e.get("status") in ("ok", "ok_no_metrics")]
        err_entries  = [e for e in self.entries if e.get("status") == "error"]
        complete = len(ok_entries) == expected

        gt_count  = len(list((self.campaign_dir / "ground_truth").glob("*.json")))
        log_count = len(list((self.campaign_dir / "logs").glob("*.log")))

        print()
        print("=" * 65)
        print("  VÉRIFICATION FINALE")
        print("=" * 65)
        print(f"  Scénarios attendus      : {len(scenarios)}")
        print(f"  Runs attendus (×2)      : {expected}")
        print(f"  Runs exécutés           : {len(self.entries)}")
        print(f"  Succès                  : {len(ok_entries)}")
        print(f"  Erreurs                 : {len(err_entries)}")
        print(f"  Campagne complète       : {'OUI ✓' if complete else 'NON ✗'}")
        print()
        print(f"  Fichiers ground_truth   : {gt_count} / {expected}")
        print(f"  Fichiers logs           : {log_count}")
        print()
        print(f"  Artefacts de campagne :")
        for fname in ("summary.json", "summary.csv", "benchmark_summary.md", "manifest.json"):
            exists = (self.campaign_dir / fname).exists()
            print(f"    {'[OK]' if exists else '[--]'}  {fname}")
        print(f"  Dossier : {self.campaign_dir}")
        print("=" * 65)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Framework de benchmarking multi-scénarios — Cassandra detection-service."
    )
    p.add_argument(
        "--scenarios-dir",
        default=str(SUITE_DIR / "scenarios"),
        metavar="DIR",
        help="Dossier contenant les fichiers .yaml (défaut: scenarios/)",
    )
    p.add_argument(
        "--results-dir",
        default=str(SUITE_DIR / "results"),
        metavar="DIR",
        help="Dossier racine pour les campagnes (défaut: results/)",
    )
    p.add_argument(
        "--min-core-duration",
        type=int,
        default=300,
        metavar="SECONDS",
        help="Durée minimale du run core en secondes (défaut: 300)",
    )
    p.add_argument(
        "--prometheus-window",
        type=int,
        default=300,
        metavar="SECONDS",
        help="Fenêtre d'isolation Prometheus entre runs (défaut: 300)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=1800,
        metavar="SECONDS",
        help="Timeout max par runner.py (défaut: 1800)",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Reprend la campagne la plus récente en sautant les runs déjà complétés",
    )
    p.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue vers le scénario suivant en cas d'échec (défaut: abort)",
    )
    return p


if __name__ == "__main__":
    args = _build_parser().parse_args()
    BenchmarkSuite(args).run()
