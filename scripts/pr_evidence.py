#!/usr/bin/env python3
"""Build the before/after evidence comment for a PR (docs/REVIEW.md, "Evidence").

Runs every scenario in the head tree's scenarios file against two source
trees, the PR's base and its head, and writes one Markdown comment body:
a summary table, then a diff for every scenario whose output changed.

Safety: each run gets a fresh, empty HOME and a minimal environment (the same
variables the Muse SDK gives drop-in commands), so no real config, secret, or
saved snapshot can be read. The script refuses to start if it looks like it
is running on a machine with a real Housesitter setup.

Usage:
    python scripts/pr_evidence.py --base-tree BASE --head-tree HEAD --out comment.md
    python scripts/pr_evidence.py --base-tree BASE --head-tree HEAD --out comment.md \
        --ui-artifacts UI --repo OWNER/NAME --pr 12 --head-sha <40 hex>

The Web UI section is built here from the artifact's ui_manifest.json, with
every value re-validated; pre-rendered markdown in the artifact is ignored.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

MARKER = "<!-- housesitter-evidence -->"
SCENARIOS = Path("tests/evidence/scenarios.json")
MAX_COMMENT_BYTES = 60_000  # GitHub's cap is 65,536; posts stay under 60,000
# Longest comment marker the publisher ever stamps (review round 3,
# finding 3): the 40-hex head SHA, the longest outcome word, and 19 digits
# each of CI run id and attempt — far beyond GitHub's ~10-digit run ids,
# so any real marker fits. Every fitting budget below reserves the growth
# from the bare marker to this maximum, so a body that fits the budget
# still fits after the publisher stamps it. The CI-side check
# (evidence_checks.check, which renders without run ids) uses the same
# reservation, so both sides agree; the final size check still guards the
# posted body itself.
STAMPED_MARKER_MAX_BYTES = len(
    "<!-- housesitter-evidence head="
    + "0" * 40
    + " run="
    + "0" * 19
    + " attempt="
    + "0" * 19
    + " outcome=evidence -->"
)
MARKER_GROWTH_RESERVE = STAMPED_MARKER_MAX_BYTES - len(MARKER.encode())
RUN_TIMEOUT_S = 60
VOLATILE = "<volatile>"

# Fake-controller evidence (Decision 9, option A). A scenario with a "fake"
# key runs the HEAD tree's scripts/fake_unifi.py on 127.0.0.1 with an
# ephemeral port, serving the HEAD tree's fixtures, and points the run's
# fresh HOME at it with a written config plus secrets file — the same files
# a user would set up (install/config.example.json). Both the base and the
# head run see the SAME fake, so the diff shows only code changes. The base
# tree's fake and fixtures are never used.
#
#   "fake": {"fixture_set": "typical",              # typical|empty|large
#            "reject_login": false,                 # 401 on every login
#            "bad_fingerprint": false},             # pin a wrong fingerprint
#   "steps": [{"argv": [...]}, ...]                 # sharing one HOME
#   "render": "data-counts"                        # optional: project every
#                                                 # list inside "data" to an
#                                                 # "<N items>" count string
#                                                 # (default "full" renders
#                                                 # every record)
#
# Without "steps", one step runs scenario["argv"]. Every step's argv may
# carry $saved_id(N), $more_command(N), or $more_params(N), replaying step
# N's envelope (the follow-up page replays step 0's "more" call exactly).
# Steps render with one header each, so a reviewer can tell which step
# produced which output. "data-counts" keeps scalars, dicts, and everything
# outside "data" (truncated, more, warnings, source, schema) in full, so
# summary counts and paging behaviour still diff; the step header says so.
EVIDENCE_PASSWORD = "PLANTED-SECRET-evidence-unifi-wifi-01"
FAKE_SCRIPT = Path("scripts/fake_unifi.py")
FAKE_BOOT_TIMEOUT_S = 30
FIXTURE_SETS = ("typical", "empty", "large")


class FakeError(Exception):
    """The evidence run itself cannot proceed (missing openssl under
    HOUSESITTER_REQUIRE_TEST_TOOLS=1). Unlike a skip, this aborts the run."""


class _SkipScenario(Exception):
    """A single scenario's fake could not be provided; the scenario reports
    as skipped with this reason instead of being dropped."""


def tools_required() -> bool:
    """True when a missing test prerequisite must fail instead of skip."""
    return os.environ.get("HOUSESITTER_REQUIRE_TEST_TOOLS") == "1"
SHA_PLACEHOLDER = "__HOUSESITTER_EVIDENCE_SHA__"  # must equal ui_evidence's

# A posted evidence SHA is always 40 hex; every image URL carries one
# placeholder until the comment step pins it.
SHA_FINAL_LEN = 40
PLACEHOLDER_GROWTH = SHA_FINAL_LEN - len(SHA_PLACEHOLDER.encode())


def final_len(body: str) -> int:
    """Byte length of the body exactly as it will be posted.

    The marker is stamped with the longest possible marker (see
    STAMPED_MARKER_MAX_BYTES), and every image placeholder counts at its
    posted length, so a body that fits MAX_COMMENT_BYTES here still fits
    after the publisher stamps it and the comment step pins the SHA. For
    an already-stamped body the reserve overestimates slightly, which is
    the safe direction.
    """
    return (
        len(body.encode())
        + MARKER_GROWTH_RESERVE
        + PLACEHOLDER_GROWTH * body.count(SHA_PLACEHOLDER)
    )


# Omission notices name the dropped sections, so their length is bounded by
# construction: at most this many headings, then "and N more". Every
# fitting decision below measures the notice itself (see final_len), so an
# unbounded heading list can never eat the marker reserve (review round 4,
# finding 1).
MAX_OMITTED_SHOWN = 5


def omission_notice(omitted: list[str]) -> str:
    """Bounded notice naming the dropped sections."""
    shown = omitted[:MAX_OMITTED_SHOWN]
    text = "**Omitted to fit a GitHub comment:** " + ", ".join(shown)
    if len(omitted) > len(shown):
        text += f", and {len(omitted) - len(shown)} more"
    return "\n" + text + ". Run `scripts/pr_evidence.py` locally to see them.\n"


def _section_heading(section: str) -> str:
    return section.split("\n", 1)[0].removeprefix("### ")


def assemble_text(top: str, sections: list[str], keep: int) -> str:
    """The top plus the first ``keep`` sections and the omission notice."""
    body = top + "\n".join(sections[:keep])
    if keep != len(sections):
        omitted = [_section_heading(s) for s in sections[keep:]]
        body += omission_notice(omitted)
    return body


def fit_text_sections(top: str, sections: list[str]) -> str:
    """Keep the longest section prefix whose final-form length fits.

    Whole sections only, so a cut never leaves a code fence open. The
    omission notice is part of the measured body, so what is returned
    always fits MAX_COMMENT_BYTES unless even the top plus the notice is
    over budget — then the over-budget body is returned and the caller
    refuses it.
    """
    for keep in range(len(sections), -1, -1):
        body = assemble_text(top, sections, keep)
        if final_len(body) <= MAX_COMMENT_BYTES:
            return body
    return body

# UI section (F4): built here from ui_manifest.json, never pasted from a
# pre-rendered markdown file. The name pattern must stay identical to
# scripts/ui_evidence.py CAPTURE_RE (pinned by test), so only allow-listed
# capture files can become <img> tags.
UI_STATUSES = ("added", "removed", "changed", "unchanged")
UI_CAPTURE_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]|_(?!_)){0,47}"
    r"__(index-phone|index-desktop|index-desktop-dark|eink)"
    r"__(base|head|diff)\.png"
)
UI_VIEW_WIDTHS = {
    "index-phone": 240,
    "index-desktop": 480,
    "index-desktop-dark": 480,
    "eink": 400,
}
UI_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
UI_SHA_RE = re.compile(r"[0-9a-f]{40}")
MAX_UI_TEXT_LEN = 200  # cap per displayed check failure (matches ui_evidence)
MAX_UI_LABEL_LEN = 100  # cap per displayed fixture/view label
# The comment intro names the browser that captured the screenshots. These
# values must stay identical to scripts/ui_evidence.py's (pinned by test):
# this module never imports ui_evidence, so it keeps its own copies.
UI_INTRO = (
    "Fixture house only. `scripts/ui_evidence.py` captures every fixture in "
    "`tests/fixtures/dashboard/` at phone (360x780), desktop (1280x900, light "
    "and dark), and e-ink (800x480) sizes."
)
UI_IMAGES_NOTE = (
    "Images live on the `pr-evidence` branch, pinned to the evidence push, "
    "so they never change afterwards."
)
UI_BROWSER_UNKNOWN = "The manifest does not record a recognised browser."
UI_BROWSER_NAMES = {
    "chromium": "Playwright's bundled Chromium",
    "chrome": "Google Chrome",
    "chrome-beta": "Google Chrome Beta",
    "msedge": "Microsoft Edge",
}
UI_BROWSER_VERSION_RE = re.compile(r"[0-9]+(\.[0-9]+){0,3}")
UI_BROWSER_VERSION_MAX_LEN = 32
# Valid ``crop_px`` heights a manifest may record (Decision 5): ui_evidence
# crops full-page shots down these steps when the push cap would refuse
# them. Must equal ui_evidence.CROP_STEPS[1:] (pinned by test); this module
# never imports ui_evidence, so it keeps its own copy.
UI_CROP_HEIGHTS = (6000, 4000, 3000)


class Refused(Exception):
    pass


def refuse_if_real_setup() -> None:
    if os.environ.get("HOUSESITTER_LIVE"):
        raise Refused("HOUSESITTER_LIVE is set; evidence uses the fixture house only")
    if os.environ.get("HOUSESITTER_CONFIG"):
        raise Refused("HOUSESITTER_CONFIG is set; evidence uses the fixture house only")
    real = Path.home() / ".config" / "housesitter" / "config.json"
    if real.exists():
        raise Refused(f"{real} exists; run evidence on a machine without a real setup")


def load_scenarios(tree: Path) -> tuple[list[dict], list[str]]:
    path = tree / SCENARIOS
    if not path.exists():
        return [], []
    data = json.loads(path.read_text())
    return data["scenarios"], data.get("volatile_keys", [])


def has_package(tree: Path) -> bool:
    return (tree / "src" / "housesitter" / "__init__.py").exists()


def run_scenario(
    tree: Path,
    scenario: dict,
    fake: FakeInfo | None = None,
    volatile: set | frozenset = frozenset(),
    side: str | None = None,
) -> dict:
    """Run one scenario against one tree. Scenarios without a "fake" key
    behave exactly as before. A scenario with one needs the shared fake
    (collect_runs passes it so base and head share one); when fake is None
    the run starts its own from this tree's script (tests and local use —
    never the base tree's script in collect_runs)."""
    if scenario.get("fake") is None:
        return _run_classic(tree, scenario)
    if fake is None:
        try:
            with _self_contained_fake(tree, scenario) as own:
                return _run_fake_scenario(tree, scenario, own, volatile, side=None)
        except _SkipScenario as exc:
            return _skip_result(None, str(exc))
    return _run_fake_scenario(tree, scenario, fake, volatile, side)


