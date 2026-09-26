import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# app/env.py auto-loads .env.local (incl. the real DATABASE_URL + credentials)
# for the running app (Phase 5.3); under pytest we want the tmp-SQLite fallback
# path, so mark the run before any app module is imported. TEST_DATABASE_URL
# (set explicitly by the user) stays untouched for the pg-layer tests.
os.environ["___APP_UNDER_PYTEST___"] = "1"

# Same characterization suite, two consumers:
#   default                     → legacy ``services.py`` (frozen reference)
#   MODULE_UNDER_TEST=app.services → ported ``app/services.py``
# Alias the target module under the name ``services`` so tests/test_services.py
# and anything else importing it run unmodified against either implementation.
MUT = os.environ.get("MODULE_UNDER_TEST", "").strip()
if MUT:
    import importlib

    sys.modules["services"] = importlib.import_module(MUT)


@pytest.fixture(autouse=True)
def _isolate_databases(tmp_path, monkeypatch):
    """Give every test its own analytics-history, users, and support-ticket DB
    so endpoint tests never touch the real ./analytics_history.db, ./users.db,
    or ./support.db (Phase 4.7 auth).

    Baseline allowlist so legacy seed/login helpers (@college.edu, @test.local)
    keep authenticating; the strict Phase 4.7.2 domain gate is asserted by
    dedicated tests (test_google_auth / test_auth) that override this env."""
    from app import accounts, auth, crosscheck, storage, support

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "history.db")
    monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setattr(support, "DB_PATH", tmp_path / "support.db")
    monkeypatch.setattr(accounts, "ACCOUNTS_DB", tmp_path / "accounts.db")
    monkeypatch.setattr(crosscheck, "REFERENCE_DB", tmp_path / "reference.db")
    monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "college.edu mitwpu.edu.in test.local")