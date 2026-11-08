# Review gates

A change is **done** only after it passes every gate below, in order.
Record the result of each gate in the PR description, with one line per
finding: `fixed`, `won't fix: <reason>`, or `not a problem: <reason>`.
Gates 2–4 are adversarial. The reviewer's job is to find what is wrong, not
to confirm that the author is right.

| # | Gate | Who | Required for |
|---|------|-----|--------------|
| 1 | Automated checks | CI | every change |
| 2 | Independent agent review | a fresh agent, not the author | every change to code |
| 3 | Threat and privacy review | a fresh agent or Jon | new module; any change to http allow-lists, secrets, identifiers, envelope, storage, drop-ins, or install |
| 4 | Spec-vs-code review | a fresh agent | every change that touches code or docs |
| 5 | Human sign-off | Jon | every merge to `main` |

## Evidence: before and after (every PR)

Every PR shows what changed in Housesitter's output, **before** and
**after**, inside the PR itself (D13). There are two forms, depending on
what changed:

| Component | Evidence |
|-----------|----------|
| Command-line output (envelopes, `describe`, `doctor`, errors) | **Text diffs.** CI posts one PR comment. For each changed scenario it shows a colored ```` ```diff ```` block from before to after, with the full before and after outputs in collapsible sections below. |
| Web UI (any page Housesitter serves, if one ever exists) | **Rendered screenshots.** A headless browser captures each page before and after, plus a highlighted visual diff. The images are hosted on the `pr-evidence` branch (D12) and embedded in the same comment. |

Every PR gets the Web UI section below when the artifact has
`ui_manifest.json`, even when neither side has `web/`. The comment is
text-only when the artifact has no manifest, and the `pr-evidence` branch
is created on first use.

**Status:** text diffs are built (`scripts/pr_evidence.py`, tested in
`tests/test_pr_evidence.py`), and so are rendered screenshots
(`scripts/ui_evidence.py`, tested in `tests/test_ui_evidence.py`).
PR #1 predates the tooling and has no evidence.

**How it works**
- `tests/evidence/scenarios.json` lists the scenarios: a fixed set of
  Housesitter invocations against the **fixture house**.
  - `describe` and `doctor`
  - each module command with representative params
  - a truncated (paged) result
  - one or two error cases (e.g. bad password, TLS mismatch)
- `scripts/pr_evidence.py` runs every scenario on the PR's base (`main`, in
  a temporary worktree) and on its head. It writes the comment body: a
  summary table (scenario, changed or unchanged, envelope size as the SDK
  measures it), then a diff for each changed scenario. Volatile fields
  (`collected_at`, saved ids) are normalised so they don't show up as noise.
- Each run gets a fresh, empty `HOME` and a minimal environment: the
  variables the SDK gives drop-in commands, plus `PYTHONPATH` and
  `PYTHONDONTWRITEBYTECODE`. A real config or secret therefore can't be
  *found* through `HOME` or the environment. That guards against accidents,
  not against code that reads absolute paths on purpose.
- A scenario that is meant to fail (an error case) sets `"expect_exit"`. The
  comment calls out any scenario whose exit code doesn't match, and
  `tests/test_pr_evidence.py` fails on it.
- In `.github/workflows/ci.yml`, the `evidence` job runs the capture and
  comment scripts on every PR with a read-only token: all jobs in that
  workflow have `contents: read` and nothing else. It writes the text
  comment body to the job summary and uploads the evidence artifact
  (`evidence.md` plus the UI PNGs and `ui_manifest.json`, if any). It never
  pushes and never comments. The `ci-ok` job passes only when `test` and
  `evidence` both passed or were skipped on purpose.
- A separate privileged workflow, `.github/workflows/evidence-publish.yml`,
  runs on `workflow_run` after each CI run and always executes the file from
  the default branch — never PR code. It resolves the PR number through the
  API from trusted event data (head branch and head repository owner;
  `workflow_run.pull_requests` is empty for forks), verifies the API's
  current head SHA still matches the run's head SHA, validates the artifact
  as fully untrusted data with main's `ui_evidence.validate_artifact` (via
  `scripts/publish_evidence.py render`), rebuilds the Web UI section from
  the validated manifest with main's `pr_evidence` renderer, pushes only the
  validated PNGs to the orphan `pr-evidence` branch (below), and creates or
  edits **one** bot comment. Fork PRs get the comment too, because the
  write token belongs to this workflow, not to the PR. When the artifact is
  invalid or the run is stale or ambiguous, it publishes nothing: no push,
  no comment.
- Each CI `test` job runs pytest with `--junitxml`, writes a results table
  to its job summary page, and uploads `test-results-<matrix python>`.
  The publisher downloads the XML and reads the run attempt's test-job
  conclusions from the GitHub API. Main's `scripts/publish_evidence.py
  render` adds a one-line `**Tests:**` summary under the evidence heading,
  including failing names when space permits.
  - Verdicts come from the API. For duplicate jobs reporting the same
    Python version, the worst conclusion wins regardless of job order.
    Only `failure` and `timed_out` show as failed; other non-success
    conclusions show as `job <conclusion>` (for example, `job cancelled`).
  - Counts and names from PR-produced XML are **author-attested**, like
    the text diffs. Main's `scripts/test_results.py` accepts only UTF-8,
    strips a UTF-8 byte-order mark, and rejects other encodings before
    parsing. It caps XML size and element count, rejects `DOCTYPE` and
    `ENTITY`, validates counts and suite versions, and allows only
    length-capped names with a fixed set of characters. If failures +
    errors + skipped exceed tests, the file is invalid for the comment
    line; the CI job summary table still renders those counts as a
    convenience view. At most 20 failing names appear; others are counted
    but not shown. Failure messages and tracebacks are never published.
  - Jon's option A (D21): missing or invalid results or unusable job data
    degrade only the Tests line: `results unavailable`, `no test jobs
    found`, or `no valid results file`. To fit the comment size budget,
    failing names are dropped first (counts and verdicts stay), then a
    fixed short line is used, and if even that doesn't fit, the line is
    left out. Test data never blocks the publish or hides
    the screenshots and diffs reviewers need. Invalid UI artifacts still
    refuse the whole publish.
- Trust model (the fine print that makes the above honest). The `CI`
  workflow runs on `pull_request`, so its file comes from the PR: a PR that
  edits `ci.yml` controls that run's jobs, including what the artifact
  contains — which is why the publisher treats the artifact as hostile and
  why `ci.yml` itself is a protected path under `ci-guard` (D16). The
  publisher's guarantees (runs main's code, resolves the PR from the API,
  validates before pushing, pins URLs) hold because `workflow_run`
  workflows always run from the default branch, and the resolve step also
  pins the workflow file path, so a same-named workflow a PR adds cannot
  trigger a publish. A malicious PR can therefore break or fake its own
  evidence, but it cannot get a green `ci-ok`/`ci-guard` pair onto `main`
  through the guard, and it cannot publish a fake comment. The text diffs,
  the UI check results, the table, and the pixels are all
  **author-attested**: PR code produces them, exactly like the text diffs. A
  red CI run is reliable for honest mistakes, not against a malicious PR.
  Screenshots remain a review convenience, never trusted fact (D16).
- The `main` ruleset also requires `ci-guard`
  (`.github/workflows/ci-guard.yml`, D16): on `pull_request_target` from
  the base commit, it lists the PR's changed files through the API and fails
  when the PR touches a protected path unless its author has admin,
  maintain, or write permission. It never checks out or runs PR code.
- Web UI screenshots work the same way, with the browser in the loop. In
  the `evidence` job, `scripts/ui_evidence.py` serves each tree's own
  `scripts/dev_dashboard.py` with that tree's own `web/` and fixtures, and
  captures every fixture in `tests/fixtures/dashboard/` at phone (360x780,
  full page), desktop (1280x900, full page, light and dark), and e-ink
  (800x480, never full page). A tree without `web/` contributes "no page"
  for its side. CI captures with Google Chrome on GitHub's runner
  (`--browser-channel chrome`); local runs use Playwright's bundled
  Chromium. The top-level `browser` in `ui_manifest.json` records the
  `--browser-channel` value (or `chromium` when unset) and the launched
  browser's version, using the head's record, else the base's, else `null`
  when neither side has `web/`; the manifest stays at version 1,
  and older readers ignore the extra key. The comment names that browser
  and version only when it lists captures. Playwright is a dev/CI-only
  extra (`ui` in `pyproject.toml`), never a runtime dependency.
- Capture file names are `<fixture>__<view>__<side>.png`, parsed from the
  right (the view and side are fixed words). Fixture stems are lowercase
  alphanumerics plus `-` and single `_`, at most 48 chars, with no `__`
  inside; anything else fails validation. The page-height check measures
  the whole page: above 120,000 CSS px (`MAX_PAGE_HEIGHT_PX`), it records a
  "page taller than 120000 px" check failure. This limit covers the
  600-client stress fixture (~104,200 px on phone) with headroom and bounds
  runaway pages. Saved full-page screenshots are separately cropped to the
  top 8,000 CSS px (`MAX_SHOT_HEIGHT_PX`); this crop is not a check failure.
  It keeps each image well under the 8 MB per-image cap and readable in a PR
  comment. In Google Chrome on the CI runner, the stress fixture's
  full-height shots were ~9 MB each, so main's publisher refused the whole
  evidence upload. A visual change only below the first 8,000 px of a page
  will not appear in the screenshots or the pixel diff.
- The same script runs the UI checks, and any failure fails the `evidence`
  job: a console error or warning, a page error, a CSP or Trusted Types
  violation, a dialog, horizontal page overflow, internal clipping (an
  element with `overflow` hidden/clip whose scroll size exceeds its client
  size by more than 1 px; intentionally scrollable regions are allowed), an
  e-ink shot that is not exactly 800x480, and a page taller than the
  120,000 CSS px page-height limit. A failed `evidence` run still gets its
  comment: the publisher accepts
  `success` and `failure` conclusions, the Checks column shows the failures,
  and a top-line notice says the checks failed. `ci-ok` still fails, because
  `evidence` did.
- Capture pairs are classified as added, removed, changed, or unchanged by
  decoded pixel equality, not file bytes: a stdlib `zlib` PNG decoder keeps
  the comparison a unit-testable pure function with no new dependency
  (byte-identical files short-circuit, which is sound, and anything else is
  decoded and compared pixel by pixel). In the comment, added captures get
  only the after image and a note that the base has no page; removed captures
  get only the before image and a note that the head no longer has the page.
  Unchanged captures get a table row only, with no images. Changed pairs get
  before and after images plus a highlighted diff image: agreed pixels
  dimmed towards white, differing pixels red. A
  pair whose PNGs won't decode is marked changed with no diff image and a
  "diff unavailable" note, instead of crashing before the manifest is
  written.
- The publisher (main's `scripts/publish_evidence.py render`, calling into
  main's `scripts/pr_evidence.py`) builds the Web UI section from the
  validated `ui_manifest.json` — it never pastes pre-rendered markdown
  files from the artifact, and the `evidence` job's `evidence.md` carries
  the text section only. The comment carries only known boilerplate plus a
  table and images built from the re-validated manifest: capture names
  against the capture regex, status against the
  added/removed/changed/unchanged enum, view against the known views,
  problems escaped (code spans) and length-capped, browser channel against
  an allow-list (`chromium` → Playwright's bundled Chromium, `chrome` →
  Google Chrome, `chrome-beta` → Google Chrome Beta, `msedge` → Microsoft
  Edge), and browser version against 1–4 dot-separated groups of digits,
  at most 32 characters. The manifest must be an object with a `captures`
  list, or the artifact is invalid and nothing is published. Each capture's
  image fields must fit its status (added: only an after image; removed:
  only a before image; unchanged: none), only a changed capture without a
  diff image may carry a diff error, and removed must carry no check
  problems. Entries that break these rules are dropped and counted in the
  "Omitted N UI capture(s)" note, so a hostile manifest cannot push images
  the comment never shows or report check failures for a page that no
  longer exists. An unrecognised browser gets "The manifest does
  not record a recognised browser." followed by the images sentence; bad
  values are never echoed. In the published comment, the browser and images
  sentences appear only when at least one capture passes validation. With
  no captures to list, the published comment has no table, not even an empty
  header. In the published comment, the nothing-to-show sentence appears
  only when the manifest's `captures` list is empty. The note
  "**Omitted N UI capture(s) with invalid manifest entries.**" appears only
  when at least one entry fails validation. The two never appear together.
  Image URLs always match
  `https://raw.githubusercontent.com/<repo>/__HOUSESITTER_EVIDENCE_SHA__/pr/<n>/<head-sha>/<capture-name>`
  exactly. Scenario titles and ids in the text section go through code spans
  the same way. This protects against accidents and hostile *data*; a
  hostile PR author controls the PR-side building scripts, but those scripts
  never run in the publisher (see the trust model above).
