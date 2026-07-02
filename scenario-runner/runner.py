#!/usr/bin/env python3
import argparse
import asyncio
import json
import yaml
import httpx
from datetime import datetime, timezone
from pathlib import Path

SERVICES = {
    "orders":   "http://localhost:8001",
    "payments": "http://localhost:8002",
}

async def inject_fault(client: httpx.AsyncClient, service: str, fault_type: str, params: dict):
    base_url = SERVICES[service]
    url = f"{base_url}/faults/{fault_type}"
    response = await client.post(url, json=params, timeout=10)
    response.raise_for_status()
    return response.json()

async def reset_fault(client: httpx.AsyncClient, service: str):
    base_url = SERVICES[service]
    await client.post(f"{base_url}/faults/reset", timeout=10)

async def run_scenario(scenario_file: str, output_file: str):
    with open(scenario_file) as f:
        scenario = yaml.safe_load(f)

    scenario_id = scenario.get("id", Path(scenario_file).stem)
    faults = scenario.get("faults", [])
    ground_truth = []

    print(f"Running scenario: {scenario_id}")
    print(f"Total faults: {len(faults)}")

    async with httpx.AsyncClient() as client:
        for i, fault in enumerate(faults):
            service     = fault["service"]
            fault_type  = fault["type"]
            params      = fault.get("params", {})
            duration_s  = fault.get("duration_seconds", 60)
            wait_before = fault.get("wait_before_seconds", 0)

            if wait_before > 0:
                print(f"  [{i+1}/{len(faults)}] Waiting {wait_before}s before next fault...")
                await asyncio.sleep(wait_before)

            injected_at = datetime.now(timezone.utc).isoformat()
            print(f"  [{i+1}/{len(faults)}] Injecting {fault_type} on {service} (duration: {duration_s}s)")

            result = await inject_fault(client, service, fault_type, params)
            print(f"    -> {result}")

            await asyncio.sleep(duration_s)

            cleared_at = datetime.now(timezone.utc).isoformat()
            await reset_fault(client, service)
            print(f"    -> Reset {service}")

            ground_truth.append({
                "scenario_id":      scenario_id,
                "fault_type":       fault_type,
                "target_service":   service,
                "target_endpoint":  fault.get("target_endpoint", "*"),
                "injected_at":      injected_at,
                "cleared_at":       cleared_at,
                "magnitude":        params,
            })

    with open(output_file, "w") as f:
        json.dump(ground_truth, f, indent=2)

    print(f"\nGround-truth log written to: {output_file}")
    print(f"Total injection windows recorded: {len(ground_truth)}")

def main():
    parser = argparse.ArgumentParser(description="Cassandra scenario runner")
    parser.add_argument("scenario", help="Path to scenario YAML file")
    parser.add_argument("--output", default="ground_truth.json", help="Output ground-truth log file")
    args = parser.parse_args()
    asyncio.run(run_scenario(args.scenario, args.output))

if __name__ == "__main__":
    main()