def _run_classic(tree: Path, scenario: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="hs-evidence-") as home:
        env = {
            "HOME": home,
            "USER": "evidence",
            "LOGNAME": "evidence",
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONPATH": str((tree / "src").resolve()),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        stdin = scenario.get("stdin")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "housesitter", *scenario["argv"]],
                cwd=home,
                env=env,
                input=(json.dumps(stdin) if stdin is not None else "").encode(),
                capture_output=True,
                timeout=RUN_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "exit": "timeout",
                "stdout": decode(exc.stdout),
                "stderr": decode(exc.stderr),
                "sdk_size": None,
            }
    stdout = decode(proc.stdout)
    return {
        "exit": proc.returncode,
        "stdout": stdout,
        "stderr": decode(proc.stderr),
        # The Muse SDK's own output-size measure (executor.py _clip).
        "sdk_size": len(json.dumps(stdout)) - 2,
    }


def _skip_result(side: str | None, reason: str) -> dict:
    """A visibly skipped scenario: it keeps its table row and an
    unexpected-exit line, and the reason reaches results.json, so a skip is
    reported, never missing. The side label ("base"/"head") makes the two
    texts differ honestly, so the reason also renders in the comment."""
    label = f" on {side}" if side else ""
    stdout = f"(skipped{label}: {reason})\n"
    return {
        "exit": 1,
        "stdout": stdout,
        "stderr": "",
        "sdk_size": len(json.dumps(stdout)) - 2,
        "skipped": reason,
    }


def validate_fake_spec(spec: object, scenario_id: str) -> dict:
    """Check a scenario's "fake" block; authoring bugs fail loudly."""
    if not isinstance(spec, dict):
        raise ValueError(f"scenario {scenario_id!r}: 'fake' must be an object")
    unknown = set(spec) - {"fixture_set", "reject_login", "bad_fingerprint"}
    if unknown:
        raise ValueError(
            f"scenario {scenario_id!r}: unknown 'fake' keys: {sorted(unknown)}"
        )
    fixture_set = spec.get("fixture_set", "typical")
    if fixture_set not in FIXTURE_SETS:
        raise ValueError(
            f"scenario {scenario_id!r}: 'fixture_set' must be one of "
            f"{list(FIXTURE_SETS)}, got {fixture_set!r}"
        )
    for key in ("reject_login", "bad_fingerprint"):
        if key in spec and type(spec[key]) is not bool:
            raise ValueError(f"scenario {scenario_id!r}: {key!r} must be a bool")
    return {
        "fixture_set": fixture_set,
        "reject_login": bool(spec.get("reject_login", False)),
        "bad_fingerprint": bool(spec.get("bad_fingerprint", False)),
    }


RENDER_MODES = ("full", "data-counts")


def validate_render_mode(value: object, scenario_id: str) -> str:
    """Check a scenario's "render" option; authoring bugs fail loudly."""
    mode = "full" if value is None else value
    if mode not in RENDER_MODES:
        raise ValueError(
            f"scenario {scenario_id!r}: 'render' must be one of "
            f"{list(RENDER_MODES)}, got {value!r}"
        )
    return mode


def _lists_to_counts(node: object) -> object:
    if isinstance(node, list):
        return f"<{len(node)} items>"
    if isinstance(node, dict):
        return {key: _lists_to_counts(value) for key, value in node.items()}
    return node


def project_data_counts(envelope: object) -> object:
    """Project an envelope for "data-counts" rendering: every list inside
    "data" (recursively) becomes an "<N items>" string. Scalars, dicts, and
    everything outside "data" pass through untouched."""
    if not isinstance(envelope, dict) or "data" not in envelope:
        return envelope
    projected = dict(envelope)
    projected["data"] = _lists_to_counts(envelope["data"])
    return projected


