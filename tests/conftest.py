import os
import sys
from pathlib import Path

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