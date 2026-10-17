"""Inspect actual server first; apply the reviewed schema only with --apply."""
import argparse
from pathlib import Path

from sqlalchemy import text

from backend.app.config import settings
from backend.app.db import Database


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
    with db.engine.begin() as conn:
        sql=Path('deploy/tiger.sql').read_text()
        for statement in sql.split(';'):
            if statement.strip():conn.execute(text(statement))
    print('Hypertable and hourly aggregate created. No paid infrastructure was provisioned by this script.')


if __name__=='__main__':main()
