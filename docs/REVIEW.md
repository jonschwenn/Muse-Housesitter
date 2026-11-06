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

There is no web UI today, so only text diffs apply, and the `pr-evidence`
branch does not exist yet.

**Status: Planned.** The tooling is built in the first code PR. PRs merged
before it state "Evidence: n/a, tooling not built yet."

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
- The `evidence` job in `.github/workflows/ci.yml` runs the script on every
  PR, and creates or updates **one** comment.
- A PR that changes no output (docs, CI) still gets the comment, reading
  "no output change." That is evidence too.

**Rules**
- **Fixture data only.** Never capture a real house: no live controller
  output, and no screenshot of a real Muse chat, unless it was driven by the
  fixture house (Muse can run `--fixture` too). The script refuses to run if
  a real config is present.
- A new module or command adds its scenarios **in the same PR**.
  Conformance fails if a registered command has no scenario.
- An unexpected change in the evidence counts as a finding in gate 2.
- Optional extra: when a change affects how Muse uses a command, add a
  screenshot of a Muse chat driven by the fixture house, under "Muse
  view" in the PR template.

## Gate 1: automated checks

- ruff, and the full test suite on Python 3.9 and the newest Python.
- The conformance suite and the hygiene scan.
- All of this is the single workflow `.github/workflows/ci.yml`. Its final
  `ci-ok` job passes only if every other job passed. The `main` ruleset
  requires `ci-ok`, so CI can't be skipped (see `docs/GITHUB.md`).
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
- [ ] Nothing shells out, writes outside the data dir, or opens sockets directly.

**Secrets**
- [ ] Secrets come only from `ctx.secret`.
- [ ] Secrets are absent from argv, env dumps, logs, exceptions, envelopes, and saved files.
- [ ] The hostile fixture's planted secrets are asserted absent everywhere.
- [ ] Auth cookies and tokens are kept in memory only, and logout is called.

**Data leaving the house**
- [ ] Every returned field is in the spec.
- [ ] Identifier fields are marked, and the policy is applied by the core.
- [ ] Upstream fields that are secret-bearing or unnecessary are not
      fetched, or are dropped by allow-list. List them in the PR.
- [ ] Raw dumps (if any) are off by default and stripped of `x_*` and
      secret fields.

**Untrusted input**
- [ ] Device and network names are marked untrusted and capped.
- [ ] No upstream string is used in a description, a file path, a regex,
      or a format string.
- [ ] Large or malformed upstream responses fail cleanly: size cap,
      timeout, and a JSON parse error becomes `upstream_unexpected`.

**TLS and auth**
- [ ] TLS is verified by default. `insecure` is explicit and warns on every
      envelope.
- [ ] The account the docs recommend has the least privilege the upstream
      offers (e.g. UniFi View Only).

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
  and every spec field is produced. Conformance tests this mechanically, and
  the reviewer checks the *meanings and units* too.
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
