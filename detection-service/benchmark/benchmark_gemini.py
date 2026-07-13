"""
benchmark_gemini.py -- compare gemini-3.1-flash-lite / gemini-3.5-flash sur 5 scenarios SRE.
LLM Judge Anthropic optionnel : evalue la qualite de chaque reponse Gemini (claude-sonnet-4-6).

Usage:
    cd /mnt/c/Users/chana/cassandra/detection-service
    source venv/bin/activate
    python benchmark/benchmark_gemini.py --output benchmark/results.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# -- google-genai availability check ----------------------------------------

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    print("pip install google-genai")
    sys.exit(1)

# -- anthropic (optionnel) ---------------------------------------------------

try:
    import anthropic as _anthropic_module
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

from prompt_builder import SCENARIOS, build_prompt

# ---------------------------------------------------------------------------

MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
]

DEFAULT_RUNS = 5

_ENV_PATH = Path("/mnt/c/Users/chana/cassandra/.env")
_JUDGE_MODEL = "claude-sonnet-4-6"

_JUDGE_PROMPT = """Tu es un LLM Judge evaluant la qualite d'une explication SRE generee par un modele IA.

Scenario d'alerte :
{scenario_json}

Explication generee :
{explanation_json}

Evalue l'explication sur 4 criteres, chacun note de 0 a 3 :

- correctness (0-3) : Le summary mentionne-t-il les chiffres cles (valeur observee, seuil SLO, baseline) avec exactitude ?
  0=absent ou faux  1=partiel  2=correct mais incomplet  3=complet et exact

- quality (0-3) : L'explication est-elle claire, concise et actionnable dans son ensemble ?
  0=incomprehensible  1=passable  2=bon  3=excellent

- checks_actionability (0-3) : Les checks sont-ils des actions concretes plutot que des generalites ?
  0=tous generaux  1=un seul concret  2=la plupart concrets  3=tous concrets et specifiques

- cause_calibration (0-3) : La cause est-elle formulee avec le bon niveau de confiance par rapport a imputation_score ?
  Si score >= 0.9 : affirmation possible. Si 0.5-0.9 : prudence. Si < 0.5 ou null : forte incertitude requise.
  0=calibration incorrecte  1=approximative  2=bonne  3=parfaite

Reponds UNIQUEMENT avec un objet JSON valide, sans texte avant ou apres :
{{"correctness": <0-3>, "quality": <0-3>, "checks_actionability": <0-3>, "cause_calibration": <0-3>, "comment": "<une phrase max>"}}
"""

# ---------------------------------------------------------------------------


def _load_api_keys() -> tuple[str, str | None]:
    """Charge GEMINI_API_KEY (obligatoire) et ANTHROPIC_API_KEY (optionnelle)."""
    dotenv_path = find_dotenv(str(_ENV_PATH), usecwd=False) or str(_ENV_PATH)
    load_dotenv(dotenv_path)

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        print(f"Erreur : GEMINI_API_KEY absent de {_ENV_PATH}", file=sys.stderr)
        sys.exit(1)

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip() or None
    if not anthropic_key:
        print("WARNING: ANTHROPIC_API_KEY absent -- LLM Judge desactive", file=sys.stderr)

    return gemini_key, anthropic_key


def _build_judge_client(anthropic_key: str | None):
    """Retourne un client Anthropic ou None si indisponible."""
    if not _ANTHROPIC_AVAILABLE:
        print("WARNING: package 'anthropic' non installe -- LLM Judge desactive", file=sys.stderr)
        return None
    if not anthropic_key:
        return None
    return _anthropic_module.Anthropic(api_key=anthropic_key)


def _available_models(client) -> set[str]:
    """Retourne les noms de modeles qui supportent generateContent."""
    try:
        pages = client.models.list()
        names = set()
        for m in pages:
            name = getattr(m, "name", "") or ""
            names.add(name)
            if name.startswith("models/"):
                names.add(name[len("models/"):])
        return names
    except Exception as exc:
        print(f"WARNING: impossible de lister les modeles ({exc})", file=sys.stderr)
        return set()


def _filter_models(requested: list[str], client) -> list[str]:
    available = _available_models(client)
    approved = []
    for m in requested:
        if not available or m in available:
            approved.append(m)
        else:
            print(f"WARNING: modele '{m}' non disponible -- ignore", file=sys.stderr)
    return approved


# ---------------------------------------------------------------------------


def _strip_markdown_fences(text: str) -> str:
    """Supprime les balises ```json ... ``` que le modele insere parfois."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _validate_explanation(data: dict) -> bool:
    """Valide le contrat JSON minimal : summary, suspected_cause, checks."""
    if not isinstance(data, dict):
        return False
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        return False
    if not isinstance(data.get("suspected_cause"), str) or not data["suspected_cause"].strip():
        return False
    checks = data.get("checks")
    if not isinstance(checks, list) or len(checks) == 0:
        return False
    return all(isinstance(c, str) and c.strip() for c in checks)


