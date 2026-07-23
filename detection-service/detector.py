import os
import json
import time
import math
import logging
import psycopg2
from datetime import datetime, timezone
from notifier import send_slack_alert
from correlator import correlate, write_correlation, correlate_deploy, write_deploy_correlation
from explainer import generate_explanation
from baseline_utils import (
    get_baseline, get_baselines, compute_deviation, pg_dow,
    ENDPOINT_SLOS, DEFAULT_SLOS,
)
from features import compute_features, vectorize, direction_of, WINDOW_SIZE, MIN_WINDOW
from ttd import estimate_ttd
import ml_model
import prom_metrics as pm

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
# ANSI colors
_C = {
    "ok":         "\033[32m",  # vert
    "pending":    "\033[33m",  # jaune
    "firing":     "\033[31m",  # rouge
    "resolving":  "\033[36m",  # cyan
    "critical":   "\033[31m",
    "warning":    "\033[33m",
    "reset":      "\033[0m",
}
def _sev(s):   return f"{_C.get(s, '')}{s}{_C['reset']}" if s else ""


def _transition_line(state, endpoint_id, p99=None, err=None, score=None, count=None, total=None, prev_state=None):
    if prev_state:
        if count is not None and total is not None:
            label = f"[{prev_state.upper()} -> {state.upper()} {count}/{total}]"
        else:
            label = f"[{prev_state.upper()} -> {state.upper()}]"
    elif count is not None and total is not None:
        label = f"[{state.upper()} {count}/{total}]"
    else:
        label = f"[{state.upper()}]"
    color = _C.get(state, '')
    tag = f"{color}{label}{_C['reset']}"
    pad = " " * max(1, 16 - len(label))

    metrics = []
    if p99 is not None:
        metrics.append(f"p99={p99:.0f}ms")
    if err is not None:
        metrics.append(f"err={err:.1f}%")
    if score is not None and score > 0 and state in ("firing", "resolving"):
        metrics.append(f"score={score:.2f}")

    m = "   ".join(metrics)
    return f"{tag}{pad}{endpoint_id:<32s}{m}"


DB_URL = os.environ.get("DATABASE_URL", "postgresql://cassandra:cassandra@localhost:5434/cassandra")

PENDING_WINDOWS = 2    # cycles avant FIRING
RESOLVING_WINDOWS = 2  # cycles avant OK

# Seuil de score combine pour declencher. Tune sur la campagne (tune_contamination.py,
# spec 9.2) : F1 maximal a 0.60 (Recall 71%, Precision 71%, 1.45 FP/h) contre 0.698
# a 0.50 -- 0.60 supprime un faux positif sans perdre de detection. Un depassement
# SLO dur (layer 0) declenche de toute facon, independamment de ce seuil.
FIRE_THRESHOLD = 0.6
# Metriques dont la couche ML a besoin de la baseline saisonniere.
ML_METRICS = ["p50_ms", "p95_ms", "p99_ms", "rps"]

# Cache du modele ML promu, recharge quand l'artefact latest change (retrain nightly).
_ML = {"bundle": None, "mtime": 0.0}


def maybe_load_model():
    """Charge/recharge l'artefact ML promu si son mtime a change. Best-effort."""
    path = ml_model.latest_path()
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return  # pas d'artefact : la couche ML reste inactive
    if _ML["bundle"] is None or mt > _ML["mtime"]:
        bundle = ml_model.load_latest()
        if bundle:
            _ML["bundle"] = bundle
            _ML["mtime"] = mt
            log.info(f"Modele ML charge (trained_at={bundle['meta'].get('trained_at')}, "
                     f"n={bundle['meta'].get('n_samples')})")


def get_window(cur, endpoint_id, n):
    """Derniere fenetre de n cycles pour un endpoint (time croissant, dicts)."""
    cur.execute("""
        SELECT time, rps, p50_ms, p95_ms, p99_ms, error_rate_5xx
        FROM endpoint_features
        WHERE endpoint_id = %s AND p99_ms IS NOT NULL AND p99_ms < 'NaN'::float8
        ORDER BY time DESC
        LIMIT %s
    """, (endpoint_id, n))
    rows = list(reversed(cur.fetchall()))
    return [
        {"time": t, "rps": rps, "p50_ms": p50, "p95_ms": p95, "p99_ms": p99, "error_rate_5xx": err}
        for (t, rps, p50, p95, p99, err) in rows
    ]


def _finite(v):
    """Renvoie v si c'est un nombre fini, sinon None (baselines NaN cote DB)."""
    return v if (isinstance(v, (int, float)) and math.isfinite(v)) else None


