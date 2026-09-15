import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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
    """Give every test its own analytics-history and users DB so endpoint tests
    never touch the real ./analytics_history.db or ./users.db (Phase 4.7 auth)."""
    from app import auth, storage

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "history.db")
    monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")