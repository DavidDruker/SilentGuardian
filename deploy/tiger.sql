-- Run only after scripts/migrate_tiger.py reports compatible provisioned service.
-- Core relational tables are created by SQLAlchemy. Time-series tables become hypertables here.
SELECT create_hypertable('pose_windows', by_range('ts'), if_not_exists => TRUE, migrate_data => TRUE);
SELECT create_hypertable('event_stage_log', by_range('ts'), if_not_exists => TRUE, migrate_data => TRUE);
SELECT create_hypertable('monitoring_coverage', by_range('bucket'), if_not_exists => TRUE, migrate_data => TRUE);

CREATE MATERIALIZED VIEW IF NOT EXISTS hourly_incidents
WITH (timescaledb.continuous) AS
SELECT person_id, time_bucket(INTERVAL '1 hour', ts) AS bucket,
       count(*) AS incident_count
FROM event_stage_log
WHERE stage = 'incident_opened' AND source <> 'demo_call'
GROUP BY person_id, bucket
WITH NO DATA;

SELECT add_continuous_aggregate_policy('hourly_incidents',
  start_offset => INTERVAL '8 days', end_offset => INTERVAL '1 hour',
  schedule_interval => INTERVAL '1 hour', if_not_exists => TRUE);

CREATE MATERIALIZED VIEW IF NOT EXISTS hourly_movement
WITH (timescaledb.continuous) AS
SELECT person_id, time_bucket(INTERVAL '1 hour', ts) AS bucket,
       count(*) AS window_count, avg(quality) AS mean_quality,
       avg((features->>'sway')::double precision) AS mean_sway,
       avg((features->>'hip_speed')::double precision) AS mean_hip_speed
FROM pose_windows
GROUP BY person_id, bucket
WITH NO DATA;

SELECT add_continuous_aggregate_policy('hourly_movement',
  start_offset => INTERVAL '2 days', end_offset => INTERVAL '1 hour',
  schedule_interval => INTERVAL '1 hour', if_not_exists => TRUE);

-- Mean/stddev fallback deliberately avoids version-dependent percentile support.
-- Synthetic backfill is kept in a separate group and never trains a personal baseline.
CREATE MATERIALIZED VIEW IF NOT EXISTS daily_baseline
WITH (timescaledb.continuous) AS
SELECT person_id, time_bucket(INTERVAL '1 day', ts) AS bucket,
       features->>'posture' AS posture,
       COALESCE((features->>'synthetic_trend')::boolean, false) AS synthetic,
       count(*) AS window_count,
       avg((features->>'sway')::double precision) AS mean_sway,
       stddev_pop((features->>'sway')::double precision) AS sd_sway,
       avg((features->>'hip_speed')::double precision) AS mean_hip_speed
FROM pose_windows
WHERE quality >= 0.8 AND (COALESCE((features->>'baseline_eligible')::boolean, false)
                       OR COALESCE((features->>'synthetic_trend')::boolean, false))
GROUP BY person_id, bucket, posture, synthetic
WITH NO DATA;

SELECT add_continuous_aggregate_policy('daily_baseline',
  start_offset => INTERVAL '7 days', end_offset => INTERVAL '1 day',
  schedule_interval => INTERVAL '1 hour', if_not_exists => TRUE);
