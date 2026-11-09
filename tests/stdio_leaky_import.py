"""Import-time leaker for the stdio-guard tests (test-only, never installed).

Importing this module prints once. The lazy-import scenario imports it
inside a handler, so the print happens while the guard is up.
"""

print("STRAY-MARKER-import-time-leak")
