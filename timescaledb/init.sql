CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------------
-- metrics : hypertable brute (reservee aux exports directs OTel eventuels)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metrics (
    time        TIMESTAMPTZ NOT NULL,
    endpoint_id TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value       DOUBLE PRECISION NOT NULL
);

SELECT create_hypertable('metrics', 'time', if_not_exists => TRUE);

-- ---------------------------------------------------------------------------
-- endpoint_features : features RED alimentees par scraper.py (poll Prometheus)
-- Hypertable time-series, une ligne par (endpoint, cycle de 60s).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS endpoint_features (
    time           TIMESTAMPTZ NOT NULL,
    endpoint_id    TEXT NOT NULL,
    service        TEXT NOT NULL,
    rps            DOUBLE PRECISION,
    p50_ms         DOUBLE PRECISION,
    p95_ms         DOUBLE PRECISION,
    p99_ms         DOUBLE PRECISION,
    error_rate_5xx DOUBLE PRECISION,
    error_rate_4xx DOUBLE PRECISION
);

SELECT create_hypertable('endpoint_features', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_ef_endpoint_time
    ON endpoint_features (endpoint_id, time DESC);

-- ---------------------------------------------------------------------------
-- endpoint_baseline : baseline saisonniere (dow x heure) calculee par
-- baseline_job.py via percentile_cont sur 14 jours glissants.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS endpoint_baseline (
    endpoint_id  TEXT     NOT NULL,
    metric       TEXT     NOT NULL,
    dow          SMALLINT NOT NULL,
    hour_bucket  SMALLINT NOT NULL,
    p10          DOUBLE PRECISION,
    p50          DOUBLE PRECISION,
    p90          DOUBLE PRECISION,
    sample_count INTEGER,
    updated_at   TIMESTAMPTZ,
    PRIMARY KEY (endpoint_id, metric, dow, hour_bucket)
);

-- ---------------------------------------------------------------------------
-- alerts : etat courant de l'alerting (une ligne par endpoint x signal).
-- La state machine (detector.py) upsert sur (endpoint_id, signal_type).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    alert_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    endpoint_id           TEXT NOT NULL,
    signal_type           TEXT NOT NULL,
    state                 TEXT NOT NULL,
    opened_at             TIMESTAMPTZ,
    resolved_at           TIMESTAMPTZ,
    severity              TEXT,
    score                 DOUBLE PRECISION,
    layer                 TEXT,
    contributing_features JSONB,
    updated_at            TIMESTAMPTZ DEFAULT now(),
    pending_since         TIMESTAMPTZ,
    resolving_since       TIMESTAMPTZ,
    raw_value             DOUBLE PRECISION,
    pending_count         INTEGER DEFAULT 0,
    resolving_count       INTEGER DEFAULT 0,
    suspected_deploy_id   TEXT,
    imputation_score      DOUBLE PRECISION,
    suspected_fault       TEXT,
    explanation           JSONB
);

-- Contrainte d'unicite utilisee par le ON CONFLICT de la state machine.
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_active
    ON alerts (endpoint_id, signal_type);