def scenario_steps(scenario: dict) -> list:
    """The step list a fake scenario runs; authoring bugs fail loudly."""
    steps = scenario.get("steps")
    if steps is None:
        return [{"argv": scenario["argv"]}]
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"scenario {scenario['id']!r}: 'steps' must be a non-empty list")
    for step in steps:
        if not isinstance(step, dict) or set(step) != {"argv"}:
            raise ValueError(
                f"scenario {scenario['id']!r}: each step must be exactly "
                f"{{'argv': [...]}}, got {step!r}"
            )
        argv = step["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(arg, str) for arg in argv)
        ):
            raise ValueError(
                f"scenario {scenario['id']!r}: step argv must be a non-empty "
                f"list of strings, got {argv!r}"
            )
    return steps


class FakeInfo:
    """A running fake controller: its port and the fingerprint it prints."""

    def __init__(self, proc: subprocess.Popen[bytes], port: int, fingerprint: str):
        self.proc = proc
        self.port = port
        self.fingerprint = fingerprint


def make_cert(directory: str) -> tuple:
    """Mint a throwaway self-signed cert with openssl (never committed).

    Returns (cert_path, key_path). Missing openssl fails the run when
    HOUSESITTER_REQUIRE_TEST_TOOLS=1 demands tools, else the caller turns
    it into a per-scenario skip.
    """
    openssl = shutil.which("openssl")
    if openssl is None:
        reason = "openssl is not installed: cannot mint a throwaway evidence cert"
        if tools_required():
            raise FakeError(reason)
        raise _SkipScenario(reason)
    cert_path = str(Path(directory) / "evidence-cert.pem")
    key_path = str(Path(directory) / "evidence-key.pem")
    proc = subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", key_path, "-out", cert_path, "-days", "1",
            "-nodes", "-subj", "/CN=127.0.0.1",
        ],
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0:
        tail = decode(proc.stderr)[-300:]
        raise _SkipScenario(f"openssl could not mint an evidence cert: {tail}")
    return cert_path, key_path


def _wait_for_lines(proc: subprocess.Popen[bytes], needles: list, timeout: float) -> dict:
    """Read the child's stdout until every needle has appeared (select so a
    silent child cannot hang the evidence run past the timeout)."""
    found: dict = {}
    buf = ""
    fd = proc.stdout.fileno()
    deadline = time.monotonic() + timeout
    while len(found) < len(needles):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        chunk = os.read(fd, 4096)
        if not chunk:
            break  # EOF: the child is gone
        buf += chunk.decode("utf-8", errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            for needle in needles:
                if needle in line and needle not in found:
                    found[needle] = line
    missing = [needle for needle in needles if needle not in found]
    if missing:
        raise _SkipScenario(
            "the fake controller did not start "
            f"(never saw {missing[0]!r} on its stdout)"
        )
    return found


def _stop_fake(proc: subprocess.Popen[bytes]) -> None:
    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    except OSError:
        pass
    finally:
        try:
            if proc.stdout:
                proc.stdout.close()
        except OSError:
            pass


@contextmanager
def run_fake(head_tree: Path, spec: dict, cert_path: str, key_path: str):
    """Boot the HEAD tree's fake for one scenario; both trees share it."""
    argv = [
        sys.executable,
        str(head_tree / FAKE_SCRIPT),
        "--cert", cert_path,
        "--key", key_path,
        "--bind", "127.0.0.1",
        "--port", "0",
        "--fixture-set", spec["fixture_set"],
    ]
    if spec["reject_login"]:
        argv.append("--reject-login")
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise _SkipScenario(f"the fake controller could not start: {exc}") from exc
    try:
        lines = _wait_for_lines(
            proc,
            ["listening on https://127.0.0.1:", "fingerprint_sha256:"],
            FAKE_BOOT_TIMEOUT_S,
        )
    except _SkipScenario:
        _stop_fake(proc)
        raise
    try:
        port = int(lines["listening on https://127.0.0.1:"].rsplit(":", 1)[1].strip())
        fingerprint = lines["fingerprint_sha256:"].split("fingerprint_sha256:", 1)[1].strip()
        if not 1 <= port <= 65535 or not fingerprint:
            raise ValueError("unparseable startup line")
    except (ValueError, IndexError) as exc:
        _stop_fake(proc)
        raise _SkipScenario(
            f"the fake controller printed an unparseable startup line: {exc}"
        ) from exc
    try:
        yield FakeInfo(proc, port, fingerprint)
    finally:
        _stop_fake(proc)


@contextmanager
def _self_contained_fake(tree: Path, scenario: dict):
    """A fake from this tree's own script, for direct run_scenario calls."""
    spec = validate_fake_spec(scenario.get("fake"), scenario["id"])
    with tempfile.TemporaryDirectory(prefix="hs-evidence-cert-") as certdir:
        cert_path, key_path = make_cert(certdir)
        with run_fake(tree, spec, cert_path, key_path) as fake:
            yield fake


def write_fake_home(home: str, fake: FakeInfo, spec: dict) -> None:
    """Write config.json plus the secrets file into the run's fresh HOME —
    the same two files a user sets up (install/config.example.json)."""
    cfg_dir = Path(home) / ".config" / "housesitter"
    secrets_dir = cfg_dir / "secrets"
    secrets_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(secrets_dir, 0o700)
    fingerprint = fake.fingerprint
    if spec["bad_fingerprint"]:
        first = "0" if not fingerprint.startswith("0") else "1"
        fingerprint = first + fingerprint[1:]
    config = {
        "housesitter": {"identifiers": "full", "retention": 500},
        "modules": {
            "unifi_wifi": {
                "enabled": True,
                "base_url": f"https://127.0.0.1:{fake.port}",
                "site": "default",
                "flavor": "unifi_os",
                "auth": {
                    "username": "housesitter-readonly",
                    "password_secret": "unifi_password",
                },
                "tls": {"fingerprint_sha256": fingerprint},
                "request_timeout_s": 15,
            }
        },
    }
    (cfg_dir / "config.json").write_text(json.dumps(config, indent=2, sort_keys=True))
    secret_path = secrets_dir / "unifi_password"
    secret_path.write_text(EVIDENCE_PASSWORD)
    os.chmod(secret_path, 0o600)


_SAVED_REF_RE = re.compile(r"\$saved_id\((\d+)\)")
_MORE_COMMAND_REF_RE = re.compile(r"\$more_command\((\d+)\)")
_MORE_PARAMS_REF_RE = re.compile(r"\$more_params\((\d+)\)")


class _BadStepReference(Exception):
    """A step replays a value an earlier step never produced."""


def _envelope_of(step_result: dict):
    """The step's stdout parsed as an envelope, else None."""
    try:
        parsed = json.loads(step_result["stdout"])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _replay_value(envelopes: list, step_index: int, match: re.Match, kind: str) -> str:
    number = int(match.group(1))
    if number >= len(envelopes):
        raise _BadStepReference(
            f"step {step_index} replays step {number}, which has not run yet"
        )
    envelope = envelopes[number]
    if not isinstance(envelope, dict):
        raise _BadStepReference(
            f"step {step_index} replays step {number}, whose output is not JSON"
        )
    if kind == "saved":
        value = envelope.get("saved")
        if not isinstance(value, str) or not value:
            raise _BadStepReference(
                f"step {step_index} replays step {number}'s saved id, "
                "which that step never produced"
            )
        return value
    more = envelope.get("more")
    if not isinstance(more, dict):
        raise _BadStepReference(
            f"step {step_index} replays step {number}'s follow-up page, "
            "which that step has none of (not truncated)"
        )
    if kind == "command":
        command = more.get("command")
        if not isinstance(command, str) or not command:
            raise _BadStepReference(
                f"step {number}'s follow-up page has no command to replay"
            )
        return command
    params = more.get("params")
    if not isinstance(params, dict):
        raise _BadStepReference(
            f"step {number}'s follow-up page has no params to replay"
        )
    return json.dumps(params, sort_keys=True)


def _resolve_step_argv(argv: list, envelopes: list, step_index: int) -> list:
    """Substitute $saved_id(N)/$more_command(N)/$more_params(N)."""
    resolved = []
    for token in argv:
        token = _SAVED_REF_RE.sub(
            lambda match: _replay_value(envelopes, step_index, match, "saved"), token
        )
        token = _MORE_COMMAND_REF_RE.sub(
            lambda match: _replay_value(envelopes, step_index, match, "command"), token
        )
        token = _MORE_PARAMS_REF_RE.sub(
            lambda match: _replay_value(envelopes, step_index, match, "params"), token
        )
        resolved.append(token)
    return resolved


def _run_one_step(tree: Path, argv: list, home: str, env: dict) -> dict:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "housesitter", *argv],
            cwd=home,
            env=env,
            input=b"",
            capture_output=True,
            timeout=RUN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": argv,
            "exit": "timeout",
            "stdout": decode(exc.stdout),
            "stderr": decode(exc.stderr),
        }
    return {
        "argv": argv,
        "exit": proc.returncode,
        "stdout": decode(proc.stdout),
        "stderr": decode(proc.stderr),
    }


