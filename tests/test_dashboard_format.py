"""Display rounding on the dashboard pages (issue #41, docs/DASHBOARD.md §3).

The pages round numbers for display only, in web/format.js. These tests run
that file under Node and pin what a person sees for each case: long
fractions, reference-range boundaries, unknown values, zero, 100, negative
dBm, and very large counts. /api/dashboard itself is never rounded; that is
checked at the end against the fixtures.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FORMAT_JS = ROOT / "web" / "format.js"
FIXTURES = ROOT / "tests" / "fixtures" / "dashboard"

# Values JSON can't carry are passed by name and rebuilt in Node.
SPECIAL = {"NaN": "NaN", "Infinity": "Infinity", "-Infinity": "-Infinity",
           "undefined": "undefined", "-0": "-0"}

# (function, input, expected text)
CASES = [
    # The issue's own example: a retry percentage from the real snapshot path.
    ("retryText", 14.285714285714285, "14.3"),
    ("pct1", 14.285714285714285, "14.3"),
    ("pct1", 2, "2.0"),
    ("pct1", 0, "0.0"),
    ("pct1", 100, "100.0"),
    ("pct1", -0.04, "0.0"),  # never "-0.0"
    # Retries near a boundary: number and label must agree (<10 normal,
    # 10-20 elevated, >20 high). The label uses the unrounded value.
    ("retryText", 9.96, "9.96"),
    ("retryText", 9.9996, "9.99"),
    ("retryText", 10, "10.0"),
    ("retryText", 20, "20.0"),
    ("retryText", 20.04, "20.04"),
    ("retryText", 20.0001, "20.01"),
    ("retryText", 0, "0.0"),
    ("retryText", 100, "100.0"),
    # Whole-number percentages stay whole; a fraction gets one decimal.
    ("pct", 96, "96"),
    ("pct", 0, "0"),
    ("pct", 100, "100"),
    ("pct", 33.333333333333336, "33.3"),
    # Utilization (<40 light, 40-70 busy, >70 congested).
    ("utilizationText", 41, "41"),
    ("utilizationText", 39.96, "39.96"),
    ("utilizationText", 70.04, "70.04"),
    ("utilizationText", 40, "40"),
    ("utilizationText", 70, "70"),
    ("utilizationText", 12.345, "12.3"),
    # Signal (>=-60 strong, >=-70 good, >=-75 weak, else very weak).
    ("signalText", -54, "-54"),
    ("signalText", -60, "-60"),
    ("signalText", -60.4, "-60.4"),
    ("signalText", -70.4, "-70.4"),
    ("signalText", -75.4, "-75.4"),
    ("signalText", -59.6, "-60"),  # strong either way, plain rounding
    ("signalText", -82.7, "-83"),
    ("dbm", -95, "-95"),
    ("dbm", 23, "23"),
    ("dbm", -0.3, "0"),  # never "-0"
    # Rates: whole from 10 Mbps, one decimal below (5.5 and 6.5 are real rates).
    ("mbps", 433.3, "433"),
    ("mbps", 72.2, "72"),
    ("mbps", 2161, "2161"),
    ("mbps", 6.5, "6.5"),
    ("mbps", 5.5, "5.5"),
    ("mbps", 1, "1.0"),
    ("mbps", 9.96, "10"),
    ("mbps", 0, "0.0"),
    # Counts: no separators, whatever the locale.
    ("whole", 600, "600"),
    ("whole", 0, "0"),
    ("whole", 123456789, "123456789"),
    ("whole", 9007199254740991, "9007199254740991"),
]
UNKNOWN_INPUTS = [None, "undefined", "NaN", "Infinity", "-Infinity", "12", True, {}, []]
UNKNOWN_FUNCS = ["fixed", "pct1", "pct", "dbm", "mbps", "whole", "signalText",
                 "utilizationText", "retryText", "signalLabel", "utilizationLabel",
                 "retryLabel"]
LABELS = [
    ("retryLabel", 9.96, "normal"),
    ("retryLabel", 10, "elevated"),
    ("retryLabel", 20, "elevated"),
    ("retryLabel", 20.04, "high"),
    ("utilizationLabel", 39.96, "light"),
    ("utilizationLabel", 70, "busy"),
    ("utilizationLabel", 70.04, "congested"),
    ("signalLabel", -60, "strong"),
    ("signalLabel", -60.4, "good"),
    ("signalLabel", -70.4, "weak"),
    ("signalLabel", -75.4, "very weak"),
]

NODE_SCRIPT = r"""
const F = require(process.argv[1]);
const special = {NaN: NaN, Infinity: Infinity, "-Infinity": -Infinity,
                 undefined: undefined, "-0": -0};