-- ---------------------------------------------------------------------------
-- alerts_history : journal append-only des transitions -> FIRING.
-- Alimente par le trigger trg_alerts_firing, lu par le dashboard Grafana.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts_history (
    history_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id         UUID NOT NULL,
    endpoint_id      TEXT NOT NULL,
    signal_type      TEXT NOT NULL,
    opened_at        TIMESTAMPTZ,
    layer            TEXT,
    suspected_fault  TEXT,
    imputation_score DOUBLE PRECISION,
    score            DOUBLE PRECISION,
    severity         TEXT,
    raw_value        DOUBLE PRECISION,
    fired_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trigger : a chaque passage OK/pending/resolving -> firing, on ecrit une
-- ligne d'historique. Idempotent au niveau du couple fonction + trigger.
CREATE OR REPLACE FUNCTION log_firing_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.state = 'firing' AND (OLD.state IS DISTINCT FROM 'firing') THEN
        INSERT INTO alerts_history (
            alert_id, endpoint_id, signal_type,
            opened_at, layer, suspected_fault,
            imputation_score, score, severity, raw_value
        ) VALUES (
            NEW.alert_id, NEW.endpoint_id, NEW.signal_type,
            NEW.opened_at, NEW.layer, NEW.suspected_fault,
            NEW.imputation_score, NEW.score, NEW.severity, NEW.raw_value
        );
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_alerts_firing ON alerts;
CREATE TRIGGER trg_alerts_firing
    AFTER UPDATE ON alerts
    FOR EACH ROW
    EXECUTE FUNCTION log_firing_transition();

-- ---------------------------------------------------------------------------
-- deploy_events : registre des deploiements (control plane).
-- Alimente par l'API deploy (deploy_api.py) et interroge par le correlator
-- pour attribuer une regression a un deploiement recent (suspected_deploy_id).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS deploy_events (
    deploy_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service     TEXT NOT NULL,
    version     TEXT NOT NULL,
    deployed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata    JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_deploy_events_time
    ON deploy_events (deployed_at DESC);
CREATE INDEX IF NOT EXISTS idx_deploy_events_service_time
    ON deploy_events (service, deployed_at DESC);

-- ---------------------------------------------------------------------------
-- anomalies : anomaly store (spec 6.3). Un enregistrement par fenetre scoree
-- (endpoint x cycle), ecrit par detector.py. Alimente la timeline de score
-- anomalie du dashboard Grafana (spec 5.9) et trace la provenance des couches.
--   layer     : static | baseline | iforest | combined
--   direction : degradation | improvement | normal
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id            UUID NOT NULL DEFAULT gen_random_uuid(),
    endpoint_id           TEXT NOT NULL,
    signal_type           TEXT NOT NULL DEFAULT 'p99_ms',
    detected_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_start          TIMESTAMPTZ,
    score                 DOUBLE PRECISION,   -- score combine calibre [0,1]
    layer                 TEXT,
    direction             TEXT,
    contributing_features JSONB
);

SELECT create_hypertable('anomalies', 'detected_at', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_anomalies_endpoint_time
    ON anomalies (endpoint_id, detected_at DESC);

-- ---------------------------------------------------------------------------
-- eval_runs : resultats de campagne d'evaluation (spec 9.3). Une ligne par
-- (run, fault_type), plus une ligne fault_type='OVERALL' agregee. Ecrite par
-- evaluate_layered.py --persist, lue par le dashboard d'evaluation Grafana.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eval_runs (
    run_id              UUID NOT NULL DEFAULT gen_random_uuid(),
    run_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_set           TEXT,
    fault_type          TEXT NOT NULL,
    magnitude           TEXT NOT NULL DEFAULT 'all',  -- 'all' | 'core' | 'stress' (sensibilite)
    n                   INTEGER,
    dr_static           DOUBLE PRECISION,   -- detection rate static (layer 0)
    dr_layered          DOUBLE PRECISION,   -- detection rate layered (baseline+ML)
    delay_static_s      DOUBLE PRECISION,
    delay_layered_s     DOUBLE PRECISION,
    fp_static           INTEGER,            -- rempli sur la ligne OVERALL
    fp_layered          INTEGER,
    fp_per_hour_static  DOUBLE PRECISION,
    fp_per_hour_layered DOUBLE PRECISION,
    span_hours          DOUBLE PRECISION,
    PRIMARY KEY (run_id, fault_type, magnitude)
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_time ON eval_runs (run_at DESC);

-- ---------------------------------------------------------------------------
-- Compression + retention des hypertables time-series (perf + espace disque).
-- Ces tables sont append-only (jamais d'UPDATE sur les vieilles lignes), donc la
-- compression est sans risque : on compresse les chunks > 7 jours et on purge
-- les chunks > 90 jours. Sans ces politiques, endpoint_features et anomalies
-- (~7 200 lignes/jour chacune) croissent sans limite.
--
-- Les ALTER ... SET compress sont enveloppes dans un DO/EXCEPTION pour rester
-- idempotents si init.sql est rejoue sur une base existante ; les policies
-- utilisent if_not_exists => TRUE.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    ALTER TABLE endpoint_features SET (
        timescaledb.compress,
        timescaledb.compress_segmentby = 'endpoint_id',
        timescaledb.compress_orderby = 'time DESC'
    );
EXCEPTION WHEN others THEN
    RAISE NOTICE 'compression endpoint_features deja configuree';
END $$;

SELECT add_compression_policy('endpoint_features', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy('endpoint_features', INTERVAL '90 days', if_not_exists => TRUE);

DO $$
BEGIN
    ALTER TABLE anomalies SET (
        timescaledb.compress,
        timescaledb.compress_segmentby = 'endpoint_id',
        timescaledb.compress_orderby = 'detected_at DESC'
    );
EXCEPTION WHEN others THEN
    RAISE NOTICE 'compression anomalies deja configuree';
END $$;

SELECT add_compression_policy('anomalies', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy('anomalies', INTERVAL '90 days', if_not_exists => TRUE);
