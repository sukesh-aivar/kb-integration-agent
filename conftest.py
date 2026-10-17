"""Pytest path setup so tests import ``kb_agent`` and ``evals`` without install.

Keeping this at the repo root means ``make test`` works on a clean clone with no
editable install step - part of the "make test must pass offline" requirement.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

for path in (ROOT, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