- Image URLs carry a placeholder for the evidence push commit SHA. The
  publisher substitutes the real SHA only after pushing (main's
  `publish_evidence.py substitute`, which validates the SHA), so every image
  URL is pinned to an immutable commit and never changes afterwards. When
  the run brings no PNGs (every capture unchanged, or no web UI on either
  side), it skips the commit and push entirely and still posts the comment,
  which then needs no SHA. The repo is public, so
  `https://raw.githubusercontent.com/<owner>/<repo>/<sha>/<path>` works.
- Trust model, publisher side: the publisher treats the screenshot artifact
  as untrusted data produced by PR code. It validates file names, PNG magic
  bytes, size and count caps, rejects symlinks and subdirectories, refuses
  to push or post anything when validation fails, and pushes only to the
  orphan `pr-evidence` branch — then posts the pixels as the author's claim,
  exactly like the text diffs. Validation passing means the files are PNGs
  of a sane size, not that the pixels are truthful. The validator is main's
  `ui_evidence.validate_artifact`, called by main's publisher script, so
  there is no second copy to drift; `tests/test_ui_evidence.py` pins the
  wiring (which script entry points the workflow calls).
- Retention: the `pr-evidence` branch only grows (one directory per head
  SHA). Only added, removed, or changed captures are pushed; unchanged
  captures are listed in the table only, and runs that bring no PNGs push
  nothing at all. Removed captures' before images are pushed and pictured in
  the comment, so reviewers can see what disappeared rather than just a
  "removed" row. The publisher pushes exactly the images the full Web UI
  section pictures: the same function decides both, so files named only by
  dropped entries are not pushed. (When the details don't fit the comment
  budget, the comment falls back to the table, and no images are pictured.)
  Pruning and its trade-off are described in DECISIONS D12, "Retention."
