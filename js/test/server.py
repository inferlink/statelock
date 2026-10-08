"""Start the Python browser-test proxy (tests/browser_support.py) for the JS tests.

Prints one JSON line {"base": ...} when ready and runs until stdin closes.
Keys: slk_test_<agent_id>. Needs the Python package installed (pip install -e ".[dev]").
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

from browser_support import running_server


class _TempDirs:
    def mktemp(self, name: str) -> Path:
        return Path(tempfile.mkdtemp(prefix=f"{name}-"))


with running_server(_TempDirs()) as server:  # type: ignore[arg-type]
    print(json.dumps({"base": server["base"], "root": str(server["root"])}), flush=True)
    sys.stdin.read()
