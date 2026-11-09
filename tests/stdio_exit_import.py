"""Test-only plugin import that exits 0 at import time (never installed).

Imported lazily by tests/stdio_helper.py under the production guard to
reproduce a plugin whose import raises SystemExit(0) via load_registry.
"""

raise SystemExit(0)