- A PR that changes no output (docs, CI) still gets the comment, reading
  "no output change." That is evidence too.

**Rules**
- **Fixture data only.** Never capture a real house: no live controller
  output, and no screenshot of a real Muse chat, unless it was driven by the
  fixture house (Muse can run `--fixture` too). The script refuses to run if
  `HOUSESITTER_LIVE` or `HOUSESITTER_CONFIG` is set, or if
  `~/.config/housesitter/config.json` exists.
- A new module or command adds its scenarios **in the same PR**.
  `tests/test_pr_evidence.py` checks that every scenario runs. Conformance
  fails if a command of an installed built-in module has no scenario.
  The test-only demo module is excluded because evidence runs the
  installed CLI; third-party entry points are also excluded.
- An unexpected change in the evidence counts as a finding in gate 2.
- Optional extra: when a change affects how Muse uses a command, add a
  screenshot of a Muse chat driven by the fixture house, under "Muse
  view" in the PR template.

## Gate 1: automated checks

- ruff, and the full test suite on Python 3.9 and the newest Python.
- The conformance suite and the hygiene scan.
- Test results appear on each `test` job's summary page and in the evidence
  comment's `**Tests:**` line (see Evidence above).
- Automated checks are three workflows. `.github/workflows/ci.yml` ends in
  `ci-ok`, which passes only if `test` and `evidence` both passed or were
  skipped on purpose. `.github/workflows/ci-guard.yml` reports `ci-guard`
  (D16). The `main` ruleset requires both `ci-ok` and `ci-guard`, so neither
  CI nor the guard can be skipped (see `docs/GITHUB.md`).
  `.github/workflows/evidence-publish.yml` publishes the evidence comment;
  it is not a required check and never blocks a merge.
