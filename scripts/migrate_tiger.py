"""Inspect actual server first; apply the reviewed schema only with --apply."""
import argparse
from pathlib import Path

from sqlalchemy import text

from backend.app.config import settings
from backend.app.db import Database
from backend.app.service import Guardian


def statements(sql):
    """Drop comment lines before splitting so a ';' inside a comment cannot break a statement."""
    code='\n'.join(line for line in sql.splitlines() if not line.lstrip().startswith('--'))
    return [s.strip() for s in code.split(';') if s.strip()]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if not settings.database_url.startswith('postgresql'):
        raise SystemExit('Set DATABASE_URL to the Tiger Data postgresql+psycopg connection string first.')
    db=Database(settings.database_url)
    with db.engine.connect() as conn:
        pg=conn.execute(text('SHOW server_version')).scalar()
        ts=conn.execute(text("SELECT extversion FROM pg_extension WHERE extname='timescaledb'")).scalar()
        has_range=conn.execute(text("SELECT count(*) FROM pg_proc WHERE proname='by_range'")).scalar()
        print(f'Provisioned PostgreSQL {pg}; TimescaleDB {ts}; by_range available: {bool(has_range)}')
        if not ts or not has_range:raise SystemExit('This migration requires the TimescaleDB generalized hypertable API. Review service-specific syntax before applying.')
    if not args.apply:
        print('Read-only check complete. Review deploy/tiger.sql, then rerun with --apply.')
        return
    db.create()
    Guardian(db, settings).backfill_stages()
    with db.engine.begin() as conn:
        for statement in statements(Path('deploy/tiger.sql').read_text()):
            conn.execute(text(statement))
    with db.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
        conn.execute(text("CALL refresh_continuous_aggregate('hourly_incidents', NOW() - INTERVAL '8 days', date_trunc('hour', NOW()))"))
        conn.execute(text("CALL refresh_continuous_aggregate('hourly_movement', NOW() - INTERVAL '2 days', date_trunc('hour', NOW()))"))
        conn.execute(text("CALL refresh_continuous_aggregate('daily_baseline', NOW() - INTERVAL '8 days', date_trunc('day', NOW()))"))
    print('Hypertables and continuous aggregates created; recent completed buckets backfilled.')


if __name__=='__main__':main()
