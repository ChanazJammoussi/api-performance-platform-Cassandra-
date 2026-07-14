import time
import math
import logging
import psycopg2
from datetime import datetime, timezone
from notifier import send_slack_alert
from correlator import correlate, write_correlation
from explainer import generate_explanation
from baseline_utils import get_baseline, compute_deviation, ENDPOINT_SLOS, DEFAULT_SLOS

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


DB_URL = "postgresql://cassandra:cassandra@localhost:5434/cassandra"

PENDING_WINDOWS = 2    # cycles avant FIRING
RESOLVING_WINDOWS = 2  # cycles avant OK

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

def process_signal(cur, endpoint_id, signal_type, anomaly, severity, score, raw_value, layer, now, expected_states=None, p99=None, err5xx=None):
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
                send_slack_alert(endpoint_id, signal_type, severity, score, raw_value)
                # --- correlation ---
                try:
                    correlation = correlate(endpoint_id, now)
                    write_correlation(cur, endpoint_id, signal_type, correlation)
                except Exception as e:
                    log.error(f"Correlation failed for {endpoint_id}/{signal_type}: {e}")
                # --- explanation (LLM) ---
                try:
                    generate_explanation(cur, endpoint_id, signal_type, now)
                except Exception as e:
                    log.error(f"Explanation generation failed for {endpoint_id}/{signal_type}: {e}")
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
    dow = now.weekday()
    hour = now.hour
    _expected_states = {}
    rows = get_latest_features(cur)
    for row in rows:
        endpoint_id, p99, err5xx, ts = row

        if p99 is None or (isinstance(p99, float) and math.isnan(p99)):
            continue

        baseline = get_baseline(cur, endpoint_id, "p99_ms", dow, hour)
        if baseline:
            p10, p50, p90 = baseline
            deviation = compute_deviation(p99, p10, p90)
        else:
            deviation = 0.0
            p90 = None

        slos = ENDPOINT_SLOS.get(endpoint_id, DEFAULT_SLOS)

        if p99 > slos["p99_ms"]:
            severity = "critical" if p99 > slos["p99_ms"] * 2 else "warning"
            static_score = (p99 - slos["p99_ms"]) / (slos["p99_ms"] + 1e-6)
            process_signal(cur, endpoint_id, "p99_ms", True, severity, static_score, p99, "static", now, _expected_states, p99=p99, err5xx=err5xx)
        elif deviation > 1.0:
            severity = "critical" if deviation > 2.0 else "warning"
            process_signal(cur, endpoint_id, "p99_ms", True, severity, deviation, p99, "baseline", now, _expected_states, p99=p99, err5xx=err5xx)
        else:
            process_signal(cur, endpoint_id, "p99_ms", False, None, 0.0, p99, None, now, _expected_states, p99=p99, err5xx=err5xx)

        if err5xx is not None and err5xx > slos["error_rate_5xx"]:
            err_score = (err5xx - slos["error_rate_5xx"]) / (slos["error_rate_5xx"] + 1e-6)
            process_signal(cur, endpoint_id, "error_rate_5xx", True, "critical", err_score, err5xx, "static", now, _expected_states, p99=p99, err5xx=err5xx)
        else:
            process_signal(cur, endpoint_id, "error_rate_5xx", False, None, 0.0, err5xx, None, now, _expected_states, p99=p99, err5xx=err5xx)

    audit_state_consistency(cur, _expected_states)
    conn.commit()
    cur.close()

def run():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    ensure_schema(cur)
    conn.commit()
    cur.close()
    log.info("Detector started")
    while True:
        try:
            run_detection(conn)
        except Exception as e:
            log.error(f"Detection cycle failed: {e}")
            conn = psycopg2.connect(DB_URL)
        time.sleep(60)

if __name__ == "__main__":
    run()