def _collect_redactions(step_results: list) -> set:
    """Volatile strings the run produced (saved ids, collection times):
    redacting the exact strings in the echoed step headers keeps runs
    deterministic without touching real module output."""
    values = set()
    for step in step_results:
        envelope = _envelope_of(step)
        if not isinstance(envelope, dict):
            continue
        for key in ("saved", "collected_at"):
            value = envelope.get(key)
            if isinstance(value, str) and len(value) >= 8:
                values.add(value)
        more = envelope.get("more")
        if isinstance(more, dict) and isinstance(more.get("params"), dict):
            value = more["params"].get("id")
            if isinstance(value, str) and len(value) >= 8:
                values.add(value)
    return values


def _render_step(
    step: dict,
    index: int,
    count: int,
    volatile: set,
    redactions: set | frozenset = frozenset(),
    data_counts: bool = False,
) -> str:
    out = step["stdout"]
    try:
        parsed = json.loads(out)
    except ValueError:
        pass
    else:
        blanked = blank_volatile(parsed, volatile)
        if data_counts:
            blanked = project_data_counts(blanked)
        out = json.dumps(blanked, indent=2, sort_keys=True) + "\n"
    # Redaction applies ONLY to the echoed argv header: the replayed saved
    # and follow-up ids must not leak run-specific values into the diff, but
    # the JSON body is already blanked by volatile_keys and any real module
    # datum that happens to equal a volatile string (a client name matching
    # collected_at, say) must survive verbatim, or a real change to that
    # datum would compare as unchanged. Stderr renders verbatim: module logs
    # below WARNING never reach it (only fixed info strings exist), and the
    # determinism test pins two real runs byte-identical, which would fail
    # if stderr carried anything volatile.
    header_argv = " ".join(step["argv"])
    if redactions:
        header_argv = _redact_known_values(header_argv, redactions)
    notice = "(data lists shown as item counts)\n" if data_counts else ""
    text = (
        f"--- step {index + 1}/{count} ---\n"
        f"{notice}"
        f"$ housesitter {header_argv}\n"
        f"$ exit {step['exit']}\n--- stdout ---\n{out}"
    )
    if step["stderr"]:
        text += f"--- stderr ---\n{step['stderr']}"
    if not text.endswith("\n"):
        text += "\n"
    return text


def _run_fake_scenario(
    tree: Path, scenario: dict, fake: FakeInfo, volatile: set | frozenset, side: str | None
) -> dict:
    spec = validate_fake_spec(scenario.get("fake"), scenario["id"])
    steps = scenario_steps(scenario)
    data_counts = validate_render_mode(scenario.get("render"), scenario["id"]) == "data-counts"
    with tempfile.TemporaryDirectory(prefix="hs-evidence-") as home:
        env = {
            "HOME": home,
            "USER": "evidence",
            "LOGNAME": "evidence",
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONPATH": str((tree / "src").resolve()),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        try:
            write_fake_home(home, fake, spec)
        except OSError as exc:
            return _skip_result(side, f"could not write the evidence home: {exc}")
        step_results = []
        envelopes: list = []
        for index, step in enumerate(steps):
            try:
                argv = _resolve_step_argv(step["argv"], envelopes, index)
            except _BadStepReference as exc:
                step_results.append(
                    {
                        "argv": step["argv"],
                        "exit": 1,
                        "stdout": f"(bad step reference: {exc})\n",
                        "stderr": "",
                    }
                )
                envelopes.append(None)
                continue
            result = _run_one_step(tree, argv, home, env)
            step_results.append(result)
            envelopes.append(_envelope_of(result))
    volatile_set = set(volatile)
    redactions = _collect_redactions(step_results)
    combined = "".join(
        _render_step(step, index, len(step_results), volatile_set, redactions, data_counts)
        for index, step in enumerate(step_results)
    )
    exit_code: object = 0
    for step in step_results:
        if step["exit"] != 0:
            exit_code = step["exit"]
            break
    else:
        exit_code = step_results[-1]["exit"]
    return {
        "exit": exit_code,
        "stdout": combined,
        "stderr": "",
        # The SDK measures each call's raw stdout, not this rendering; a
        # stepped scenario reports its largest call, the one closest to the
        # budget.
        "sdk_size": max(len(json.dumps(step["stdout"])) - 2 for step in step_results),
        "stepped": True,
    }


def _redact_known_values(text: str, values: set) -> str:
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, VOLATILE)
    return text


def decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


def blank_volatile(obj: object, keys: set) -> object:
    """Blank volatile values: a bare key matches at any depth (as before);
    a dotted key ("more.params.id") matches that path from the envelope
    root only, so real module output that shares a leaf name stays."""
    names = {key for key in keys if "." not in key}
    paths = [tuple(key.split(".")) for key in keys if "." in key]
    return _blank_volatile(obj, names, paths, ())


def _blank_volatile(obj: object, names: set, paths: list, prefix: tuple) -> object:
    if isinstance(obj, dict):
        return {
            key: (
                VOLATILE
                if key in names or prefix + (key,) in paths
                else _blank_volatile(value, names, paths, prefix + (key,))
            )
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_blank_volatile(value, names, paths, prefix) for value in obj]
    return obj


def render(result: dict | None, volatile: set) -> str:
    """Stable text form of one run, used for diffing and display."""
    if result is None:
        return "(not present)\n"
    if result.get("stepped"):
        # Multi-step runs are already rendered per step (with volatile
        # bodies and redacted headers); emit verbatim.
        text = result["stdout"]
        if result["stderr"]:
            text += f"--- stderr ---\n{result['stderr']}"
        if not text.endswith("\n"):
            text += "\n"
        return text
    out = result["stdout"]
    try:
        parsed = json.loads(out)
    except ValueError:
        pass
    else:
        out = json.dumps(blank_volatile(parsed, volatile), indent=2, sort_keys=True) + "\n"
    text = f"$ exit {result['exit']}\n--- stdout ---\n{out}"
    if result["stderr"]:
        text += f"--- stderr ---\n{result['stderr']}"
    if not text.endswith("\n"):
        text += "\n"
    return text


