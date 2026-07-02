import threading
import time

class FaultState:
    def __init__(self):
        self._lock = threading.Lock()
        self.latency_ms = 0        # latence additionnelle en ms
        self.error_rate = 0.0      # taux d'erreurs 5xx (0.0 à 1.0)
        self.pool_max = 5          # taille max du connection pool
        self.downstream_slow = 0   # latence additionnelle sur appels sortants

    def get(self):
        with self._lock:
            return {
                "latency_ms": self.latency_ms,
                "error_rate": self.error_rate,
                "pool_max": self.pool_max,
                "downstream_slow": self.downstream_slow,
            }

    def reset(self):
        with self._lock:
            self.latency_ms = 0
            self.error_rate = 0.0
            self.pool_max = 5
            self.downstream_slow = 0

# instance globale partagée entre les threads
fault_state = FaultState()

def apply_latency_step(latency_ms: int):
    with fault_state._lock:
        fault_state.latency_ms = latency_ms

def apply_error_burst(error_rate: float):
    with fault_state._lock:
        fault_state.error_rate = min(max(error_rate, 0.0), 1.0)

def apply_pool_shrink(pool_max: int):
    with fault_state._lock:
        fault_state.pool_max = max(1, pool_max)

def apply_downstream_slow(latency_ms: int):
    with fault_state._lock:
        fault_state.downstream_slow = latency_ms

def start_latency_creep(target_ms: int, duration_minutes: float):
    """augmente la latence graduellement jusqu'à target_ms sur duration_minutes"""
    def creep():
        steps = 30
        step_ms = target_ms / steps
        step_sleep = (duration_minutes * 60) / steps
        for i in range(steps):
            with fault_state._lock:
                fault_state.latency_ms += step_ms
            time.sleep(step_sleep)
    t = threading.Thread(target=creep, daemon=True)
    t.start()