def _json_safe(obj):
    """Remplace recursivement les floats non finis par None : JSONB refuse NaN/Inf."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


def write_anomaly(cur, endpoint_id, signal_type, window_start, score, layer, direction, contributing):
    """Ecrit un enregistrement dans l'anomaly store (spec 6.3). Best-effort cote appelant."""
    cur.execute("""
        INSERT INTO anomalies
            (endpoint_id, signal_type, window_start, score, layer, direction, contributing_features)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (endpoint_id, signal_type, window_start, score, layer, direction,
          json.dumps(_json_safe(contributing))))

def get_latest_features(cur):
    cur.execute("""
        SELECT DISTINCT ON (endpoint_id)
            endpoint_id, p99_ms, error_rate_5xx, time
        FROM endpoint_features
        WHERE p99_ms IS NOT NULL
        ORDER BY endpoint_id, time DESC
    """)
    return cur.fetchall()

def get_alert_state(cur, endpoint_id, signal_type):
    cur.execute("""
        SELECT state, pending_count, resolving_count
        FROM alerts
        WHERE endpoint_id = %s AND signal_type = %s
        LIMIT 1
    """, (endpoint_id, signal_type))
    row = cur.fetchone()
    log.debug(f"DB state for {endpoint_id}/{signal_type}: {row}")
    return row

def set_pending(cur, endpoint_id, signal_type, severity, score, raw_value, layer, now):
    cur.execute("""
        INSERT INTO alerts
            (endpoint_id, signal_type, state, severity, score, raw_value, layer,
             pending_count, resolving_count, opened_at, resolved_at, pending_since, updated_at)
        VALUES (%s, %s, 'pending', %s, %s, %s, %s,
                0, 0, NULL, NULL, %s, %s)
        ON CONFLICT (endpoint_id, signal_type)
        DO UPDATE SET
            state           = 'pending',
            severity        = EXCLUDED.severity,
            score           = EXCLUDED.score,
            raw_value       = EXCLUDED.raw_value,
            layer           = EXCLUDED.layer,
            pending_count   = 0,
            resolving_count = 0,
            resolved_at     = NULL,
            pending_since   = COALESCE(alerts.pending_since, EXCLUDED.pending_since),
            updated_at      = %s
    """, (endpoint_id, signal_type, severity, score, raw_value, layer,
          now, now, now))

def increment_pending(cur, endpoint_id, signal_type, severity, score, raw_value, layer, now):
    cur.execute("""
        UPDATE alerts
        SET severity        = %s,
            score           = %s,
            raw_value       = %s,
            layer           = %s,
            pending_count   = pending_count + 1,
            updated_at      = %s
        WHERE endpoint_id = %s AND signal_type = %s
    """, (severity, score, raw_value, layer, now, endpoint_id, signal_type))

def set_firing(cur, endpoint_id, signal_type, severity, score, raw_value, layer, now):
    cur.execute("""
        UPDATE alerts
        SET state           = 'firing',
            severity        = %s,
            score           = %s,
            raw_value       = %s,
            layer           = %s,
            opened_at       = CASE
                                  WHEN state = 'resolving' THEN COALESCE(opened_at, %s)
                                  ELSE %s
                              END,
            pending_count   = 0,
            resolving_count = 0,
            updated_at      = %s
        WHERE endpoint_id = %s AND signal_type = %s
    """, (severity, score, raw_value, layer, now, now, now, endpoint_id, signal_type))

def set_resolving(cur, endpoint_id, signal_type, now):
    cur.execute("""
        UPDATE alerts
        SET state           = 'resolving',
            resolving_count = 1,
            pending_count   = 0,
            updated_at      = %s
        WHERE endpoint_id = %s AND signal_type = %s AND state = 'firing'
    """, (now, endpoint_id, signal_type))

def set_ok(cur, endpoint_id, signal_type, now):
    cur.execute("""
        UPDATE alerts
        SET state           = 'ok',
            resolved_at     = %s,
            pending_count   = 0,
            resolving_count = 0,
            pending_since   = NULL,
            updated_at      = %s
        WHERE endpoint_id = %s AND signal_type = %s
    """, (now, now, endpoint_id, signal_type))

def process_signal(cur, endpoint_id, signal_type, anomaly, severity, score, raw_value, layer, now, expected_states=None, p99=None, err5xx=None, ttd=None):
    row = get_alert_state(cur, endpoint_id, signal_type)
    current_state = row[0] if row else "ok"
    pending_count = row[1] if row else 0
    resolving_count = row[2] if row else 0
    log.debug(f"  {endpoint_id}/{signal_type} : db_state={current_state} pending={pending_count} resolving={resolving_count}")

    new_state = current_state
    prev = None
    counter = None
    total = None

    if anomaly:
        if current_state in ("ok", None):
            set_pending(cur, endpoint_id, signal_type, severity, score, raw_value, layer, now)
            new_state = "pending"
            prev = "ok"
            counter = 1
            total = PENDING_WINDOWS
        elif current_state == "pending":
            if pending_count + 1 >= PENDING_WINDOWS:
                set_firing(cur, endpoint_id, signal_type, severity, score, raw_value, layer, now)
                new_state = "firing"
                prev = "pending"
                # L'ordre est important : la correlation et l'explication doivent etre
                # calculees AVANT la notification pour que le message Slack porte la
                # cause suspectee et l'explication LLM. Chaque etape est best-effort :
                # une panne de correlation ou du LLM ne doit jamais bloquer l'alerte.
                # --- correlation injection (ground-truth des scenarios) ---
                correlation = None
                try:
                    correlation = correlate(endpoint_id, now)
                    write_correlation(cur, endpoint_id, signal_type, correlation)
                except Exception as e:
                    log.error(f"Correlation failed for {endpoint_id}/{signal_type}: {e}")
                # --- correlation deploiement (table deploy_events) ---
                deploy = None
                try:
                    deploy = correlate_deploy(cur, endpoint_id, now)
                    write_deploy_correlation(cur, endpoint_id, signal_type, deploy)
                except Exception as e:
                    log.error(f"Deploy correlation failed for {endpoint_id}/{signal_type}: {e}")
                # --- explanation (LLM, avec fallback template interne) ---
                explanation = None
                try:
                    explanation = generate_explanation(cur, endpoint_id, signal_type, now)
                    pm.LLM_CALLS.labels(result="fallback" if explanation.get("fallback") else "llm").inc()
                except Exception as e:
                    pm.LLM_CALLS.labels(result="error").inc()
                    log.error(f"Explanation generation failed for {endpoint_id}/{signal_type}: {e}")
                # --- notification (en dernier : enrichie cause + deploy + explication + TTD) ---
                send_slack_alert(endpoint_id, signal_type, severity, score, raw_value,
                                 correlation=correlation, deploy=deploy, explanation=explanation,
                                 ttd=ttd)
            else:
                increment_pending(cur, endpoint_id, signal_type, severity, score, raw_value, layer, now)
                new_state = "pending"
                counter = pending_count + 2
                total = PENDING_WINDOWS
        elif current_state == "firing":
            cur.execute("""
                UPDATE alerts SET score = %s, raw_value = %s, severity = %s, updated_at = %s
                WHERE endpoint_id = %s AND signal_type = %s AND state = 'firing'
            """, (score, raw_value, severity, now, endpoint_id, signal_type))
            if cur.rowcount == 0:
                log.warning(f"{endpoint_id}/{signal_type} : stale firing state, skipping")
            new_state = "firing"
        elif current_state == "resolving":
            if resolving_count >= RESOLVING_WINDOWS:
                set_ok(cur, endpoint_id, signal_type, now)
            else:
                set_firing(cur, endpoint_id, signal_type, severity, score, raw_value, layer, now)
            new_state = "firing"
    else:
        if current_state == "firing":
            set_resolving(cur, endpoint_id, signal_type, now)
            if cur.rowcount == 0:
                log.warning(f"{endpoint_id}/{signal_type} : stale firing state, no row to resolve")
            new_state = "resolving"
            prev = "firing"
            counter = 1
            total = RESOLVING_WINDOWS
        elif current_state == "resolving":
            if resolving_count >= RESOLVING_WINDOWS:
                set_ok(cur, endpoint_id, signal_type, now)
                new_state = "ok"
                prev = "resolving"
            else:
                cur.execute("""
                    UPDATE alerts SET resolving_count = resolving_count + 1, updated_at = %s
                    WHERE endpoint_id = %s AND signal_type = %s
                """, (now, endpoint_id, signal_type))
                new_state = "resolving"
                counter = resolving_count + 1
                total = RESOLVING_WINDOWS
        elif current_state == "pending":
            set_ok(cur, endpoint_id, signal_type, now)
            new_state = "ok"
        else:
            new_state = "ok"

    display_score = score if anomaly else 0.0
    line = _transition_line(new_state, endpoint_id, p99=p99, err=err5xx,
                            score=display_score, count=counter, total=total, prev_state=prev)
    if new_state == "ok" and current_state in ("ok", None):
        log.debug(line)
    else:
        log.info(line)

    if expected_states is not None:
        cur.execute("SELECT state FROM alerts WHERE endpoint_id = %s AND signal_type = %s", (endpoint_id, signal_type))
        row = cur.fetchone()
        expected_states[(endpoint_id, signal_type)] = row[0] if row else "ok"


def audit_state_consistency(cur, expected_states):
    """Vérifie que l'état en base correspond à ce que le détecteur vient de décider."""
    if not expected_states:
        return
    for (endpoint_id, signal_type), expected_state in expected_states.items():
        cur.execute("""
            SELECT state FROM alerts
            WHERE endpoint_id = %s AND signal_type = %s
        """, (endpoint_id, signal_type))
        row = cur.fetchone()
        actual_state = row[0] if row else "absent"
        if actual_state != expected_state and not (actual_state == "absent" and expected_state == "ok"):
            log.error(
                f"STATE MISMATCH {endpoint_id}/{signal_type}: "
                f"expected={expected_state} actual={actual_state}"
            )
        else:
            log.debug(f"STATE OK {endpoint_id}/{signal_type}: {actual_state}")

def ensure_schema(cur):
    cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS raw_value DOUBLE PRECISION")
    cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS pending_count INTEGER DEFAULT 0")
    cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS resolving_count INTEGER DEFAULT 0")
    cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS suspected_fault TEXT")
    cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS imputation_score DOUBLE PRECISION")

def run_detection(conn):
    cur = conn.cursor()
    now = datetime.now(timezone.utc)
    dow = pg_dow(now)
    hour = now.hour
    _expected_states = {}
    maybe_load_model()
    rows = get_latest_features(cur)
    pm.ENDPOINTS_SCORED.set(len(rows))
    for row in rows:
        endpoint_id, p99, err5xx, ts = row

        if p99 is None or (isinstance(p99, float) and math.isnan(p99)):
            continue

        # --- Signal p99 : layer 0 (static) + layer 1 (baseline) + layer 2 (ML) ---
        baselines = get_baselines(cur, endpoint_id, ML_METRICS, dow, hour)
        b99 = baselines.get("p99_ms")
        if b99:
            p10, p50b, p90 = _finite(b99[0]), _finite(b99[1]), _finite(b99[2])
            deviation = compute_deviation(p99, p10, p90)
        else:
            p10 = p50b = p90 = None
            deviation = 0.0

        direction = direction_of(p99, p10, p90)
        slos = ENDPOINT_SLOS.get(endpoint_id, DEFAULT_SLOS)

        static_breach = p99 > slos["p99_ms"]
        static_norm = min(1.0, (p99 - slos["p99_ms"]) / (slos["p99_ms"] + 1e-6)) if static_breach else 0.0
        baseline_norm = min(1.0, deviation / 2.0)  # deviation>2 -> 1.0 (critique)

        # Fenetre + features derivees endpoint-relatives (aussi pour l'anomaly store).
        window = get_window(cur, endpoint_id, WINDOW_SIZE)
        feat = compute_features(window, baselines) if len(window) >= MIN_WINDOW else None

        # Layer 2 : Isolation Forest, calibre puis GATE par la direction (spec 5.4) :
        # ne contribue qu'en cas de degradation, jamais sur une perf anormalement bonne.
        ml_norm = 0.0
        ml_top = None
        bundle = _ML["bundle"]
        if feat is not None and bundle is not None:
            try:
                x = vectorize(feat)
                meta = bundle["meta"]
                raw = ml_model.raw_anomaly(bundle["model"], x)[0]
                ml_norm = float(ml_model.calibrate(raw, meta["calibration"]))
                ml_top = ml_model.attribute(bundle["model"], x, meta["feature_medians"], meta.get("calibration"))
            except Exception as e:
                log.error(f"ML scoring failed for {endpoint_id}: {e}")
        ml_gated = ml_norm if direction == "degradation" else 0.0

        # Combinaison calibree (spec 8.3) : layer 1 = plancher + direction, layer 2 additif.
        combined = baseline_norm + (1.0 - baseline_norm) * ml_gated
        if static_breach:
            combined = max(combined, static_norm)  # un depassement SLO dur est un plancher
        if not math.isfinite(combined):
            combined = 0.0

        anomaly = static_breach or combined >= FIRE_THRESHOLD

        layer_scores = {"static": static_norm, "baseline": baseline_norm, "iforest": ml_gated}
        strong = [k for k, v in layer_scores.items() if v >= 0.3]
        if len(strong) >= 2:
            layer = "combined"
        elif anomaly:
            layer = max(layer_scores, key=layer_scores.get)
        else:
            layer = None

        severity = "critical" if (p99 > slos["p99_ms"] * 2 or combined >= 0.8) else "warning"

        # Attribution top-3 : ML si dispo, sinon deviations baseline les plus fortes.
        if ml_top:
            top_features = [[n, s] for n, s in ml_top]
        elif feat:
            top_features = [[k, round(v, 4)]
                            for k, v in sorted(feat.items(), key=lambda kv: abs(kv[1]), reverse=True)[:3]]
        else:
            top_features = []

        # Alerte precoce TTD (spec 8.4, advisory) : si la tendance p99 est haussiere,
        # on extrapole vers le SLO. Utile surtout avant le breach (layer 1/2 firent
        # avant le SLO dur) : "a ce rythme, SLO atteint dans ~X min".
        ttd = None
        if anomaly and window and len(window) >= MIN_WINDOW:
            try:
                ttd = estimate_ttd([r.get("p99_ms") for r in window], slos["p99_ms"])
            except Exception as e:
                log.error(f"TTD estimation failed for {endpoint_id}: {e}")

        contributing = {
            "combined": round(combined, 4),
            "layers": {k: round(v, 4) for k, v in layer_scores.items()},
            "direction": direction,
            "ml_norm": round(ml_norm, 4),
            "top_features": top_features,
            "ttd": ttd,
            "baseline": {
                "metric": "p99_ms",
                "observed": round(p99, 2),
                "expected": round(p50b, 2) if p50b is not None else None,
                "band": [round(p10, 2), round(p90, 2)] if (p10 is not None and p90 is not None) else None,
            },
        }

        # Anomaly store : un enregistrement par cycle scoree (spec 6.3), best-effort.
        try:
            write_anomaly(cur, endpoint_id, "p99_ms", ts, round(combined, 4),
                          layer or "combined", direction, contributing)
            pm.ANOMALY_WRITES.inc()
        except Exception as e:
            log.error(f"Anomaly write failed for {endpoint_id}: {e}")

        process_signal(cur, endpoint_id, "p99_ms", anomaly, severity if anomaly else None,
                       combined if anomaly else 0.0, p99, layer, now, _expected_states,
                       p99=p99, err5xx=err5xx, ttd=ttd)

        # Persiste les features contributives sur l'alerte (best-effort).
        if anomaly:
            try:
                cur.execute(
                    "UPDATE alerts SET contributing_features = %s WHERE endpoint_id = %s AND signal_type = %s",
                    (json.dumps(_json_safe(contributing)), endpoint_id, "p99_ms"))
            except Exception as e:
                log.error(f"contributing_features write failed for {endpoint_id}: {e}")

        if err5xx is not None and err5xx > slos["error_rate_5xx"]:
            err_score = (err5xx - slos["error_rate_5xx"]) / (slos["error_rate_5xx"] + 1e-6)
            process_signal(cur, endpoint_id, "error_rate_5xx", True, "critical", err_score, err5xx, "static", now, _expected_states, p99=p99, err5xx=err5xx)
        else:
            process_signal(cur, endpoint_id, "error_rate_5xx", False, None, 0.0, err5xx, None, now, _expected_states, p99=p99, err5xx=err5xx)

    audit_state_consistency(cur, _expected_states)
    _update_metrics_gauges(cur)
    conn.commit()
    cur.close()


def _update_metrics_gauges(cur):
    """Met a jour les gauges Prometheus (alertes par etat + fraicheur). Best-effort."""
    try:
        cur.execute("SELECT state, count(*) FROM alerts GROUP BY state")
        counts = {state: n for state, n in cur.fetchall()}
        for st in ("ok", "pending", "firing", "resolving"):
            pm.ALERTS_STATE.labels(state=st).set(counts.get(st, 0))
        cur.execute("SELECT EXTRACT(EPOCH FROM (now() - max(time))) FROM endpoint_features")
        row = cur.fetchone()
        if row and row[0] is not None:
            pm.SCRAPE_FRESHNESS.set(float(row[0]))
    except Exception as e:
        log.error(f"metrics gauges update failed: {e}")


def run():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    ensure_schema(cur)
    conn.commit()
    cur.close()
    pm.start_metrics_server()
    log.info("Detector started")
    while True:
        try:
            t0 = time.monotonic()
            run_detection(conn)
            pm.CYCLE_SECONDS.observe(time.monotonic() - t0)
        except Exception as e:
            pm.CYCLE_ERRORS.inc()
            log.error(f"Detection cycle failed: {e}")
            conn = psycopg2.connect(DB_URL)
        time.sleep(60)

if __name__ == "__main__":
    run()