def code_span(text: str) -> str:
    ticks = "`"
    while ticks in text:
        ticks += "`"
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def _longest_run(text: str, char: str) -> int:
    """Length of the longest run of ``char`` in ``text`` (one linear pass)."""
    best = run = 0
    for ch in text:
        if ch == char:
            run += 1
            if run > best:
                best = run
        else:
            run = 0
    return best


def fence(text: str, lang: str = "") -> str:
    # One linear pass for the fence: one tick longer than the longest
    # backtick run (at least three) is exactly what the old search loop
    # computed, without its quadratic rescanning of ever-longer strings.
    ticks = "`" * max(3, _longest_run(text, "`") + 1)
    # The closing fence must start its own CommonMark line, or it becomes
    # part of the last content line and a later opening fence closes the
    # block instead: PR text would then render as Markdown. CommonMark
    # recognises LF, CRLF and lone CR as line endings, so only a text
    # without any of those endings needs one added.
    if text and not text.endswith(("\n", "\r")):
        text += "\n"
    return f"{ticks}{lang}\n{text}{ticks}\n"


def _ui_cell(text: object, limit: int) -> str:
    """Make manifest text safe for a Markdown table cell.

    Collapses whitespace (no newlines to break rows), swaps `|` for `/` (so a
    hostile value cannot add columns), and caps the length. Callers wrap the
    result in a code span, which neutralises every other Markdown/HTML-active
    character: the text stays readable but nothing in it renders.
    """
    return " ".join(str(text).split()).replace("|", "/")[:limit]


def _ui_img(src: str, width: int, alt: str) -> str:
    """An <img> for an evidence-branch file. Only allow-listed names render."""
    name = src.rsplit("/", 1)[-1]
    if not UI_CAPTURE_RE.fullmatch(name):
        raise ValueError(f"refusing <img> for unexpected file name: {name!r}")
    return (
        f'<img src="{html.escape(src, quote=True)}" '
        f'width="{width:d}" loading="lazy" alt="{html.escape(alt, quote=True)}">'
    )


def browser_display(manifest: dict) -> str | None:
    """E.g. "Google Chrome 131.0.6778" for a recognised browser, else None.

    The manifest is untrusted data (a PR-authored artifact), so the channel
    goes through an allow-list and the version through a strict numeric
    shape; anything else renders the neutral sentence, never the bad value.
    Mirrors ui_evidence.browser_display (pinned by test).
    """
    browser = manifest.get("browser")
    if not isinstance(browser, dict):
        return None
    channel = browser.get("channel")
    version = browser.get("version")
    if not isinstance(channel, str) or not isinstance(version, str):
        return None
    name = UI_BROWSER_NAMES.get(channel)
    if name is None:
        return None
    if len(version) > UI_BROWSER_VERSION_MAX_LEN:
        return None
    if not UI_BROWSER_VERSION_RE.fullmatch(version):
        return None
    return f"{name} {version}"


def load_ui_manifest(ui_dir: Path | None) -> dict | None:
    """Read the UI manifest, or None when there is no UI evidence.

    A corrupt (unparseable) manifest raises: the capture pipeline is broken
    and failing closed beats posting a comment that silently omits the UI.
    """
    if ui_dir is None:
        return None
    path = Path(ui_dir) / "ui_manifest.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("captures"), list):
        raise ValueError("UI manifest must be an object with a captures list")
    return data


# Names a status must not carry: the comment never references them, so the
# push must not carry them either. Added shows only head, removed only base,
# unchanged shows no image at all (its PNGs are deleted by build_manifest).
UI_FORBIDDEN_NAMES = {
    "added": ("base", "diff"),
    "removed": ("head", "diff"),
    "unchanged": ("base", "head", "diff"),
}


def select_ui_captures(manifest: dict) -> tuple:
    """Split the manifest into kept captures and a dropped count.

    Returns ([(capture, referenced files)], dropped): for every kept
    capture, exactly the PNG names the details block references as <img>
    URLs (added -> head; removed -> base; changed -> base, head, and diff
    when set; unchanged -> none). This is the single decision point for
    which images the full Web UI section pictures;
    publish_evidence.copy_pushable_files copies exactly these names (plus
    the manifest), so the push carries exactly what the full section
    pictures. When the comment budget forces the table-only fallback, the
    pushed images are not pictured (docs/REVIEW.md, "Retention").

    Besides the needed names per status, added/removed/unchanged forbid
    the names above; a non-None diff_error is allowed only on a changed
    capture without a diff image; and removed forbids non-empty
    head_problems (a removed page has no head run to report checks for).
    A violating entry is dropped, like every other invalid entry.
    """
    kept, dropped = [], 0
    for cap in manifest.get("captures", []):
        if not isinstance(cap, dict):
            dropped += 1
            continue
        status = cap.get("status")
        names = {key: cap.get(key) for key in ("base", "head", "diff")}
        problems = cap.get("head_problems", [])
        view = cap.get("view")
        if status not in UI_STATUSES or view not in UI_VIEW_WIDTHS:
            dropped += 1
            continue
        if any(
            value is not None
            and (not isinstance(value, str) or not UI_CAPTURE_RE.fullmatch(value))
            for value in names.values()
        ):
            dropped += 1
            continue
        if not isinstance(problems, list) or any(
            not isinstance(item, str) for item in problems
        ):
            dropped += 1
            continue
        diff_error = cap.get("diff_error")
        if diff_error is not None and not isinstance(diff_error, str):
            dropped += 1
            continue
        crop_px = cap.get("crop_px")
        if crop_px is not None and (
            type(crop_px) is not int or crop_px not in UI_CROP_HEIGHTS
        ):
            dropped += 1
            continue
        need = {"added": ("head",), "removed": ("base",), "changed": ("base", "head")}.get(
            status, ()
        )
        if any(not isinstance(names[key], str) for key in need):
            dropped += 1
            continue
        forbidden = UI_FORBIDDEN_NAMES.get(status, ())
        if any(names[key] is not None for key in forbidden):
            dropped += 1
            continue
        # A diff error only exists for a changed pair whose diff failed, so
        # it never comes with a diff image or with any other status.
        if diff_error is not None and (status != "changed" or names["diff"] is not None):
            dropped += 1
            continue
        if status == "removed" and len(problems) != 0:
            dropped += 1
            continue
        if status == "added":
            files = [names["head"]]
        elif status == "removed":
            files = [names["base"]]
        elif status == "changed":
            files = [names["base"], names["head"]]
            if names["diff"] is not None:
                files.append(names["diff"])
        else:
            files = []
        kept.append((cap, files))
    return kept, dropped


