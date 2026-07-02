CREATE EXTENSION IF NOT EXISTS timescaledb;

-- table principale qui recevra les métriques RED générées par spanmetrics
CREATE TABLE IF NOT EXISTS metrics (
    time        TIMESTAMPTZ NOT NULL,
    endpoint_id TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value       DOUBLE PRECISION NOT NULL
);

SELECT create_hypertable('metrics', 'time', if_not_exists => TRUE);