const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
const out = cases.map(([fn, arg, extra]) => {
  const v = typeof arg === "string" && arg in special ? special[arg] : arg;
  return typeof F[fn] === "function" ? F[fn](v, extra) : "missing function " + fn;
});
process.stdout.write(JSON.stringify(out));
"""


def run_node(cases, locale="C"):
    node = shutil.which("node")
    # CI runners have Node. A missing node must fail, not silently skip.
    assert node, "node is required to test web/format.js"
    env = {"PATH": "/usr/bin:/bin", "LANG": locale, "LC_ALL": locale}
    proc = subprocess.run(
        [node, "-e", NODE_SCRIPT, str(FORMAT_JS)],
        input=json.dumps(cases), capture_output=True, text=True, timeout=30, env=env,
        check=True,
    )
    return json.loads(proc.stdout)


@pytest.mark.parametrize("fn,value,expected", CASES)
def test_displayed_text(fn, value, expected):
    assert run_node([[fn, value]]) == [expected]


@pytest.mark.parametrize("fn,value,expected", LABELS)
def test_labels_use_the_unrounded_value(fn, value, expected):
    assert run_node([[fn, value]]) == [expected]


@pytest.mark.parametrize("fn", UNKNOWN_FUNCS)
def test_unknown_values_show_unknown(fn):
    extra = 1 if fn == "fixed" else None
    assert run_node([[fn, v, extra] for v in UNKNOWN_INPUTS]) == ["Unknown"] * len(UNKNOWN_INPUTS)


def test_banded_text_and_label_always_agree():
    """Sweep across every boundary: the shown number, read back, gets the
    same label as the measured value."""
    sweeps = {
        "retry": [x / 1000 for x in range(9900, 10100)] + [x / 1000 for x in range(19900, 20100)],
        "utilization": [x / 1000 for x in range(39900, 40100)]
        + [x / 1000 for x in range(69900, 70100)],
        "signal": [-x / 100 for x in range(5950, 6050)] + [-x / 100 for x in range(6950, 7050)]
        + [-x / 100 for x in range(7450, 7550)],
    }
    for kind, values in sweeps.items():
        cases = [[kind + "Text", v] for v in values] + [[kind + "Label", v] for v in values]
        out = run_node(cases)
        texts, labels = out[: len(values)], out[len(values):]
        readback = run_node([[kind + "Label", float(t)] for t in texts])
        for v, t, want, got in zip(values, texts, labels, readback):
            assert got == want, (kind, v, t, want, got)
            assert abs(float(t) - v) <= 0.051 if kind != "signal" else abs(float(t) - v) <= 0.51


@pytest.mark.parametrize("locale", ["C", "de_DE.UTF-8", "fr_FR.UTF-8", "hi_IN.UTF-8"])
def test_formatting_ignores_locale(locale):
    cases = [["whole", 1234567], ["pct1", 1234.56], ["mbps", 1200.9], ["mbps", 6.5]]
    assert run_node(cases, locale) == ["1234567", "1234.6", "1201", "6.5"]


def test_format_js_is_text_only():
    """format.js builds strings only: no DOM, HTML sinks, or locale APIs."""
    src = FORMAT_JS.read_text(encoding="utf-8")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                   "DOMParser", "toLocaleString", "Intl.", "eval(", "Function("):
        assert banned not in src, banned


def test_pages_load_format_js_from_a_file():
    for page in ("index.html", "eink.html"):
        html = (ROOT / "web" / page).read_text(encoding="utf-8")
        assert '<script defer src="format.js"></script>' in html, page
        assert html.index('src="format.js"') < html.index('src="dashboard.js"'), page
        assert "<script>" not in html, page


# Each displayed field and the formatter the approved precision table gives it.
WIRING = dict([
    ("signal_dbm", "F.signalText"),
    ("noise_dbm", "F.dbm"),
    ("tx_power_dbm", "F.dbm"),
    ("tx_rate_mbps", "F.mbps"),
    ("rx_rate_mbps", "F.mbps"),
    ("tx_retry_pct_cumulative", "F.retryText"),
    ("tx_retry_pct_recent", "F.pct1"),
    ("channel_utilization_pct", "F.utilizationText"),
    ("satisfaction_pct", "F.pct"),
    ("self_rx_pct", "F.pct"),
    ("self_tx_pct", "F.pct"),
    ("uplink_speed_mbps", None),  # whole Mbps: num()'s default
    ("channel", "raw"),
    ("width_mhz", "raw"),
    ("uptime_s", "raw"),
    ("connected_s", "raw"),
    ("idle_s", "raw"),
])


@pytest.mark.parametrize("field", sorted(WIRING))
def test_every_display_of_a_field_uses_its_formatter(field):
    """Pin the wiring in dashboard.js: every num(x.<field>, ...) call passes
    the formatter the precision table gives that field."""
    src = (ROOT / "web" / "dashboard.js").read_text(encoding="utf-8")
    calls = re.findall(r"num\(\s*[\w?.]+\." + field + r"\b([^()]*)\)", src)
    assert calls, f"{field} is never displayed through num()"
    for args in calls:
        fmt = [a.strip() for a in args.split(",")][2:]
        assert (fmt[0] if fmt else None) == WIRING[field], (field, args)


def test_fixtures_carry_unrounded_values():
    """The API stays unrounded (AGENTS rule 2): the fixtures carry the long
    fraction and the boundary values the screenshots exercise."""
    typical = (FIXTURES / "typical.json").read_text(encoding="utf-8")
    assert '"tx_retry_pct_cumulative": 14.285714285714285' in typical
    edge = json.loads((FIXTURES / "edge.json").read_text(encoding="utf-8"))
    latest = edge["unifi_wifi"]["latest"]
    radios = [r for a in latest["aps"] for r in a["radios"]]
    assert {9.96, 20.04} <= {r["tx_retry_pct_cumulative"] for r in radios}
    assert {39.96, 70.04} <= {r["channel_utilization_pct"] for r in radios}
    assert -60.4 in {c["signal_dbm"] for c in latest["clients"]}