def render_ui_section(manifest: dict, repo: str, pr: int, head_sha: str) -> tuple:
    """Build (full section, table-only section, check-failure count).

    Every manifest value is re-validated: capture names against the capture
    regex, status against the enum, the view against the known views, and
    problems escaped (via code spans) and length-capped. Captures that fail
    validation are dropped and counted, never rendered. Image URLs are built
    from the validated repo/PR/head SHA plus the validated file name, so they
    always match the pinned evidence-branch pattern exactly. The browser key
    is untrusted too: only an allow-listed channel with a strict numeric
    version is named, otherwise a neutral sentence renders instead.
    """
    if not UI_REPO_RE.fullmatch(repo or ""):
        raise ValueError(f"bad repo: {repo!r}")
    if not isinstance(pr, int) or pr <= 0:
        raise ValueError(f"bad PR number: {pr!r}")
    if not UI_SHA_RE.fullmatch(head_sha or ""):
        raise ValueError(f"bad head SHA: {head_sha!r}")
    url = (
        f"https://raw.githubusercontent.com/{repo}/{SHA_PLACEHOLDER}"
        f"/pr/{pr}/{head_sha}"
    )
    kept_with_files, dropped = select_ui_captures(manifest)
    kept = [cap for cap, _ in kept_with_files]
    lines = [
        "## Web UI",
        "",
        UI_INTRO,
        "",
    ]
    if kept:
        display = browser_display(manifest)
        if display is None:
            lines += [UI_BROWSER_UNKNOWN + " " + UI_IMAGES_NOTE, ""]
        else:
            lines += ["Captured headless with " + display + ". " + UI_IMAGES_NOTE, ""]
    if not manifest["captures"]:
        lines += [
            "Neither the base nor the head captured anything: no `web/` on "
            "either side, so there is nothing to show.",
            "",
        ]
    failures = 0
    if kept:
        lines += [
            "| Capture | Status | Checks |",
            "|---------|--------|--------|",
        ]
        for cap in kept:
            # Validation treats a missing head_problems as none, so must this.
            checks = [
                _ui_cell(item, MAX_UI_TEXT_LEN) for item in cap.get("head_problems", [])
            ]
            if cap.get("diff_error"):
                checks.append(_ui_cell(cap["diff_error"], MAX_UI_TEXT_LEN))
            if checks:
                failures += 1
            label = _ui_cell(
                f"{cap.get('fixture', '')} / {cap.get('view', '')}", MAX_UI_LABEL_LEN
            )
            checks_cell = code_span("; ".join(checks) if checks else "pass")
            lines.append(f"| {code_span(label)} | {cap['status']} | {checks_cell} |")
    lines += [""]
    if dropped:
        lines += [
            f"**Omitted {dropped} UI capture(s) with invalid manifest entries.**",
            "",
        ]
    table_section = "\n".join(lines)

    details = []
    by_fixture: dict = {}
    for cap in kept:
        by_fixture.setdefault(_ui_cell(cap.get("fixture", ""), MAX_UI_LABEL_LEN), []).append(cap)
    for fixture in sorted(by_fixture):
        shown = [c for c in by_fixture[fixture] if c["status"] in ("added", "changed", "removed")]
        if not shown:
            continue
        # Fixed summary text: the fixture label is PR-derived data, and
        # Markdown does not render inside <summary>, so even a code span
        # would show its backticks literally while hostile prose ("Approved
        # by maintainer") would read as the bot's own words. The label
        # still appears below in code spans, where it cannot render.
        block = ["<details><summary>Screenshots</summary>", ""]
        for cap in shown:
            label = _ui_cell(f"{cap.get('fixture', '')} / {cap.get('view', '')}", MAX_UI_LABEL_LEN)
            width = UI_VIEW_WIDTHS[cap["view"]]
            block += [f"#### {code_span(label)}", ""]
            if cap["status"] == "added":
                block += [
                    "New page: no before image (the base doesn't have this page).",
                    "",
                    _ui_img(f"{url}/{cap['head']}", width, f"after: {label}"),
                    "",
                ]
            elif cap["status"] == "removed":
                block += [
                    "Removed page: no after image (the head no longer has this page).",
                    "",
                    _ui_img(f"{url}/{cap['base']}", width, f"before: {label}"),
                    "",
                ]
            else:
                block += [
                    _ui_img(f"{url}/{cap['base']}", width, f"before: {label}"),
                    _ui_img(f"{url}/{cap['head']}", width, f"after: {label}"),
                ]
                if cap.get("diff") is not None:
                    block.append(_ui_img(f"{url}/{cap['diff']}", width, f"diff: {label}"))
                else:
                    reason = _ui_cell(
                        cap.get("diff_error") or "unknown reason", MAX_UI_TEXT_LEN
                    )
                    # A code span, like the table: no markdown or HTML renders.
                    block.append("Diff unavailable: " + code_span(reason) + ".")
            if cap.get("crop_px") is not None:
                # select_ui_captures above kept only valid heights, so this
                # int is safe to format: fixed words plus the recorded
                # height. A cropped shot shows only the top of the page.
                block.append(
                    f"Cropped at {cap['crop_px']:,} px to fit the size limit; "
                    "changes below this line are not shown."
                )
            block += [""]
        block += ["</details>", ""]
        details.append((fixture, "\n".join(block)))

    # No budget trimming here: the caller picks whole tiers (full section,
    # table alone, or a one-line pointer) against the shared comment budget,
    # so a cut never leaves a tag or fence open.
    full = table_section
    for _, block in details:
        full += ("\n" if not full.endswith("\n\n") else "") + block
    return full, table_section, failures


# Fixed suffixes for the Web UI shrink tiers (review round 4): the table
# fallback says the screenshot details were omitted, the pointer says the
# whole section was. The publisher tries them after the full section, in
# this order, measured on the final form (see final_len).
UI_DETAILS_OMITTED_SUFFIX = (
    "\n**Web UI details omitted to fit a GitHub comment:** "
    "run `scripts/ui_evidence.py` locally to see them.\n"
)
UI_SECTION_OMITTED_SUFFIX = (
    "\n**Web UI evidence omitted to fit a GitHub comment:** "
    "run `scripts/ui_evidence.py` locally to see it.\n"
)


def ui_tiers(
    manifest: dict, repo: str, pr: int, head_sha: str
) -> tuple:
    """The Web UI suffixes, richest first: full section, table plus the
    details-omitted notice, and the section-omitted pointer.

    Returns (suffixes, check-failure count). The publisher walks the
    suffixes in order against one final-form budget, so screenshots survive
    everything except a comment that cannot fit them after the Tests line
    has already degraded away (review round 4, finding 2).
    """
    full, table, failures = render_ui_section(manifest, repo, pr, head_sha)
    return (
        ["\n" + full, "\n" + table + UI_DETAILS_OMITTED_SUFFIX, UI_SECTION_OMITTED_SUFFIX],
        failures,
    )


def append_ui_section(
    body: str,
    ui_dir: Path | None,
    *,
    repo: str | None = None,
    pr: int | None = None,
    head_sha: str | None = None,
) -> str:
    """Append the Web UI section, built from ui_manifest.json, if present.

    Pre-rendered markdown files in the artifact are ignored: the comment
    carries only known boilerplate plus a table and images built from the
    re-validated manifest. Without repo/PR/head-SHA context the pinned image
    URLs cannot be built, so the body is returned unchanged.
    """
    manifest = load_ui_manifest(ui_dir)
    if manifest is None or repo is None or pr is None or head_sha is None:
        return body
    suffixes, _ = ui_tiers(manifest, repo, pr, head_sha)
    for suffix in suffixes[:2]:
        if final_len(body + suffix) <= MAX_COMMENT_BYTES:
            return body + suffix
    return body + suffixes[2]


