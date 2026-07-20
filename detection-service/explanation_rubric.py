#!/usr/bin/env python3
"""
explanation_rubric.py -- evaluation de la qualite des explications LLM (spec 9.2).

Score un echantillon d'explications (alerts.explanation) sur une petite rubrique. La
notation reste en partie subjective (spec 9.2) : l'outil score des proxys OBJECTIFS
et laisse la note finale d'appreciation a l'humain.

Rubrique (total 6 points) :
  R1 Structure valide (0/1)      : summary + suspected_cause + checks presents
  R2 Chiffres coherents (0/2)    : la valeur observee (raw_value) est-elle citee ?
  R3 Actionnabilite checks (0/2) : >= 2 checks, specifiques (verbe + cible)
  R4 Cadrage incertitude (0/1)   : la cause suspectee est-elle nuancee ?

Usage :
    python explanation_rubric.py
    python explanation_rubric.py --output rubric.json
"""

import argparse
import json
import os
import re
import statistics
import sys

import psycopg2
import psycopg2.extras

DB_URL = os.environ.get("DATABASE_URL", "postgresql://cassandra:cassandra@localhost:5434/cassandra")

ACTION_TERMS = ["verifi", "examin", "confirm", "investig", "analys", "inspect", "compar", "consult",
                "logs", "metriqu", "métriqu", "cpu", "memoir", "mémoir", "db", "requet", "requêt",
                "endpoint", "deploy", "déploie", "pool", "latence", "erreur", "trafic"]
HEDGE_TERMS = ["suspect", "faible", "incertain", "probable", "possible", "score", "peut", "semble",
               "pourrait", "hypoth", "non confirm", "a confirmer", "à confirmer"]


def _numbers(text):
    return [float(x.replace(",", ".")) for x in re.findall(r"\d+[.,]?\d*", text or "")]


def score_explanation(exp, raw_value):
    """Retourne (scores_dict, total) pour une explication."""
    summary = (exp.get("summary") or "").strip()
    cause = (exp.get("suspected_cause") or "").strip()
    checks = exp.get("checks") or []

    # R1 structure
    r1 = 1 if (summary and cause and isinstance(checks, list) and len(checks) >= 1) else 0

    # R2 chiffres coherents : la valeur observee est-elle citee (±5%) ?
    nums = _numbers(summary) + _numbers(cause)
    if raw_value is not None and any(abs(n - raw_value) <= max(0.05 * raw_value, 1.0) for n in nums):
        r2 = 2
    elif nums:
        r2 = 1
    else:
        r2 = 0

    # R3 actionnabilite : >=2 checks specifiques (longueur + terme d'action)
    def specific(c):
        c = (c or "").lower()
        return len(c) >= 40 and any(t in c for t in ACTION_TERMS)
    n_spec = sum(1 for c in checks if specific(c))
    r3 = 2 if n_spec >= 2 else (1 if n_spec == 1 else 0)

    # R4 cadrage de l'incertitude : la cause nuance-t-elle ?
    if not cause:
        r4 = 1  # rien a nuancer
    else:
        r4 = 1 if any(t in cause.lower() for t in HEDGE_TERMS) else 0

    scores = {"R1_structure": r1, "R2_chiffres": r2, "R3_actionnabilite": r3, "R4_incertitude": r4}
    return scores, r1 + r2 + r3 + r4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Rapport JSON optionnel")
    args = parser.parse_args()

    conn = psycopg2.connect(DB_URL)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT endpoint_id, signal_type, raw_value, imputation_score,
                   suspected_fault, explanation
            FROM alerts
            WHERE explanation IS NOT NULL
            ORDER BY endpoint_id, signal_type
        """)
        rows = cur.fetchall()
    conn.close()

    if not rows:
        print("Aucune explication en base (alerts.explanation).", file=sys.stderr)
        return 1

    print(f"Echantillon : {len(rows)} explication(s)\n")
    print(f"{'endpoint':<28} {'src':>4} {'R1':>3} {'R2':>3} {'R3':>3} {'R4':>3} {'tot/6':>6}")
    print("-" * 60)
    totals, report = [], []
    n_llm = 0
    for r in rows:
        exp = r["explanation"]
        if isinstance(exp, str):
            exp = json.loads(exp)
        src = "tmpl" if exp.get("fallback") else "llm"
        if src == "llm":
            n_llm += 1
        sc, tot = score_explanation(exp, r["raw_value"])
        totals.append(tot)
        report.append({"endpoint": r["endpoint_id"], "signal": r["signal_type"],
                       "source": src, "scores": sc, "total": tot})
        print(f"{r['endpoint_id']:<28} {src:>4} {sc['R1_structure']:>3} {sc['R2_chiffres']:>3} "
              f"{sc['R3_actionnabilite']:>3} {sc['R4_incertitude']:>3} {tot:>6}")
    print("-" * 60)
    mean = statistics.mean(totals)
    print(f"\nScore moyen : {mean:.2f} / 6   ({100*mean/6:.0f}%)   "
          f"| explications LLM : {n_llm}/{len(rows)}")
    print("Note : R1-R3 sont des proxys objectifs ; l'appreciation finale de la "
          "pertinence reste subjective (spec 9.2).")

    if args.output:
        payload = {"n": len(rows), "n_llm": n_llm, "mean_score": mean,
                   "max_score": 6, "explanations": report}
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print(f"\nrapport ecrit dans {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
