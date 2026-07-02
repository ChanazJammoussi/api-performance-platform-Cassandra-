from fastapi import FastAPI, HTTPException
import httpx
import os
import time
import random

app = FastAPI()

ORDERS_URL = os.getenv("ORDERS_URL", "http://orders:8001")
PAYMENTS_URL = os.getenv("PAYMENTS_URL", "http://payments:8002")

@app.get("/api/orders")
async def list_orders():
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{ORDERS_URL}/orders")
        return response.json()

@app.get("/api/orders/{order_id}")
async def get_order(order_id: int):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{ORDERS_URL}/orders/{order_id}")
        if response.status_code != 200:
            raise HTTPException(status_code=404, detail="order not found")
        return response.json()

@app.post("/api/payments")
async def create_payment(payment: dict):
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{PAYMENTS_URL}/payments", json=payment)
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return response.json()

@app.get("/health")
def health():
    return {"status": "ok"}