def collect_runs(base_tree: Path, head_tree: Path) -> tuple:
    """Run every head scenario against both trees. Returns
    (scenarios, volatile, base_scenarios, base_ok, runs) where runs is a list
    of (scenario, before, after); before is None when the base has no
    package. Both comment builders below share this, so the text comment
    and the structured results describe the same runs.

    Fake scenarios share one fake per scenario (booted from the HEAD tree,
    serving the HEAD tree's fixtures) for the base and head runs, and one
    throwaway cert for the whole collection. A fake that cannot be provided
    becomes a visible skip on both sides, never a dropped scenario."""
    scenarios, volatile_keys = load_scenarios(head_tree)
    volatile = set(volatile_keys)
    base_scenarios = load_scenarios(base_tree)[0]
    base_ok = has_package(base_tree)
    runs = []
    with tempfile.TemporaryDirectory(prefix="hs-evidence-cert-") as certdir:
        cert_paths: tuple | None = None
        for scenario in scenarios:
            if scenario.get("fake") is None:
                before = run_scenario(base_tree, scenario) if base_ok else None
                after = run_scenario(head_tree, scenario)
                runs.append((scenario, before, after))
                continue
            spec = validate_fake_spec(scenario.get("fake"), scenario["id"])
            try:
                if cert_paths is None:
                    cert_paths = make_cert(certdir)
                with run_fake(head_tree, spec, *cert_paths) as fake:
                    before = (
                        run_scenario(base_tree, scenario, fake, volatile, side="base")
                        if base_ok
                        else None
                    )
                    after = run_scenario(head_tree, scenario, fake, volatile, side="head")
            except _SkipScenario as exc:
                before = _skip_result("base", str(exc)) if base_ok else None
                after = _skip_result("head", str(exc))
            runs.append((scenario, before, after))
    return scenarios, volatile, base_scenarios, base_ok, runs


def build_results(
    base_tree: Path, head_tree: Path, head_sha: str | None = None
) -> dict:
    """Run every scenario and return the structured results (Decision 13,
    option A). The privileged publisher validates this against a strict
    schema and renders the comment itself; the PR's markdown is never
    posted. ``before``/``after`` hold the normalised texts ``render``
    produces (the same strings the comment diffs); ``before`` is None when
    the base has no package to run against."""
    scenarios, volatile, base_scenarios, base_ok, runs = collect_runs(base_tree, head_tree)
    return _results_from_runs(scenarios, volatile, base_scenarios, base_ok, runs, head_sha)


def _results_from_runs(
    scenarios: list,
    volatile: set,
    base_scenarios: list,
    base_ok: bool,
    runs: list,
    head_sha: str | None,
) -> dict:
    base_ids = {s["id"] for s in base_scenarios}
    head_ids = {s["id"] for s in scenarios}
    entries = []
    for scenario, before, after in runs:
        before_text = render(before, volatile) if before is not None else None
        after_text = render(after, volatile)
        new = before is None or scenario["id"] not in base_ids
        command = ["housesitter", *scenario["argv"]]
        if scenario.get("fake") is not None and scenario.get("steps"):
            # Show every step in order, as authored (replay references
            # unresolved), so the table names what actually ran.
            command = []
            for index, step in enumerate(scenario["steps"]):
                if index:
                    command.append("&&")
                command.extend(["housesitter", *step["argv"]])
        entries.append(
            {
                "id": scenario["id"],
                "title": scenario.get("title", ""),
                "command": command,
                "exit_before": before["exit"] if before is not None else None,
                "exit_after": after["exit"],
                "expected_exit": scenario.get("expect_exit", 0),
                "size_before": before["sdk_size"] if before is not None else None,
                "size_after": after["sdk_size"],
                "changed": new or before_text != after_text,
                "new_in_pr": scenario["id"] not in base_ids,
                "before": before_text,
                "after": after_text,
            }
        )
    metadata: dict = {"generator": "scripts/pr_evidence.py", "base_has_package": base_ok}
    if head_sha is not None:
        metadata["head_sha"] = head_sha
    return {
        "version": 1,
        "metadata": metadata,
        "scenarios": entries,
        "removed": sorted(base_ids - head_ids),
    }


def neutralise(text: object) -> str:
    """Break up @mentions and <tags> in PR-supplied text.

    Fenced code blocks already stop GitHub rendering mentions, links, and
    HTML, but this breaks them up anyway (an invisible separator after each
    ``@`` and ``<``), so the raw bytes cannot ping or tag even if a fence
    is ever dropped. Idempotent and invisible in the rendered comment.
    """
    return str(text).replace("@", "@\u200b").replace("<", "<\u200b")


def _inline(text: object) -> str:
    """Single-line form of PR-supplied text for headings and table cells."""
    return neutralise(" ".join(str(text).split()))


def render_text_from_results(results: dict, head_sha: str) -> str:
    """Rebuild the text section from validated structured results (main's
    code: the privileged publisher calls this, never PR code).

    The caller must have validated ``results`` (unknown keys rejected,
    types and caps checked, ids known or marked new). Every PR-supplied
    string appears only inside fenced code blocks (whose fence outgrows any
    backtick run in the content) or single-line code spans, neutralised, so
    a forged "Approved by maintainer" can never render as a heading,
    prose, link, or image. The fixed header says the outputs came from the
    PR's own CI run: they are author-attested, not trusted fact.
    """
    top, sections = text_parts_from_results(results, head_sha)
    return fit_text_sections(top, sections)


def text_parts_from_results(results: dict, head_sha: str) -> tuple:
    """Build the text section's (top, sections) before any budget fitting.

    The publisher's shrink order fits these parts together with the Tests
    line and the Web UI section against one final-form budget (see
    publish_evidence.render_comment); this wrapper fits them alone for
    callers that only need the text.
    """
    scenarios = results["scenarios"]
    removed = results.get("removed", [])
    base_ok = results.get("metadata", {}).get("base_has_package", True)

    def size(value: object) -> str:
        return "—" if value is None else f"{value:,} B"

    def exit_(value: object) -> str:
        return "—" if value is None else str(value)

    rows, sections, unexpected = [], [], []
    for entry in scenarios:
        before_text = entry["before"] if entry["before"] is not None else "(not present)\n"
        after_text = entry["after"]
        before_text, after_text = neutralise(before_text), neutralise(after_text)
        if entry.get("new_in_pr"):
            status = "new"
        elif not entry["changed"]:
            status = "unchanged"
        else:
            status = "changed"
        if entry["exit_after"] != entry["expected_exit"]:
            unexpected.append(
                f"{code_span(_inline(entry['id']))} exited "
                f"{entry['exit_after']}, expected {entry['expected_exit']}"
            )
        rows.append(
            f"| {code_span(_inline(entry['id']))} | {status} "
            f"| {exit_(entry['exit_before'])} → {exit_(entry['exit_after'])} "
            f"| {size(entry['size_before'])} → {size(entry['size_after'])} |"
        )
        if status == "unchanged":
            continue
        diff = "".join(
            difflib.unified_diff(
                before_text.splitlines(keepends=True),
                after_text.splitlines(keepends=True),
                fromfile="before",
                tofile="after",
            )
        )
        heading = f"### {code_span(_inline(entry['id']))}"
        if entry.get("title", ""):
            heading += f": {code_span(_inline(entry['title']))}"
        command = " ".join(_inline(arg) for arg in entry["command"])
        sections.append(
            heading + "\n\n"
            + code_span(command)
            + "\n\n"
            + fence(diff, "diff")
            + "\n<details><summary>Full before</summary>\n\n"
            + fence(before_text)
            + "\n</details>\n<details><summary>Full after</summary>\n\n"
            + fence(after_text)
            + "\n</details>\n"
        )
    for ident in removed:
        rows.append(f"| {code_span(_inline(ident))} | removed | — | — |")

    head = [
        MARKER,
        "## Before / after evidence",
        "",
        "Fixture house only. Rebuilt by main's publisher from the PR's structured "
        "results (`results.json`); see docs/REVIEW.md, \"Evidence\". The outputs below "
        f"were produced by the PR's own CI run at `{head_sha}` from the PR's own code: "
        "they are author-attested, like the screenshots, not trusted fact. "
        "Size is the Muse SDK's measure of stdout (limit 96 KiB; budget 88 KiB).",
        "",
    ]
    if not base_ok:
        head += ["The base has no `housesitter` package yet, so every scenario is new.", ""]
    if not scenarios:
        head += ["**The head has no scenarios.** Every PR must keep at least one.", ""]
    for line in unexpected:
        head += [f"**Unexpected exit:** {line}.", ""]
    head += [
        "| Scenario | Status | Exit before → after | Size before → after |",
        "|----------|--------|---------------------|---------------------|",
        *rows,
        "",
    ]
    if not sections and not any("| removed |" in r for r in rows):
        head += ["**No output change.**", ""]
    top = "\n".join(head) + "\n"
    return top, sections


