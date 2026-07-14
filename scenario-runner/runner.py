#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import yaml
import httpx
from datetime import datetime, timezone, timedelta
from pathlib import Path

SERVICES = {
    "orders":   "http://localhost:8001",
    "payments": "http://localhost:8002",
}

# API du registre de deploiements (control plane). Surchargeable pour cibler
# le conteneur deploy-api depuis un autre reseau.
DEPLOY_API_URL = os.environ.get("DEPLOY_API_URL", "http://localhost:8090")

# Repertoire des ground truth JSON, resolu par rapport a l'emplacement de ce
# fichier (et non au CWD) pour que le correlator les retrouve toujours.
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Fenetre exacte utilisee par scraper.py dans ses requetes Prometheus rate([5m]).
# Apres cleared_at d'une injection, les erreurs restent presentes dans cette
# fenetre et peuvent faire fire le detecteur. Attendre ce delai entre deux runs
# garantit qu'aucun residu du run precedent n'influence le run suivant.
PROMETHEUS_RESIDUE_WINDOW = 300  # secondes

async def inject_fault(client: httpx.AsyncClient, service: str, fault_type: str, params: dict):
    base_url = SERVICES[service]
    url = f"{base_url}/faults/{fault_type}"
    response = await client.post(url, json=params, timeout=10)
    response.raise_for_status()
    return response.json()

async def reset_fault(client: httpx.AsyncClient, service: str):
    base_url = SERVICES[service]
    await client.post(f"{base_url}/faults/reset", timeout=10)


async def register_deploy(client: httpx.AsyncClient, deploy: dict) -> dict | None:
    """
    Enregistre un evenement de deploiement via l'API deploy-api.
    Best-effort : si l'API est injoignable, on logue et on continue le scenario.
    """
    payload = {
        "service": deploy["service"],
        "version": deploy["version"],
    }
    if "metadata" in deploy:
        payload["metadata"] = deploy["metadata"]
    try:
        resp = await client.post(f"{DEPLOY_API_URL}/deploys", json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        print(f"  Deploy enregistre: {data['service']} {data['version']} "
              f"(id={data['deploy_id']}, deployed_at={data['deployed_at']})")
        return data
    except Exception as e:
        print(f"  [WARN] Enregistrement du deploy echoue ({DEPLOY_API_URL}): {e}")
        return None

def _write_output(output_file: str, duration_override, faults: list, deploy: dict | None = None):
    output = {"duration_override": duration_override, "faults": faults}
    if deploy is not None:
        output["deploy"] = deploy
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2)


async def run_scenario(scenario_file: str, output_file: str | None = None, duration_override: int | None = None):
    with open(scenario_file) as f:
        scenario = yaml.safe_load(f)

    scenario_id = scenario.get("id", Path(scenario_file).stem)
    faults = scenario.get("faults", [])
    deploy_spec = scenario.get("deploy")
    ground_truth = []
    deploy_record = None

    if output_file is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        output_file = str(RESULTS_DIR / f"{scenario_id}_{timestamp}.json")

    print(f"Running scenario: {scenario_id}")
    print(f"Total faults: {len(faults)}")
    if duration_override is not None:
        print(f"duration_override: {duration_override}s (YAML values ignored)")

    async with httpx.AsyncClient() as client:
        # Enregistre le deploiement AVANT les injections : il doit preceder
        # l'onset de la regression pour que le correlator l'attribue.
        if deploy_spec:
            deploy_record = await register_deploy(client, deploy_spec)

        for i, fault in enumerate(faults):
            service     = fault["service"]
            fault_type  = fault["type"]
            params      = fault.get("params", {})
            duration_s  = duration_override if duration_override is not None else fault.get("duration_seconds", 60)
            wait_before = fault.get("wait_before_seconds", 0)

            if wait_before > 0:
                print(f"  [{i+1}/{len(faults)}] Waiting {wait_before}s before next fault...")
                await asyncio.sleep(wait_before)

            injected_at = datetime.now(timezone.utc)
            print(f"  [{i+1}/{len(faults)}] Injecting {fault_type} on {service} (duration: {duration_s}s)")

            result = await inject_fault(client, service, fault_type, params)
            print(f"    -> {result}")

            # Write partial ground truth immediately so correlator can match
            # while the fault is still active. Use expected cleared_at so
            # correlator needs no changes (cleared_at=null would be skipped).
            expected_cleared_at = injected_at + timedelta(seconds=duration_s)
            partial_entry = {
                "scenario_id":     scenario_id,
                "fault_type":      fault_type,
                "target_service":  service,
                "target_endpoint": fault.get("target_endpoint", "*"),
                "injected_at":     injected_at.isoformat(),
                "cleared_at":      expected_cleared_at.isoformat(),
                "magnitude":       params,
            }
            _write_output(output_file, duration_override, ground_truth + [partial_entry], deploy_record)
            print(f"    -> Partial ground-truth written (cleared_at expected: {expected_cleared_at.isoformat()})")

            await asyncio.sleep(duration_s)

            cleared_at = datetime.now(timezone.utc)
            await reset_fault(client, service)
            print(f"    -> Reset {service}")

            ground_truth.append({
                "scenario_id":     scenario_id,
                "fault_type":      fault_type,
                "target_service":  service,
                "target_endpoint": fault.get("target_endpoint", "*"),
                "injected_at":     injected_at.isoformat(),
                "cleared_at":      cleared_at.isoformat(),
                "magnitude":       params,
            })
            # Overwrite with final cleared_at for this fault
            _write_output(output_file, duration_override, ground_truth, deploy_record)

    print(f"\nGround-truth log written to: {output_file}")
    print(f"Total injection windows recorded: {len(ground_truth)}")

def main():
    parser = argparse.ArgumentParser(description="Cassandra scenario runner")
    parser.add_argument("scenario", help="Path to scenario YAML file")
    parser.add_argument("--output", default=None, help="Output ground-truth log file (default: results/{scenario_id}_{timestamp}.json)")
    parser.add_argument(
        "--duration-override",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Override duration_seconds for every fault (YAML values ignored)",
    )
    args = parser.parse_args()
    asyncio.run(run_scenario(args.scenario, args.output, args.duration_override))

if __name__ == "__main__":
    main()
