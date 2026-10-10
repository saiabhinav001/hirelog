from __future__ import annotations

import os
import sys
from pathlib import Path

# Tests assume the exam module is disabled unless a test enables it; a developer's .env or shell must not change that.
os.environ["EXAMS_ENABLED"] = "false"

# Keep backend/app importable no matter where pytest is invoked from.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
