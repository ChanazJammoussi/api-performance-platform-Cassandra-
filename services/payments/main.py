from fastapi import FastAPI, HTTPException
import httpx
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
    apply_downstream_slow,
    start_latency_creep,
)

exporter = OTLPMetricExporter(endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317"))
reader = PeriodicExportingMetricReader(exporter, export_interval_millis=10000)
provider = MeterProvider(metric_readers=[reader])
metrics.set_meter_provider(provider)
meter = metrics.get_meter("payments")

queue_depth_gauge = meter.create_gauge("payments.queue_depth", description="Payment processing queue depth")
processing_time_gauge = meter.create_gauge("payments.processing_ms", description="Payment processing time ms")

work_queue = []
work_queue_lock = threading.Lock()

app = FastAPI()

ORDERS_URL = os.getenv("ORDERS_URL", "http://orders:8001")

def maybe_inject_error():
    state = fault_state.get()
    if state["error_rate"] > 0 and random.random() < state["error_rate"]:
        raise HTTPException(status_code=500, detail="injected fault")

def apply_latency():
    state = fault_state.get()
    if state["latency_ms"] > 0:
        time.sleep(state["latency_ms"] / 1000)

def get_downstream_timeout():
    state = fault_state.get()
    base = 5.0
    extra = state["downstream_slow"] / 1000
    return base + extra

# ---- fault control API ----

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

@app.post("/faults/downstream_slow")
def fault_downstream_slow(body: dict):
    apply_downstream_slow(int(body.get("latency_ms", 500)))
    return {"applied": "downstream_slow", "downstream_slow": fault_state.get()["downstream_slow"]}

@app.post("/faults/reset")
def fault_reset():
    fault_state.reset()
    return {"applied": "reset"}

@app.get("/faults/state")
def fault_get_state():
    return fault_state.get()

# ---- endpoints métier ----

@app.post("/payments")
async def create_payment(payment: dict):
    maybe_inject_error()
    apply_latency()

    order_id = payment.get("order_id")
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id required")

    timeout = get_downstream_timeout()
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(f"{ORDERS_URL}/orders/{order_id}")
        if response.status_code != 200:
            raise HTTPException(status_code=404, detail="order not found")

    start = time.time()
    time.sleep(random.uniform(0.02, 0.08))
    processing_ms = (time.time() - start) * 1000
    processing_time_gauge.set(processing_ms)

    with work_queue_lock:
        work_queue.append(order_id)
        if len(work_queue) > 10:
            work_queue.pop(0)
        queue_depth_gauge.set(len(work_queue))

    return {
        "payment_id": random.randint(1000, 9999),
        "order_id": order_id,
        "status": "approved",
        "amount": response.json().get("amount")
    }

@app.get("/payments/{payment_id}")
def get_payment(payment_id: int):
    maybe_inject_error()
    apply_latency()
    time.sleep(random.uniform(0.01, 0.05))
    return {
        "payment_id": payment_id,
        "status": "approved",
        "amount": round(random.uniform(10, 500), 2)
    }

@app.get("/health")
def health():
    return {"status": "ok"}