- If CI is red, stop. Don't argue with red CI in a PR description.

## Gate 2: independent agent review

The reviewer gets the **diff, the relevant spec, and AGENTS.md**. It does
not get the author's reasoning or summary. An author who explains the
change pre-loads the conclusion.

Reviewer brief (use as is, filling in the brackets):

> You are reviewing a change to Muse Housesitter, a read-only data-gathering
> tool for a Raspberry Pi. Read `AGENTS.md`, then `[spec path]`, then the
> diff on branch `[branch]` against `main`. Find correctness bugs, broken
> hard rules, missing tests, and behaviour the tests don't pin. For each
> finding give the file and line, a concrete failing input or scenario, and
> severity (blocker / should-fix / nit). Don't report style that ruff
> already enforces. If you find nothing, say what you checked.

- Blockers and should-fix findings are fixed, or answered in writing.
- A finding the author disputes goes to Jon, not back to the same reviewer.
- After fixes, run a **new** review with the same brief plus the previous
  findings and replies. Don't reuse the old reviewer thread.
- The `/code-review` skill can serve as this gate.

## Gate 3: threat and privacy review

Use this checklist. Every item gets *yes*, *no (finding)*, or *n/a (why)*.

**Read-only**
- [ ] Every entry in `http_allow` is a read, or a login/logout that the API needs.
- [ ] No entry is broader than it needs to be (no `.*`, no wildcard methods).
- [ ] Nothing shells out, writes outside the data dir, or opens sockets
      directly. The one exception is the dashboard server's listening
      socket (below).

