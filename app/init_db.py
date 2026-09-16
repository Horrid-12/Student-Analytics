r"""Provision the Postgres schema: ``python -m app.init_db``

Creates every table in ``app/schema.sql`` on the configured database
(``DATABASE_URL`` / ``POSTGRES_URL`` / ``TEST_DATABASE_URL``). Idempotent —
safe to run repeatedly. Prints connection health so a Neon setup can be
verified before the app starts.

Example:
    $env:DATABASE_URL="postgresql://user:pass@host/db"; .\.venv\Scripts\python.exe -m app.init_db
"""

import sys

from app import database, db


def main() -> int:
    if not database.pool_url():
        print("No DATABASE_URL/POSTGRES_URL/TEST_DATABASE_URL found — nothing to do (SQLite fallback active).")
        return 2
    healthy = db.schema_healthy()
    print(f"Postgres reachable: {healthy}")
    if not healthy:
        print(f"Using: {database.pool_url().split('@')[-1] if database.pool_url() else '<none>'}")
        return 1
    if db.init_schema():
        print("Schema OK — all Phase 4.9 tables present.")
        return 0
    print("Schema init failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())