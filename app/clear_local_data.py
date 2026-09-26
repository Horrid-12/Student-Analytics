"""Phase 5.2 localhost dev cleanup.

All persistent storage is Postgres-first when ``DATABASE_URL`` is configured;
the repo-root SQLite files are the local dev fallback. This script clears the
locally-stored analysis artifacts ("the run-history flood") that pile up from
local testing, leaving the backend (Neon) alone.

Usage:
    python -m app.clear_local_data          # wipe analysis runs only
    python -m app.clear_local_data --all    # also wipe users/support/accounts/reference DBs

Deleted files are printed; the run stores are versioned caches, so nothing
that exists only locally is lost from the authoritative backend store.
"""

import argparse
import math
import shutil
import sys
from pathlib import Path

from app import accounts, auth, crosscheck, storage, support

#: Stores that hold pure analysis/roster artifacts — safe to clear by default.
ANALYSIS_FILES = [storage.DB_PATH]

#: Identity/feature stores — only wiped with ``--all``.
FEATURE_FILES = [auth.USERS_DB, support.DB_PATH, accounts.ACCOUNTS_DB, crosscheck.REFERENCE_DB]

#: Legacy files removed at the Phase-3.10 cutover — delete if they ever resurface.
STALE_FILES = [
    Path("requirements.txt.dead"),
    Path("runtime.txt"),
]
# NOTE: `.streamlit/` is deliberately NOT a cleanup target — although it is the
# old Streamlit stack's config dir, legacy credential loaders still fall back to
# `.streamlit/secrets.toml`, and the file is gitignored. The Phase 5.3 single
# secrets source is `.env.local` (auto-loaded by app/env.py); both must survive.
# Deleting `.streamlit` silently kills Google sign-in on localhost (BUG-114).


def _wipe_many(paths: list[Path], label: str) -> int:
    count = 0
    for path in paths:
        if not path.exists():
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            print(f"  ! could not remove {path}: {exc}", file=sys.stderr)
            continue
        print(f"  - {path} ({label})")
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true", help="also wipe users/support/accounts/reference DBs")
    parser.add_argument("--dry-run", action="store_true", help="list what would be removed without removing it")
    args = parser.parse_args()

    targets = list(ANALYSIS_FILES) + (list(FEATURE_FILES) if args.all else []) + list(STALE_FILES)
    if args.dry_run:
        print("Would remove:")
        for path in targets:
            if path.exists():
                print(f"  - {path}")
        return 0

    print("Clearing local data…")
    removed = _wipe_many(targets, "all" if args.all else "analysis")
    print(f"Done — removed {removed} file(s). Backend (Neon/Postgres) data is untouched.")
    if not args.all:
        print("Tip: `python -m app.clear_local_data --all` also wipes the local users/support/accounts/reference DBs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())