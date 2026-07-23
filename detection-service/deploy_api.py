"""
deploy_api.py -- API du registre de deploiements (control plane).

Une plateforme CI/CD (ou le scenario-runner pour les demos) enregistre un
deploiement via POST /deploys. Le correlator interroge ensuite deploy_events
pour attribuer une regression a un deploiement recent.

Endpoints :
  POST /deploys        enregistre un deploiement, retourne son deploy_id
  GET  /deploys        liste les deploiements recents (filtres service / since_minutes)
  GET  /health         liveness

Lancement :
  uvicorn deploy_api:app --host 0.0.0.0 --port 8090
"""

import os
import logging
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_URL = os.environ.get("DATABASE_URL", "postgresql://cassandra:cassandra@localhost:5434/cassandra")

# Cle CI dediee pour l'endpoint de deploiements (spec 12). Si definie, POST /deploys
# exige l'en-tete X-API-Key. Si absente : mode dev (non authentifie) avec avertissement.
DEPLOY_API_KEY = os.environ.get("DEPLOY_API_KEY", "").strip()
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

if DEPLOY_API_KEY:
    log.info("Auth par cle API activee sur POST /deploys (X-API-Key)")
else:
    log.warning("DEPLOY_API_KEY non defini -- POST /deploys est NON authentifie (mode dev)")


def require_api_key(key: str | None = Security(_api_key_header)):
    """Exige X-API-Key si une cle est configuree ; no-op en mode dev."""
    if not DEPLOY_API_KEY:
        return
    if key != DEPLOY_API_KEY:
        raise HTTPException(status_code=401, detail="cle API invalide ou absente (X-API-Key)")


app = FastAPI(title="Cassandra Deploy Events API", version="1.0.0")


def _connect():
    return psycopg2.connect(DB_URL)


class DeployIn(BaseModel):
    service: str = Field(..., min_length=1, description="Service deploye (ex: orders)")
    version: str = Field(..., min_length=1, description="Version / commit / tag deploye")
    deployed_at: datetime | None = Field(
        None, description="Horodatage du deploiement (defaut: maintenant, UTC)"
    )
    metadata: dict | None = Field(None, description="Metadonnees libres (auteur, PR, etc.)")


class DeployOut(BaseModel):
    deploy_id: str
    service: str
    version: str
    deployed_at: datetime
    metadata: dict | None = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/deploys", response_model=DeployOut, status_code=201,
          dependencies=[Security(require_api_key)])
def register_deploy(deploy: DeployIn):
    deployed_at = deploy.deployed_at or datetime.now(timezone.utc)
    if deployed_at.tzinfo is None:
        deployed_at = deployed_at.replace(tzinfo=timezone.utc)

    metadata = psycopg2.extras.Json(deploy.metadata) if deploy.metadata is not None else None

    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO deploy_events (service, version, deployed_at, metadata)
                    VALUES (%s, %s, %s, %s)
                    RETURNING deploy_id, service, version, deployed_at, metadata
                    """,
                    (deploy.service, deploy.version, deployed_at, metadata),
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.error(f"register_deploy failed: {e}")
        raise HTTPException(status_code=500, detail="database error")

    log.info(f"Deploy enregistre: {row[1]} {row[2]} @ {row[3].isoformat()} (id={row[0]})")
    return DeployOut(
        deploy_id=str(row[0]),
        service=row[1],
        version=row[2],
        deployed_at=row[3],
        metadata=row[4],
    )


@app.get("/deploys", response_model=list[DeployOut])
def list_deploys(
    service: str | None = Query(None, description="Filtrer par service"),
    since_minutes: int = Query(1440, ge=1, description="Fenetre en minutes (defaut: 24h)"),
    limit: int = Query(50, ge=1, le=500),
):
    clauses = ["deployed_at >= now() - make_interval(mins => %s)"]
    params: list = [since_minutes]
    if service:
        clauses.append("service = %s")
        params.append(service)
    where = " AND ".join(clauses)
    params.append(limit)

    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT deploy_id, service, version, deployed_at, metadata
                    FROM deploy_events
                    WHERE {where}
                    ORDER BY deployed_at DESC
                    LIMIT %s
                    """,
                    params,
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        log.error(f"list_deploys failed: {e}")
        raise HTTPException(status_code=500, detail="database error")

    return [
        DeployOut(
            deploy_id=str(r[0]),
            service=r[1],
            version=r[2],
            deployed_at=r[3],
            metadata=r[4],
        )
        for r in rows
    ]