def evaluate_with_judge(scenario: dict, explanation: dict | None, client) -> dict | None:
    """Evalue la reponse Gemini via Claude (LLM Judge). Retourne le score ou None."""
    if client is None or explanation is None:
        return None
    try:
        prompt = _JUDGE_PROMPT.format(
            scenario_json=json.dumps(scenario, indent=2, ensure_ascii=False),
            explanation_json=json.dumps(explanation, indent=2, ensure_ascii=False),
        )
        response = client.messages.create(
            model=_JUDGE_MODEL,
            max_tokens=256,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text if response.content else ""
        cleaned = _strip_markdown_fences(raw)
        data = json.loads(cleaned)

        required = {"correctness", "quality", "checks_actionability", "cause_calibration", "comment"}
        if not required.issubset(data.keys()):
            return None
        for k in ("correctness", "quality", "checks_actionability", "cause_calibration"):
            if not isinstance(data[k], (int, float)) or not (0 <= data[k] <= 3):
                return None
        return {
            "correctness": int(data["correctness"]),
            "quality": int(data["quality"]),
            "checks_actionability": int(data["checks_actionability"]),
            "cause_calibration": int(data["cause_calibration"]),
            "comment": str(data.get("comment", "")),
        }
    except Exception:
        return None


def _run_once(
    gemini_client,
    model: str,
    prompt: str,
    scenario: dict,
    judge_client,
) -> dict:
    """Execute un appel Gemini, valide la reponse et appelle le judge si disponible."""
    t0 = time.perf_counter()
    try:
        response = gemini_client.models.generate_content(
            model=model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(temperature=0),
        )
        latency_ms = (time.perf_counter() - t0) * 1000

        raw_text = response.text or ""
        input_tokens = getattr(response.usage_metadata, "prompt_token_count", None)
        output_tokens = getattr(response.usage_metadata, "candidates_token_count", None)

        cleaned = _strip_markdown_fences(raw_text)
        valid_json = False
        valid_schema = False
        parsed = None

        try:
            parsed = json.loads(cleaned)
            valid_json = True
            valid_schema = _validate_explanation(parsed)
        except json.JSONDecodeError:
            pass

        judge_score = evaluate_with_judge(
            scenario,
            parsed if valid_schema else None,
            judge_client,
        )

        return {
            "latency_ms": round(latency_ms, 1),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "valid_json": valid_json,
            "valid_schema": valid_schema,
            "raw_response": raw_text,
            "parsed": parsed,
            "error": None,
            "judge_score": judge_score,
        }

    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        return {
            "latency_ms": round(latency_ms, 1),
            "input_tokens": None,
            "output_tokens": None,
            "valid_json": False,
            "valid_schema": False,
            "raw_response": None,
            "parsed": None,
            "error": str(exc),
            "judge_score": None,
        }


# ---------------------------------------------------------------------------


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(int(len(s) * p), len(s) - 1)
    return round(s[idx], 1)


def _mean_or_none(values: list) -> float | None:
    return round(statistics.mean(values), 3) if values else None


def _aggregate(runs: list[dict]) -> dict:
    latencies = [r["latency_ms"] for r in runs if r["error"] is None]
    in_tok = [r["input_tokens"] for r in runs if r["input_tokens"] is not None]
    out_tok = [r["output_tokens"] for r in runs if r["output_tokens"] is not None]

    scored = [r["judge_score"] for r in runs if r.get("judge_score") is not None]

    def _score_mean(key: str) -> float | None:
        return _mean_or_none([s[key] for s in scored])

    totals = [
        s["correctness"] + s["quality"] + s["checks_actionability"] + s["cause_calibration"]
        for s in scored
    ]

    return {
        "n": len(runs),
        "errors": sum(1 for r in runs if r["error"]),
        "json_valid_rate": sum(r["valid_json"] for r in runs) / len(runs),
        "schema_valid_rate": sum(r["valid_schema"] for r in runs) / len(runs),
        "latency_mean_ms": round(statistics.mean(latencies), 1) if latencies else None,
        "latency_median_ms": round(statistics.median(latencies), 1) if latencies else None,
        "latency_p90_ms": _percentile(latencies, 0.9),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "input_tokens_mean": _mean_or_none(in_tok),
        "output_tokens_mean": _mean_or_none(out_tok),
        "score_correctness_mean": _score_mean("correctness"),
        "score_quality_mean": _score_mean("quality"),
        "score_checks_mean": _score_mean("checks_actionability"),
        "score_calibration_mean": _score_mean("cause_calibration"),
        "score_total_mean": _mean_or_none(totals),
    }


def _print_summary(results: list[dict]) -> None:
    col_w = [28, 22, 6, 6, 12, 12, 12, 10, 10, 11]
    header = [
        "model", "scenario", "json%", "sch%",
        "lat_mean_ms", "lat_p50_ms", "lat_p90_ms",
        "in_tok", "out_tok", "judge_tot",
    ]
    sep = "  ".join("-" * w for w in col_w)
    print()
    print("  ".join(f"{h:<{w}}" for h, w in zip(header, col_w)))
    print(sep)
    for entry in results:
        agg = entry["aggregate"]

        def _pct(v: float | None) -> str:
            return f"{v * 100:.0f}%" if v is not None else "-"

        def _f(v: float | None) -> str:
            return f"{v:.1f}" if v is not None else "-"

        def _score(v: float | None) -> str:
            return f"{v:.2f}/12" if v is not None else "-"

        row = [
            entry["model"],
            entry["scenario_name"],
            _pct(agg["json_valid_rate"]),
            _pct(agg["schema_valid_rate"]),
            _f(agg["latency_mean_ms"]),
            _f(agg["latency_median_ms"]),
            _f(agg["latency_p90_ms"]),
            _f(agg["input_tokens_mean"]),
            _f(agg["output_tokens_mean"]),
            _score(agg["score_total_mean"]),
        ]
        print("  ".join(f"{v:<{w}}" for v, w in zip(row, col_w)))
    print()


def _write_csv(results: list[dict], output_path: Path) -> None:
    """Ecrit summary.csv avec une ligne par modele (agregat sur tous les scenarios)."""
    by_model: dict[str, list[dict]] = {}
    for entry in results:
        by_model.setdefault(entry["model"], []).append(entry)

    fieldnames = [
        "model",
        "latency_mean_s", "latency_p95_s",
        "input_tokens_mean", "output_tokens_mean",
        "json_valid_rate",
        "score_correctness_mean", "score_quality_mean",
        "score_checks_mean", "score_calibration_mean",
        "score_total_mean",
    ]

    rows = []
    for model, entries in by_model.items():
        all_runs = [r for e in entries for r in e["runs"]]
        agg = _aggregate(all_runs)

        def _s(v: float | None) -> str:
            return f"{v / 1000:.3f}" if v is not None else ""

        def _fmt(v: float | None) -> str:
            return f"{v:.3f}" if v is not None else ""

        rows.append({
            "model": model,
            "latency_mean_s": _s(agg["latency_mean_ms"]),
            "latency_p95_s": _s(agg["latency_p95_ms"]),
            "input_tokens_mean": _fmt(agg["input_tokens_mean"]),
            "output_tokens_mean": _fmt(agg["output_tokens_mean"]),
            "json_valid_rate": _fmt(agg["json_valid_rate"]),
            "score_correctness_mean": _fmt(agg["score_correctness_mean"]),
            "score_quality_mean": _fmt(agg["score_quality_mean"]),
            "score_checks_mean": _fmt(agg["score_checks_mean"]),
            "score_calibration_mean": _fmt(agg["score_calibration_mean"]),
            "score_total_mean": _fmt(agg["score_total_mean"]),
        })

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Chemin JSON pour les resultats complets (ex: benchmark/results.json)",
    )
    parser.add_argument(
        "--model", default=None,
        help="Executer uniquement ce modele (ex: gemini-2.5-flash)",
    )
    parser.add_argument(
        "--scenario", default=None,
        help="Executer uniquement ce scenario (ex: latency_step_payments)",
    )
    parser.add_argument(
        "--runs", type=int, default=DEFAULT_RUNS,
        help=f"Nombre de runs par cellule (defaut: {DEFAULT_RUNS})",
    )
    parser.add_argument(
        "--delay", type=float, default=0.0,
        help="Delai en secondes entre chaque run (ex: --delay 6 pour rester sous 10 RPM)",
    )
    args = parser.parse_args()

    # -- filtrage --model -------------------------------------------------------
    if args.model is not None:
        if args.model not in MODELS:
            print(f"ERROR: unknown model '{args.model}'", file=sys.stderr)
            print(f"  Modeles valides : {', '.join(MODELS)}", file=sys.stderr)
            return 1
        requested_models = [args.model]
    else:
        requested_models = list(MODELS)

    # -- filtrage --scenario ----------------------------------------------------
    scenario_names = [s["name"] for s in SCENARIOS]
    if args.scenario is not None:
        if args.scenario not in scenario_names:
            print(f"ERROR: unknown scenario '{args.scenario}'", file=sys.stderr)
            print(f"  Scenarios valides : {', '.join(scenario_names)}", file=sys.stderr)
            return 1
        active_scenarios = [s for s in SCENARIOS if s["name"] == args.scenario]
    else:
        active_scenarios = list(SCENARIOS)

    n_runs = args.runs

    gemini_key, anthropic_key = _load_api_keys()
    gemini_client = genai.Client(api_key=gemini_key)
    judge_client = _build_judge_client(anthropic_key)

    active_models = _filter_models(requested_models, gemini_client)
    if not active_models:
        print("Erreur : aucun modele disponible.", file=sys.stderr)
        return 1

    judge_enabled = judge_client is not None
    total_gemini = len(active_models) * len(active_scenarios) * n_runs
    total_judge = total_gemini if judge_enabled else 0
    print(f"Models        : {len(active_models)}")
    print(f"Scenarios     : {len(active_scenarios)}")
    print(f"Runs          : {n_runs}")
    print(f"Delay         : 5s entre runs")
    print(f"LLM Judge     : {'claude-sonnet-4-6' if judge_enabled else 'desactive'}")
    print(f"Total calls   : {total_gemini}"
          + (f" Gemini + {total_judge} judge" if judge_enabled else ""))
    print()

    all_results = []

    for model in active_models:
        for scenario in active_scenarios:
            prompt = build_prompt(scenario)
            scenario_name = scenario["name"]
            print(f"  {model:<30} {scenario_name:<30}", end="", flush=True)
            runs = []
            for i in range(n_runs):
                if i > 0 and args.delay > 0:
                    time.sleep(args.delay)
                run = _run_once(gemini_client, model, prompt, scenario, judge_client)
                time.sleep(5)
                runs.append(run)
                if run["error"]:
                    status = "E"
                    print(f"\n    [run {i+1} ERROR] {run['error']}", file=sys.stderr, flush=True)
                    print(f"  {model:<30} {scenario_name:<30}", end="", flush=True)
                elif run["judge_score"] is not None:
                    status = str(run["judge_score"]["correctness"] + run["judge_score"]["quality"]
                                 + run["judge_score"]["checks_actionability"] + run["judge_score"]["cause_calibration"])
                elif run["valid_schema"]:
                    status = "."
                else:
                    status = "J" if run["valid_json"] else "x"
                print(status, end="", flush=True)
            print(f"  {runs[-1]['latency_ms']:.0f}ms")

            entry = {
                "model": model,
                "scenario_name": scenario_name,
                "scenario": scenario,
                "runs": runs,
                "aggregate": _aggregate(runs),
            }
            all_results.append(entry)

    _print_summary(all_results)

    csv_path = Path(__file__).parent / "summary.csv"
    _write_csv(all_results, csv_path)
    print(f"CSV ecrit dans  {csv_path}")

    if args.output:
        payload = {
            "benchmark_config": {
                "n_runs": n_runs,
                "judge_model": _JUDGE_MODEL if judge_enabled else None,
                "judge_enabled": judge_enabled,
            },
            "requested_models": requested_models,
            "active_models": active_models,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "n_runs": n_runs,
            "results": all_results,
        }
        args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"JSON ecrit dans {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
