"""Tests for scripts/test_results.py. Invented JUnit fixtures only, no real data."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "test_results", ROOT / "scripts" / "test_results.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_results"] = module
    spec.loader.exec_module(module)
    return module


test_results = _load()


def _suite(
    name="python-3.9.25",
    tests=3,
    failures=0,
    errors=0,
    skipped=0,
    cases=(
        ("tests/test_alpha.py", "test_one"),
        ("tests/test_alpha.py", "test_two"),
        ("tests/test_beta.py", "test_three"),
    ),
    failing=(),
    extra="",
):
    body = []
    for classname, testname in cases:
        if (classname, testname) in failing:
            body.append(
                f'<testcase classname="{classname}" name="{testname}">'
                "<failure message=\"boom\">trace</failure></testcase>"
            )
        else:
            body.append(
                f'<testcase classname="{classname}" name="{testname}"/>'
            )
    return (
        f'<testsuite name="{name}" tests="{tests}" failures="{failures}" '
        f'errors="{errors}" skipped="{skipped}">{"".join(body)}{extra}</testsuite>'
    ).encode()


def _wrap(inner: bytes, root="testsuite") -> bytes:
    if root == "testsuite":
        return b'<?xml version="1.0" encoding="utf-8"?>' + inner
    return b'<?xml version="1.0" encoding="utf-8"?><testsuites>' + inner + b"</testsuites>"


# Parsing: happy paths --------------------------------------------------------


def test_parse_all_passing():
    parsed = test_results.parse_junit(_wrap(_suite()))
    assert parsed["python"] == "3.9.25"
    assert parsed["tests"] == 3
    assert parsed["failures"] == 0
    assert parsed["errors"] == 0
    assert parsed["skipped"] == 0
    assert parsed["passed"] == 3
    assert parsed["failed_names"] == []
    assert parsed["hidden"] == 0


def test_parse_failures_and_errors():
    data = _wrap(
        _suite(
            name="python-3.14.1",
            tests=4,
            failures=1,
            errors=1,
            skipped=1,
            cases=(
                ("tests/test_alpha.py", "test_one"),
                ("tests/test_alpha.py", "test_two"),
                ("tests/test_beta.py", "test_three"),
                ("tests/test_beta.py", "test_four"),
            ),
            failing=(
                ("tests/test_alpha.py", "test_two"),
                ("tests/test_beta.py", "test_four"),
            ),
        )
    )
    parsed = test_results.parse_junit(data)
    assert parsed["python"] == "3.14.1"
    assert parsed["passed"] == 1
    assert sorted(parsed["failed_names"]) == [
        "tests/test_alpha.py::test_two",
        "tests/test_beta.py::test_four",
    ]
    assert parsed["hidden"] == 0


def test_parse_testsuites_root_sums_children():
    first = _suite(
        name="python-3.9.25", tests=2, failures=1,
        cases=(("tests/test_a.py", "test_one"), ("tests/test_a.py", "test_two")),
        failing=(("tests/test_a.py", "test_two"),),
    )
    second = _suite(
        name="python-3.9.25", tests=1,
        cases=(("tests/test_b.py", "test_three"),),
    )
    parsed = test_results.parse_junit(_wrap(first + second, root="testsuites"))
    assert parsed["tests"] == 3
    assert parsed["failures"] == 1
    assert parsed["failed_names"] == ["tests/test_a.py::test_two"]


def test_parse_skipped_cases_are_not_failing():
    data = _wrap(
        _suite(
            tests=2,
            skipped=1,
            cases=(("tests/test_a.py", "test_one"), ("tests/test_a.py", "test_two")),
            extra='<testcase classname="tests/test_a.py" name="test_two"><skipped/></testcase>',
        )
    )
    parsed = test_results.parse_junit(data)
    assert parsed["skipped"] == 1
    assert parsed["failed_names"] == []


def test_parse_error_child_counts_as_failing():
    data = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<testsuite name="python-3.9.25" tests="1" failures="0" errors="1" skipped="0">'
        b'<testcase classname="tests/test_a.py" name="test_setup">'
        b"<error>bad fixture</error></testcase></testsuite>"
    )
    parsed = test_results.parse_junit(data)
    assert parsed["failed_names"] == ["tests/test_a.py::test_setup"]


def test_parse_accepts_long_versions():
    parsed = test_results.parse_junit(_wrap(_suite(name="python-3.10.12")))
    assert parsed["python"] == "3.10.12"


def test_parse_refuses_bare_3x_suite_name():
    # CI always sets an exact version (e.g. python-3.14.1), even on the
    # "3.x" job; a bare python-3.x suite name is not a real report.
    with pytest.raises(ValueError):
        test_results.parse_junit(_wrap(_suite(name="python-3.x")))


# Parsing: refusals ------------------------------------------------------------


def test_parse_refuses_doctype_without_parsing():
    evil = (
        b'<?xml version="1.0"?><!DOCTYPE suite [<!ENTITY x "boom">]>'
        b'<testsuite name="python-3.9.25" tests="0" failures="0" errors="0" skipped="0"/>'
    )
    with pytest.raises(ValueError) as excinfo:
        test_results.parse_junit(evil)
    assert "boom" not in str(excinfo.value)


@pytest.mark.parametrize("tag", [b"<!doctype", b"<!DOCTYPE", b"<!ENTITY", b"<!entity"])
def test_parse_refuses_doctype_case_insensitive(tag):
    with pytest.raises(ValueError):
        test_results.parse_junit(
            tag + b' x><testsuite name="python-3.9.25" tests="0"/>'
        )


def test_parse_refuses_oversized_input():
    big = (
        b'<?xml version="1.0"?><testsuite name="python-3.9.25" '
        b'tests="0" failures="0" errors="0" skipped="0">'
        + b"<!-- " + b"y" * (test_results.MAX_JUNIT_BYTES + 1) + b" -->"
        + b"</testsuite>"
    )
    assert len(big) > test_results.MAX_JUNIT_BYTES
    with pytest.raises(ValueError) as excinfo:
        test_results.parse_junit(big)
    assert "y" * 16 not in str(excinfo.value)


def test_parse_refuses_malformed_xml():
    with pytest.raises(ValueError):
        test_results.parse_junit(b"<testsuite name=\"python-3.9.25\" tests=\"oops")


def test_parse_refuses_too_many_elements():
    many = b"".join(b'<testcase classname="a" name="b"/>' for _ in range(60_000))
    data = (
        b'<?xml version="1.0"?><testsuite name="python-3.9.25" '
        b'tests="60000" failures="0" errors="0" skipped="0">' + many + b"</testsuite>"
    )
    with pytest.raises(ValueError):
        test_results.parse_junit(data)


@pytest.mark.parametrize(
    "name",
    ["junit", "python-2.7", "python-3", "python-3.9.25" + ".1", "3.9.25", "", "python-3.9;rm"],
)
def test_parse_refuses_bad_suite_name(name):
    with pytest.raises(ValueError):
        test_results.parse_junit(_wrap(_suite(name=name)))


def test_parse_refuses_mismatched_suite_names():
    first = _suite(name="python-3.9.25", tests=1, cases=(("tests/test_a.py", "test_one"),))
    second = _suite(name="python-3.14.1", tests=1, cases=(("tests/test_b.py", "test_two"),))
    with pytest.raises(ValueError):
        test_results.parse_junit(_wrap(first + second, root="testsuites"))


@pytest.mark.parametrize(
    "attrs",
    [
        'tests="-1" failures="0" errors="0" skipped="0"',
        'tests="3" failures="many" errors="0" skipped="0"',
        'tests="3.5" failures="0" errors="0" skipped="0"',
        'tests="3" failures="0" errors="0" skipped="-2"',
    ],
)
def test_parse_refuses_bad_counts(attrs):
    data = (
        f'<?xml version="1.0"?><testsuite name="python-3.9.25" {attrs}/>'.encode()
    )
    with pytest.raises(ValueError):
        test_results.parse_junit(data)


# Hostile names ----------------------------------------------------------------


HOSTILE = "x" * 10_000
EVIL_NAMES = [
    ("tests/test_a.py", "test_`code`"),
    ("tests/test_a.py", "test_<b>bold</b>"),
    ("tests/test_a.py", "test_[link](http://x)"),
    ("tests/test_a.py", "test_line\nbreak"),
    ("tests/test_a.py", HOSTILE),
    ("tests/test_a.py", "test_\r_carriage"),
    ("tests/test_a.py", "test_*star*"),
]


def _hostile_doc():
    # Well-formed XML whose names still defeat the allow-list: backticks,
    # brackets, stars, an entity-decoded <b> tag, a newline (XML normalises
    # it to a space, and spaces are not allow-listed), and a 10k blob.
    names = [
        "test_`code`",
        "test_[link](http://x)",
        "test_*star*",
        "test_&lt;b&gt;bold&lt;/b&gt;",
        "test_line\nbreak",
        HOSTILE,
    ]
    attrs = (
        f'tests="{len(names)}" failures="{len(names)}" '
        'errors="0" skipped="0"'
    )
    body = "".join(
        f'<testcase classname="tests/test_a.py" name="{n}">'
        "<failure>boom</failure></testcase>"
        for n in names
    )
    return body, attrs, len(names)


def test_hostile_names_never_shown_and_counted():
    names = [n for _, n in EVIL_NAMES]
    parsed_names = []
    hidden = 0
    for name in names:
        if test_results.NAME_RE.fullmatch(name) and len(name) <= 200:
            parsed_names.append(name)
        else:
            hidden += 1
    assert parsed_names == []
    assert hidden == len(names)
    for name in names:
        assert name not in test_results.render_comment_line(
            [{"version": "3.9", "conclusion": "failure"}],
            {"3.9": {
                "python": "3.9.25", "tests": len(names), "failures": len(names),
                "errors": 0, "skipped": 0, "passed": 0,
                "failed_names": [], "hidden": hidden,
            }},
        )


def test_parse_counts_unlisted_names_as_hidden():
    body, attrs, count = _hostile_doc()
    data = (
        f'<?xml version="1.0"?><testsuite name="python-3.9.25" {attrs}>'.encode()
        + body.encode()
        + b"</testsuite>"
    )
    parsed = test_results.parse_junit(data)
    assert parsed["failed_names"] == []
    assert parsed["hidden"] == count


def test_parse_refuses_raw_markup_in_names():
    # A raw "<" inside an attribute is not well-formed XML at all.
    data = (
        b'<?xml version="1.0"?><testsuite name="python-3.9.25" '
        b'tests="1" failures="1" errors="0" skipped="0">'
        b'<testcase classname="tests/test_a.py" name="test_<b>">'
        b"<failure>boom</failure></testcase></testsuite>"
    )
    with pytest.raises(ValueError):
        test_results.parse_junit(data)


def test_parse_error_text_is_never_echoed():
    # Assembled at runtime so the literal never appears in the repo.
    marker = "failure-payload-" + "not-echoed-4821"
    data = (
        b'<?xml version="1.0"?><testsuite name="python-3.9.25" '
        b'tests="1" failures="1" errors="0" skipped="0">'
        b'<testcase classname="tests/test_a.py" name="test_one">'
        b"<failure>" + marker.encode() + b"</failure></testcase></testsuite>"
    )
    with pytest.raises(ValueError) as excinfo:
        test_results.parse_junit(b"<!DOCTYPE x>" + data)
    assert marker not in str(excinfo.value)
    parsed = test_results.parse_junit(data)
    assert marker not in repr(parsed)
    assert marker not in test_results.render_job_summary(parsed)
    line = test_results.render_comment_line(
        [{"version": "3.9", "conclusion": "failure"}], {"3.9": parsed}
    )
    assert marker not in line


# Summary table ----------------------------------------------------------------


def test_render_job_summary_table():
    parsed = test_results.parse_junit(
        _wrap(_suite(name="python-3.14.1", tests=4, failures=1, errors=1,
                      skipped=1,
                      cases=(("tests/test_a.py", "test_one"),
                             ("tests/test_a.py", "test_two"),
                             ("tests/test_b.py", "test_three"),
                             ("tests/test_b.py", "test_four")),
                      failing=(("tests/test_a.py", "test_two"),
                               ("tests/test_b.py", "test_four"))))
    )
    table = test_results.render_job_summary(parsed)
    assert "| Python | passed | failed | errors | skipped |" in table
    assert "3.14.1" in table
    assert "`tests/test_a.py::test_two`" in table
    assert "`tests/test_b.py::test_four`" in table


def test_render_job_summary_all_passing_has_no_names():
    table = test_results.render_job_summary(test_results.parse_junit(_wrap(_suite())))
    assert "3.9.25" in table
    assert "`" not in table


# Comment line -----------------------------------------------------------------


def _valid(version="3.9.25", **over):
    base = {
        "python": version, "tests": 527, "failures": 0, "errors": 0,
        "skipped": 0, "passed": 527, "failed_names": [], "hidden": 0,
    }
    base.update(over)
    return base


def test_comment_line_all_passing():
    line = test_results.render_comment_line(
        [{"version": "3.9", "conclusion": "success"}],
        {"3.9": _valid()},
    )
    assert line == "**Tests:** 527 passed on 3.9.25."


def test_comment_line_two_versions():
    line = test_results.render_comment_line(
        [
            {"version": "3.9", "conclusion": "success"},
            {"version": "3.x", "conclusion": "success"},
        ],
        {"3.9": _valid("3.9.25"), "3.x": _valid("3.14.1")},
    )
    assert line == "**Tests:** 527 passed on 3.9.25 and 3.14.1."


def test_comment_line_different_counts_stay_separate():
    line = test_results.render_comment_line(
        [
            {"version": "3.9", "conclusion": "success"},
            {"version": "3.x", "conclusion": "success"},
        ],
        {"3.9": _valid("3.9.25", tests=10, passed=10),
         "3.x": _valid("3.14.1", tests=9, passed=9)},
    )
    assert line == "**Tests:** 10 passed on 3.9.25; 9 passed on 3.14.1."


def test_comment_line_failed_with_names():
    line = test_results.render_comment_line(
        [{"version": "3.14", "conclusion": "failure"}],
        {"3.14": _valid("3.14.1", tests=527, failures=2, passed=525,
                        failed_names=["tests/test_x.py::test_one",
                                      "tests/test_y.py::test_two"])},
    )
    assert line.startswith("**Tests:** **failed** on 3.14.1")
    assert "`tests/test_x.py::test_one`" in line
    assert "`tests/test_y.py::test_two`" in line


def test_comment_line_failed_without_results():
    line = test_results.render_comment_line(
        [{"version": "3.14", "conclusion": "failure"}], {}
    )
    assert "3.14" in line
    assert "no valid results file" in line


def test_comment_line_api_failure_beats_clean_xml():
    line = test_results.render_comment_line(
        [{"version": "3.14", "conclusion": "failure"}],
        {"3.14": _valid("3.14.1")},
    )
    assert "**failed** on 3.14.1" in line


def test_comment_line_passed_without_results_is_degraded():
    line = test_results.render_comment_line(
        [{"version": "3.9", "conclusion": "success"}],
        {"3.9": None},
    )
    assert "no valid results file" in line
    assert "**failed**" not in line


def test_comment_line_invalid_results_is_degraded():
    line = test_results.render_comment_line(
        [{"version": "3.9", "conclusion": "success"}],
        {"3.9": "invalid"},
    )
    assert "no valid results file" in line


def test_comment_line_mismatched_version_is_degraded():
    line = test_results.render_comment_line(
        [{"version": "3.9", "conclusion": "success"}],
        {"3.9": _valid("3.14.1")},
    )
    assert "no valid results file" in line


def test_comment_line_no_jobs_is_unavailable():
    assert test_results.render_comment_line(None, {}) == (
        "**Tests:** results unavailable."
    )
    assert test_results.render_comment_line([], {}) == (
        "**Tests:** no test jobs found."
    )


def test_comment_line_ignores_unrelated_jobs():
    line = test_results.render_comment_line(
        [
            {"version": "3.9", "conclusion": "success"},
            {"version": "other", "conclusion": "failure"},
        ],
        {"3.9": _valid()},
    )
    assert "other" not in line


def test_comment_line_caps_names_at_twenty():
    names = [f"tests/test_many.py::test_case_{i:02d}" for i in range(25)]
    line = test_results.render_comment_line(
        [{"version": "3.9", "conclusion": "failure"}],
        {"3.9": _valid(tests=25, failures=25, passed=0, failed_names=names)},
    )
    assert line.count("`tests/test_many.py::test_case_") == 20
    assert "and 5 more" in line


def test_comment_line_names_use_code_spans():
    line = test_results.render_comment_line(
        [{"version": "3.9", "conclusion": "failure"}],
        {"3.9": _valid(tests=1, failures=1, passed=0,
                       failed_names=["tests/test_x.py::test_one"])},
    )
    assert "`tests/test_x.py::test_one`" in line


def test_comment_line_skipped_counts():
    line = test_results.render_comment_line(
        [{"version": "3.9", "conclusion": "success"}],
        {"3.9": _valid(skipped=3, passed=524)},
    )
    assert "524 passed" in line
    assert "3 skipped" in line


# CLI --------------------------------------------------------------------------


def test_summary_cli_prints_table(tmp_path, capsys):
    junit = tmp_path / "junit.xml"
    junit.write_bytes(_wrap(_suite()))
    rc = test_results.main(["summary", "--junit", str(junit)])
    assert rc == 0
    assert "3.9.25" in capsys.readouterr().out


def test_summary_cli_bad_file_prints_notice_and_exits_zero(tmp_path, capsys):
    junit = tmp_path / "junit.xml"
    junit.write_bytes(b"not xml at all")
    rc = test_results.main(["summary", "--junit", str(junit)])
    assert rc == 0
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1


def test_summary_cli_missing_file_prints_notice_and_exits_zero(tmp_path, capsys):
    rc = test_results.main(["summary", "--junit", str(tmp_path / "absent.xml")])
    assert rc == 0
    assert capsys.readouterr().out.strip()


# Regression tests for the adversarial review ----------------------------------

EVIL_DOCTYPE = (
    '<!DOCTYPE x [<!ENTITY e "BOOM">]>'
    '<testsuite name="python-3.9.25" tests="0" failures="0" '
    'errors="0" skipped="0"/>'
)


@pytest.mark.parametrize(
    "encoding", ["utf-16-le", "utf-16-be", "utf-16", "utf-32", "utf-32-le", "utf-32-be"]
)
def test_parse_refuses_non_utf8_before_parsing(encoding):
    # A non-UTF-8 document can smuggle markup past a raw-byte scan, and the
    # XML parser would sniff the encoding and parse it anyway. Refuse first.
    data = ('<?xml version="1.0"?>' + EVIL_DOCTYPE).encode(encoding)
    with pytest.raises(ValueError) as excinfo:
        test_results.parse_junit(data)
    assert "not well-formed" not in str(excinfo.value)


def test_parse_refuses_utf16_declaration_before_parsing():
    data = (
        '<?xml version="1.0" encoding="utf-16"?>' + EVIL_DOCTYPE
    ).encode("utf-16")
    with pytest.raises(ValueError) as excinfo:
        test_results.parse_junit(data)
    assert "not well-formed" not in str(excinfo.value)


def test_parse_refuses_latin1_declaration_before_parsing():
    # Latin-1 bytes that are not valid UTF-8, naming their own encoding.
    data = (
        '<?xml version="1.0" encoding="latin-1"?>'
        '<testsuite name="python-3.9.25" tests="0" failures="0" '
        'errors="0" skipped="0"><!-- caf\xe9 --></testsuite>'
    ).encode("latin-1")
    with pytest.raises(ValueError) as excinfo:
        test_results.parse_junit(data)
    assert "not well-formed" not in str(excinfo.value)


def test_parse_accepts_utf8_bom():
    parsed = test_results.parse_junit(b"\xef\xbb\xbf" + _wrap(_suite()))
    assert parsed["python"] == "3.9.25"
    assert parsed["passed"] == 3


@pytest.mark.parametrize("order", [["success", "failure"], ["failure", "success"]])
def test_comment_line_duplicate_version_failure_wins(order):
    # A PR controls its own ci.yml, so a same-named job must never mask a
    # failure, whichever order the jobs API lists them in.
    jobs = [{"version": "3.9", "conclusion": conclusion} for conclusion in order]
    line = test_results.render_comment_line(jobs, {"3.9": _valid()})
    assert "**failed**" in line
    assert "passed on" not in line


@pytest.mark.parametrize(
    "conclusion", ["cancelled", "skipped", "neutral", "action_required", "stale"]
)
def test_comment_line_non_failure_conclusions_are_not_bold_failed(conclusion):
    line = test_results.render_comment_line(
        [{"version": "3.9", "conclusion": conclusion}],
        {"3.9": _valid(tests=10, failures=1, passed=9,
                       failed_names=["tests/test_x.py::test_one"])},
    )
    assert "**failed**" not in line
    assert f"job {conclusion}" in line


@pytest.mark.parametrize("conclusion", ["failure", "timed_out"])
def test_comment_line_failure_and_timed_out_stay_bold_failed(conclusion):
    line = test_results.render_comment_line(
        [{"version": "3.9", "conclusion": conclusion}],
        {"3.9": _valid(tests=10, failures=1, passed=9,
                       failed_names=["tests/test_x.py::test_one"])},
    )
    assert "**failed**" in line


def test_comment_line_skips_malformed_job_entries():
    line = test_results.render_comment_line(
        [
            {"version": 3.9, "conclusion": "success"},
            {"version": "3.9"},
            "not-a-dict",
            {"version": "3.9", "conclusion": "success"},
        ],
        {"3.9": _valid()},
    )
    assert line == "**Tests:** 527 passed on 3.9.25."


def test_comment_line_all_malformed_jobs_means_no_jobs():
    line = test_results.render_comment_line(
        [{"version": 3.9}, "not-a-dict"], {"3.9": _valid()}
    )
    assert line == "**Tests:** no test jobs found."


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_comment_line_inconsistent_counts_are_degraded(conclusion):
    # failures + errors + skipped exceeds tests: the counts are invalid, so
    # the version degrades instead of reporting pass or fail from them.
    line = test_results.render_comment_line(
        [{"version": "3.9", "conclusion": conclusion}],
        {"3.9": _valid(tests=10, failures=8, errors=3, skipped=0, passed=0)},
    )
    assert "no valid results file" in line


def test_comment_line_non_string_failed_names_degrade():
    line = test_results.render_comment_line(
        [{"version": "3.9", "conclusion": "failure"}],
        {"3.9": _valid(failures=1, passed=526, failed_names=[123])},
    )
    assert "no valid results file" in line


def test_worst_conclusion_of_nothing_raises():
    with pytest.raises(ValueError):
        test_results.worst_conclusion([])


def test_job_summary_still_renders_inconsistent_counts():
    # The CI-side summary table is a convenience view: it renders what the
    # file says, even when the counts don't add up (the comment line refuses).
    parsed = test_results.parse_junit(_suite(tests=1, failures=1, skipped=1,
                                             failing=(("tests/test_alpha.py", "test_one"),)))
    table = test_results.render_job_summary(parsed)
    assert "3.9.25" in table