def build_comment(
    base_tree: Path,
    head_tree: Path,
    ui_dir: Path | None = None,
    *,
    repo: str | None = None,
    pr: int | None = None,
    head_sha: str | None = None,
) -> str:
    scenarios, volatile, base_scenarios, base_ok, runs = collect_runs(base_tree, head_tree)
    return _comment_from_runs(
        scenarios, volatile, base_scenarios, base_ok, runs, ui_dir,
        repo=repo, pr=pr, head_sha=head_sha,
    )


def _comment_from_runs(
    scenarios: list,
    volatile: set,
    base_scenarios: list,
    base_ok: bool,
    runs: list,
    ui_dir: Path | None,
    *,
    repo: str | None = None,
    pr: int | None = None,
    head_sha: str | None = None,
) -> str:
    base_ids = {s["id"] for s in base_scenarios}
    head_ids = {s["id"] for s in scenarios}
    ui_manifest = load_ui_manifest(ui_dir)
    ui_failures = 0
    if ui_manifest is not None and repo is not None:
        _, _, ui_failures = render_ui_section(ui_manifest, repo, pr, head_sha)

    def size(r: dict | None) -> str:
        return "—" if r is None or r["sdk_size"] is None else f"{r['sdk_size']:,} B"

    def exit_(r: dict | None) -> str:
        return "—" if r is None else str(r["exit"])

    rows, sections, unexpected = [], [], []
    for scenario, before, after in runs:
        before_text, after_text = render(before, volatile), render(after, volatile)
        if before is None or scenario["id"] not in base_ids:
            status = "new"
        elif before_text == after_text:
            status = "unchanged"
        else:
            status = "changed"
        expected = scenario.get("expect_exit", 0)
        if after["exit"] != expected:
            unexpected.append(
                f"{code_span(scenario['id'])} exited {after['exit']}, expected {expected}"
            )
        rows.append(
            f"| {code_span(scenario['id'])} | {status} | {exit_(before)} → {exit_(after)} "
            f"| {size(before)} → {size(after)} |"
        )
        if status == "unchanged":
            continue
        diff = "".join(
            difflib.unified_diff(
                before_text.splitlines(keepends=True),
                after_text.splitlines(keepends=True),
                fromfile="before",
                tofile="after",
            )
        )
        # Scenario ids and titles come from the head tree's scenarios file, so
        # they are PR-authored data: both go through code_span (which grows
        # the backticks when the value contains any) so neither can inject
        # Markdown or HTML into the heading.
        heading = f"### {code_span(scenario['id'])}"
        if scenario.get("title", ""):
            heading += f": {code_span(scenario['title'])}"
        sections.append(
            heading + "\n\n"
            + code_span("housesitter " + " ".join(scenario["argv"]))
            + "\n\n"
            + fence(diff, "diff")
            + "\n<details><summary>Full before</summary>\n\n"
            + fence(before_text)
            + "\n</details>\n<details><summary>Full after</summary>\n\n"
            + fence(after_text)
            + "\n</details>\n"
        )
    for scenario in base_scenarios:
        if scenario["id"] not in head_ids:
            rows.append(f"| {code_span(scenario['id'])} | removed | — | — |")

    head = [
        MARKER,
        "## Before / after evidence",
        "",
        "Fixture house only. Generated by `scripts/pr_evidence.py` from "
        "`tests/evidence/scenarios.json`; see docs/REVIEW.md, \"Evidence\". "
        "Size is the Muse SDK's measure of stdout (limit 96 KiB; budget 88 KiB).",
        "",
    ]
    if ui_failures:
        head += [
            f"**Web UI checks failed:** {ui_failures} capture(s) report "
            "problems; see the Web UI section below.",
            "",
        ]
    if not base_ok:
        head += ["The base has no `housesitter` package yet, so every scenario is new.", ""]
    if not scenarios:
        head += ["**The head has no scenarios.** Every PR must keep at least one.", ""]
    for line in unexpected:
        head += [f"**Unexpected exit:** {line}.", ""]
    head += [
        "| Scenario | Status | Exit before → after | Size before → after |",
        "|----------|--------|---------------------|---------------------|",
        *rows,
        "",
    ]
    if not sections and not any("| removed |" in r for r in rows):
        head += ["**No output change.**", ""]
    top = "\n".join(head) + "\n"

    # Keep whole sections only, so a cut never leaves a code fence open.
    # The omission notice is part of the measured final form (see final_len).
    body = fit_text_sections(top, sections)
    return append_ui_section(body, ui_dir, repo=repo, pr=pr, head_sha=head_sha)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-tree", type=Path, required=True)
    parser.add_argument("--head-tree", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--ui-artifacts",
        type=Path,
        default=None,
        help="directory with ui_manifest.json from scripts/ui_evidence.py",
    )
    parser.add_argument(
        "--repo", default=None, help="owner/name of the GitHub repo (with --ui-artifacts)"
    )
    parser.add_argument(
        "--pr", type=int, default=None, help="pull request number (with --ui-artifacts)"
    )
    parser.add_argument(
        "--head-sha", default=None, help="head commit SHA, 40 hex (with --ui-artifacts)"
    )
    parser.add_argument(
        "--results-out",
        type=Path,
        default=None,
        help="write the structured scenario results here as JSON "
        "(Decision 13, option A: the publisher renders from this, never from --out)",
    )
    args = parser.parse_args(argv)
    if args.ui_artifacts is not None and not (args.repo and args.pr and args.head_sha):
        parser.error("--repo, --pr, and --head-sha are required with --ui-artifacts")
    try:
        refuse_if_real_setup()
    except Refused as exc:
        print(f"pr_evidence: refused: {exc}", file=sys.stderr)
        return 2
    try:
        return _run_with_trees(args)
    except FakeError as exc:
        print(f"pr_evidence: cannot run fake-controller evidence: {exc}", file=sys.stderr)
        return 2


def _run_with_trees(args: argparse.Namespace) -> int:
    if args.results_out is not None:
        # One shared collection run feeds both outputs, so the text comment
        # (for the old publisher, while this rolls out) and the structured
        # results (for the new one) describe the same runs.
        scenarios, volatile, base_scenarios, base_ok, runs = collect_runs(
            args.base_tree, args.head_tree
        )
        args.out.write_text(
            _comment_from_runs(
                scenarios,
                volatile,
                base_scenarios,
                base_ok,
                runs,
                args.ui_artifacts,
                repo=args.repo,
                pr=args.pr,
                head_sha=args.head_sha,
            ),
            encoding="utf-8",
        )
        args.results_out.write_text(
            json.dumps(
                _results_from_runs(
                    scenarios, volatile, base_scenarios, base_ok, runs, args.head_sha
                ),
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
        return 0
    args.out.write_text(
        build_comment(
            args.base_tree,
            args.head_tree,
            args.ui_artifacts,
            repo=args.repo,
            pr=args.pr,
            head_sha=args.head_sha,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