**Secrets**
- [ ] Secrets come only from `ctx.secret`.
- [ ] Secrets are absent from argv, env dumps, logs, exceptions, envelopes, and saved files.
- [ ] The hostile fixture's planted secrets are asserted absent from output,
      saves, and captured logs. The suite scans Python streams and fds 1/2
      during command execution, plus records reaching `housesitter`
      (including default `ctx.log`). Review covers import-time output,
      threads outliving the command, other or non-propagating loggers
      (including `ctx.log` with `propagate=False`), handlers writing
      elsewhere, and leaks beyond exact-substring matching (MODULES §4).
- [ ] Modules log only through `ctx.log`, as required by Context-only I/O
      (MODULES §3).
- [ ] Auth cookies and tokens are kept in memory only, and logout is called.

**Data leaving the house**
- [ ] Every returned field is in the spec.
- [ ] Identifier fields are marked, and the policy is applied by the core.
- [ ] Upstream fields that are secret-bearing or unnecessary are not
      fetched, or are dropped by allow-list. List them in the PR.
- [ ] Raw dumps (if any) are off by default and stripped of `x_*` and
      secret fields.

**Untrusted input**
- [ ] Device and network names are marked untrusted and capped. The suite
      checks declared untrusted fields; review checks that the declarations
      cover every field that needs them.
- [ ] No upstream string is used in a description, a file path, a regex,
      or a format string.
- [ ] Large or malformed upstream responses fail cleanly: size cap,
      timeout, and a JSON parse error becomes `upstream_unexpected`.

**TLS and auth**
- [ ] TLS is verified by default. `insecure` is explicit and warns on every
      envelope.
- [ ] The account the docs recommend has the least privilege the upstream
      offers (e.g. UniFi View Only).

**Dashboard server** (when it changes)
- [ ] GET and HEAD only. Every other method returns 405.
- [ ] It never contacts upstream, never triggers collection, and opens
      files read-only.
- [ ] The default bind is `127.0.0.1`. Unexpected `Host` headers get 421.
- [ ] No directory listings and no dotfiles.
- [ ] The CSP and other headers match DASHBOARD.md §5 exactly.
- [ ] The front end passes the `hostile` fixture: no script runs, and no
      markup is rendered from data.

**Install surface**
- [ ] Drop-ins are root-owned 644, and `exec` is an absolute path to the
      installed entry point.
- [ ] Nothing the module needs requires sudo at run time.

## Gate 4: spec-vs-code review

The reviewer gets the docs (`README.md`, `AGENTS.md`, `docs/**`, every
`SKILL.md`) and the code. The job is to find every **claim that isn't
true**.

- List every factual claim the docs make about behaviour, such as "returns
  X," "refuses Y," "CI runs Z," or "mode 600." Check each one against the
  code or a test, and mark it *verified (file:line or test name)*, *false*,
  or *unverifiable*.
- Anything false or unverifiable is a finding. Fix the doc or the code.
- Check that every field in the code's `data` output is in the spec table,
  and every spec field is produced. Conformance compares spec tables with
  declarations and checks returned keys, not whether every declared field
  is produced; the reviewer checks coverage, *meanings and units* too.
- Check that SKILL.md's interpretation guidance (ranges, units, caveats)
  matches what the code returns.

This gate exists because the project's first handoff claimed CI, a browser
test, and assets that weren't there.

## Gate 5: human sign-off

Jon reads the PR, which contains:

1. what changed and why
2. the **before/after evidence** comment (above), and an explanation of
   every changed scenario
3. the gate results with findings and their dispositions
4. what was **not** tested, such as "live controller not available"
5. for a new module, the spec link

Only Jon merges to `main`. Agents don't commit or push without Jon's
explicit instruction for that change.
