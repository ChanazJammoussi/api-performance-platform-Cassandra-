from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import psycopg2
from psycopg2 import pool
import os
import time
import random
import threading
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from fault import (
    fault_state,
    apply_latency_step,
    apply_error_burst,
    apply_pool_shrink,
    apply_downstream_slow,
    start_latency_creep,
)

# setup metrics SDK
exporter = OTLPMetricExporter(endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317"))
reader = PeriodicExportingMetricReader(exporter, export_interval_millis=10000)
provider = MeterProvider(metric_readers=[reader])
metrics.set_meter_provider(provider)
meter = metrics.get_meter("orders")

pool_wait_gauge = meter.create_gauge("orders.pool_wait_ms", description="DB connection pool wait time ms")
queue_depth_gauge = meter.create_gauge("orders.queue_depth", description="Internal work queue depth")

DB_URL = os.getenv("DATABASE_URL", "postgresql://cassandra:cassandra@timescaledb:5432/cassandra")
try:
    db_pool = psycopg2.pool.ThreadedConnectionPool(minconn=1, maxconn=20, dsn=DB_URL)
except Exception:
    db_pool = None

work_queue = []
work_queue_lock = threading.Lock()

def get_db_connection():
    if db_pool is None:
        return None
    start = time.time()
    conn = db_pool.getconn()
    wait_ms = (time.time() - start) * 1000
    pool_wait_gauge.set(wait_ms)
    return conn

def release_db_connection(conn):
    if db_pool and conn:
        db_pool.putconn(conn)

def maybe_inject_error():
    state = fault_state.get()
    if state["error_rate"] > 0 and random.random() < state["error_rate"]:
        raise HTTPException(status_code=500, detail="injected fault")

def apply_latency():
    state = fault_state.get()
    if state["latency_ms"] > 0:
        time.sleep(state["latency_ms"] / 1000)

app = FastAPI()

# ---- fault control API (interne seulement) ----

@app.post("/faults/latency_step")
def fault_latency_step(body: dict):
    apply_latency_step(int(body.get("latency_ms", 200)))
    return {"applied": "latency_step", "latency_ms": fault_state.get()["latency_ms"]}

@app.post("/faults/latency_creep")
def fault_latency_creep(body: dict):
    start_latency_creep(
        target_ms=int(body.get("target_ms", 500)),
        duration_minutes=float(body.get("duration_minutes", 5))
    )
    return {"applied": "latency_creep"}

@app.post("/faults/error_burst")
def fault_error_burst(body: dict):
    apply_error_burst(float(body.get("error_rate", 0.3)))
    return {"applied": "error_burst", "error_rate": fault_state.get()["error_rate"]}

@app.post("/faults/pool_shrink")
def fault_pool_shrink(body: dict):
    apply_pool_shrink(int(body.get("pool_max", 1)))
    return {"applied": "pool_shrink", "pool_max": fault_state.get()["pool_max"]}

@app.post("/faults/reset")
def fault_reset():
    fault_state.reset()
    return {"applied": "reset"}

@app.get("/faults/state")
def fault_get_state():
    return fault_state.get()

# ---- endpoints métier ----

@app.get("/orders/{order_id}")
def get_order(order_id: int):
    maybe_inject_error()
    apply_latency()
    conn = get_db_connection()
    time.sleep(random.uniform(0.01, 0.05))
    release_db_connection(conn)
    with work_queue_lock:
        work_queue.append(order_id)
        if len(work_queue) > 10:
            work_queue.pop(0)
        queue_depth_gauge.set(len(work_queue))
    return {"order_id": order_id, "status": "confirmed", "amount": round(random.uniform(10, 500), 2)}

@app.get("/orders")
def list_orders():
    maybe_inject_error()
    apply_latency()
    conn = get_db_connection()
    time.sleep(random.uniform(0.01, 0.08))
    release_db_connection(conn)
    return {"orders": [{"order_id": i, "status": "confirmed"} for i in range(1, 6)]}

@app.post("/orders")
def create_order(item: dict):
    maybe_inject_error()
    apply_latency()
    conn = get_db_connection()
    time.sleep(random.uniform(0.02, 0.1))
    release_db_connection(conn)
    return {"order_id": random.randint(100, 999), "status": "created", "item": item}

@app.get("/health")
def health():
    return {"status": "ok"}
