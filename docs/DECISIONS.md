# Decisions

Each entry records what was decided, why, and what was rejected. Open
decisions are listed first. Changing a decided entry needs Jon's approval
and a new entry that supersedes it. Entries are never edited away.

## Open

| # | Question | Proposal |
|---|----------|----------|
| D7 | **Proactive messages** (`musegadget send-user-msg`). | Not in v1: Muse decides when to collect and when to look (rule 3, D14), and pushing messages would reverse that. Revisit with a concrete need. |
| D10 | **UniFi API-key auth** instead of a View Only password account (module item U4). | Password first, because its endpoint support is known. Add the key option once U4 is verified on a real console. |
| D11 | **SDK run-as account with or without sudo.** Sudo is required to install. Whether Muse's run-as account keeps sudo afterwards is separate. With sudo, Muse can e.g. restart `musegadget` and install drop-ins itself. Without it, those steps stay with the owner. | Owner's choice, documented in `docs/INSTALL.md`. Housesitter needs no sudo at run time. `doctor` reports which mode is in effect. |

## Decided

### D0. Reimagined from scratch (2026-11-04)
Housesitter is a **data-gathering** tool that Muse runs on a Pi through the
gadget SDK. It is not a standalone dashboard. The old skeleton's web hub,
inventory editor, what-if model, and exception rules are dropped, because
analysis is Muse's job. The old export and draft spec stay in `.context/` as
reference only.

### D1. License: MIT (2026-11-04, Jon)
Keep the MIT `LICENSE` from the initial commit. This supersedes the Muse
handoff's "license not chosen; don't publish."

### D2. Personal tool; real identifiers by default, no hashing (2026-11-04, Jon)
- **Decision:** Housesitter is explicitly for personal use: the owner's own
  network and the owner's own Muse. Output includes real MACs and IPs by
  default (`identifiers: "full"`). `none` remains available to omit them.
- **Why:** Muse is more useful when its ids match what the owner sees in
  UniFi. Hashing added a key to manage and only kept identifiers out of the
  default output path. It was never a barrier against Muse, which can read
  the Pi's files anyway.
- **Rejected:** HMAC-hashed identifiers as the default.

### D3. Python ≥ 3.9, stdlib-only runtime, JSON config (2026-11-04, Jon)
- **Why:** The SDK supports Python 3.9 (Raspberry Pi OS Bullseye). Matching
  that floor means Housesitter runs wherever the SDK does. Python 3.9 has no
  stdlib TOML reader, and YAML is not in the stdlib at all. Muse also reads
  and writes JSON easily.
- **Rejected:**
  - Python ≥ 3.11 with TOML, because it would exclude Bullseye Pis.
  - YAML, because it would add a dependency.

### D5. Integrate through SDK drop-in commands, not by importing `musegadget` (2026-11-04, proposed by agent; approved 2026-11-07, Jon)
- **Why:** Drop-ins are the SDK's documented extension point for owner
  commands. They keep Housesitter testable without the SDK and immune to
  changes in SDK internals. The SDK's `AGENTS.md` says most of the package
  is internal and can change.
- **Framing:** Housesitter depends only on the SDK's documented drop-in JSON
  format and stdin/stdout command protocol, including limits and naming rules.
  The conformance suite checks the drop-in format, names, and output limits
  (#12). SDK internals may
  change independently.
  Each command runs in its own process, so a hanging or crashing module fails
  only that command and leaves the SDK's connection to Muse intact.
- **Rejected:**
  - Subclassing `musegadget.service.Service`, which couples us to the SDK's
    connection lifecycle and needs the SDK in tests.
  - `system.run` only, which gives Muse no discoverable commands.

### D8. AGENTS.md is canonical; CLAUDE.md imports it (2026-11-04)
- **Why:** The SDK uses the AGENTS.md convention, and Muse Code and other
  agents read it. Claude Code reads CLAUDE.md. One source avoids the two
  drifting apart.

### D9. First module is `unifi_wifi` (2026-11-04)
- One module per upstream system and data domain.
- Command names follow `housesitter.<module_id>.<command>`, to fit the
  SDK's 64-character name limit.
- **Rejected:** a generic `wifi` module with vendor providers. That is
  premature until a second vendor exists, and a second vendor can become a
  second module.

### D4. Install into `/opt/housesitter` (2026-11-04, Jon)
- Housesitter is installed into a venv at `/opt/housesitter`, via
  `pip install` from a release wheel or git tag. This mirrors the SDK's
  `/opt/musegadget`.
- `housesitter dropins` generates the drop-in files without privileges, and
  `install-commands` copies them into place with
  `sudo install -o root -m 644`.

### D6. Retention is configurable (2026-11-04, Jon)
- Saved results are pruned by count after each save.
- `housesitter.retention` sets the default for all modules. It is 500
  files.
- `modules.<id>.retention` overrides it for one module.
- `0` means keep everything, so the owner prunes by hand.
- **Rejected:** a fixed limit, and time-based retention. Time-based would
  delete a meaningful "before" snapshot just because it got old.

### D13. Every PR shows before/after evidence (2026-11-04, Jon)
- **Decision:** every PR carries before and after evidence of Housesitter's
  output, in one CI comment. Command-line output is shown as text diffs,
  and any web UI as rendered screenshots. See REVIEW.md, "Evidence."
- **Why:** reviewing a data tool's behaviour from a code diff alone is hard.
  Seeing the actual output change makes regressions obvious. Jon wants
  rendered before/after images specifically for GUI work.
- **Constraint:** the images use fixture data only, never a real house
  (AGENTS rule 7).

### D12. Minimal GitHub-only CI, with `main` locked by a ruleset (2026-11-04, Jon)
- **CI:** one workflow, `.github/workflows/ci.yml`, with three jobs
  *(superseded first by the `scaffold-ci` amendment below: four jobs, then
  by the `ci-guard` bootstrap: two jobs plus `ci-ok`, with posting moved to
  `evidence-publish.yml`)*:
  - `test`: Python 3.9 and newest; ruff, pytest
  - `evidence`: PRs only
  - `ci-ok`: an aggregate of the other two
  It uses only `actions/checkout` and `actions/setup-python`, pinned to
  commit SHAs *(superseded: also the artifact actions)*. Permissions are
  read-only, except that `evidence` may write PR comments *(superseded: only
  `evidence-comment` may)*. No external services.
- **Evidence hosting:** text diffs live in the PR comment itself.
  Screenshots, needed only once a web UI exists, go on an orphan
  `pr-evidence` branch, pushed by CI and never by an agent.
- **`main` ruleset:** `.github/rulesets/protect-main.json`, applied by hand
  using the steps in `docs/GITHUB.md`.
  - It requires a PR, squash merges only, resolved conversations, and a
    green `ci-ok` check.
  - It blocks force pushes and deletion, and it has no bypass actors.
  - It requires 0 approvals, because GitHub doesn't let authors approve
    their own PRs. Jon's sign-off is merging.
- **Rejected:**
  - Separate workflows per concern.
  - Marketplace actions.
  - Release automation (releases are tagged by hand).
  - Required approvals.
  - A standing admin bypass (an admin can disable the ruleset in an
    emergency).
- **Amended (2026-11-06, `scaffold-ci`):** posting the comment moved into its
  own job, `evidence-comment`. It is the only job with write permission, and
  it never runs PR code. `ci-ok` now aggregates `test`, `evidence`, and
  `evidence-comment`. Two more GitHub-owned actions are used,
  `upload-artifact` and `download-artifact`, also pinned to SHAs.
- **Amended (2026-11-06):** the Python 3.9 test job runs on `ubuntu-24.04`,
  because `ubuntu-latest` is now Ubuntu 26.04 and has no Python 3.9 build.
  When 24.04 runners are retired, the 3.9 job needs another home (e.g. a
  `python:3.9` container) or the Python floor needs revisiting. Python 3.9
  itself reached end of life upstream in October 2025, and the floor
  exists only to match Raspberry Pi OS Bullseye (D3).
- **Amended (2026-11-06, `ci-guard` bootstrap, D16):** three workflows,
  not one. `ci.yml` keeps `test` and `evidence` (plus the `ci-ok`
  aggregate) with read-only tokens everywhere: it builds the text evidence
  and uploads the artifact, but never pushes or comments. Publishing moves
  to `evidence-publish.yml` on `workflow_run`, which always runs the file
  from the default branch: it resolves the PR through the API, validates
  the artifact with main's `ui_evidence.validate_artifact` (via main's
  `scripts/publish_evidence.py`, which also rebuilds the Web UI section
  with main's `pr_evidence` renderer), pushes only validated PNGs to the
  orphan `pr-evidence` branch, and posts or edits the single bot comment —
  for fork PRs too. `ci-guard.yml` on `pull_request_target` (from the base
  commit, never PR code) fails the PR when it touches a protected path
  unless its author has admin, maintain, or write permission. The ruleset
  requires `ci-ok` **and** `ci-guard`. The old `evidence-comment` job in
  `ci.yml` is gone (its text-only posting moves into the publisher), and
  the screenshot writer was never merged, so there is no inline validator
  copy and nothing that can drift. This partly reverses the earlier
  "separate workflows per concern" rejection above: the unprivileged build
  (`ci.yml`) and the privileged publish (`evidence-publish.yml`) must be
  separate workflows because only `workflow_run` runs the publisher from
  the default branch, and the guard (`ci-guard.yml`) must be separate
  because only `pull_request_target` runs it from the base.
- **Retention:** the `pr-evidence` branch only grows: one directory per head
  SHA, plus a commit each time evidence is pushed. Only added, removed, or
  changed captures are pushed; unchanged ones appear in the table only.
  As of 2026-11-07, the push carries exactly the images the full comment
  section pictures, including removed captures' before images (issue #21).
  A run that brings no PNGs pushes nothing at all: no commit, no push, just
  the comment. A full clone fetches this branch too, so contributors who
  don't need it can clone with `--single-branch`. Image URLs are pinned to
  commit SHAs, so deleting old directories in a new commit frees nothing
  and breaks nothing. Actually reclaiming space means replacing the
  branch's history (e.g. a fresh orphan commit with only recent PRs,
  force-pushed by hand), which breaks the images in older PR comments. That
  is the trade-off pruning accepts.

### D14. A read-only dashboard; Muse decides when to collect (2026-11-06, Jon)
- **Decision:** Housesitter serves a read-only web dashboard on the Pi
  (`docs/DASHBOARD.md`). It shows what Muse last collected, and may later
  drive an e-ink display. Hard rule 3 changes from "nothing runs in the
  background" to: collection happens only when Muse (or the owner) runs a
  command. Housesitter installs no schedules, and how often to collect is up
  to Muse. The dashboard server is the one allowed long-running process. It
  only serves saved results: it never contacts upstream, never triggers
  collection, and never writes.
- **Why:**
  - Muse analyses deltas and trends, so it should own the cadence.
  - Muse's ad-hoc demo on the Pi already serves a page on port 8080 that
    updates when the scripts run, and a dashboard is useful for people.
  - It partly reverses D0, which dropped the old web hub. The analysis
    parts of that hub stay dropped.
- **How:**
  - The backend and front end are split by a versioned JSON contract
    (`housesitter.dashboard/1`) with invented fixtures.
  - Claude builds the backend and tests. GPT (Codex, `gpt-6.1-sol`) builds
    front-end options for a bakeoff, and Jon picks the winner.
- **Security:**
  - The server answers GET only and sends a strict Content-Security-Policy
    (no inline scripts).
  - It binds to `127.0.0.1` by default. The owner sets a LAN address to
    reach it from other devices.
  - No login in v1, because it is read-only. Anyone who can reach the port
    sees the dashboard, identifiers included.
- **Rejected:**
  - Static HTML files with no server (Jon chose a local server).
  - Scheduled collection on the Pi.
  - Server-side analysis on the dashboard.

### D15. Always-on allow-list refusals; 3xx as `upstream_unexpected` (2026-11-06, Jon)
- **Decision:** two points where the as-built core is stricter than the
  first design drafts, kept as the design:
  - Percent-escapes (`%`) and `..` segments are refused for every request
    target, no matter what an allow-list entry's pattern would match. Only
    a query string (`?...`) can be opted into, per entry, via
    `allow_query`.
  - A 3xx response is never followed and never returned as a normal
    response: it is raised as `upstream_unexpected`, with the numeric
    status kept on the error's `status` attribute, so callers such as
    flavour detection can tell a redirect apart without reading headers.
- **Why:**
  - Encoded traversal must not depend on how carefully an entry's pattern
    is written. A permissive pattern such as `/api/.*` is legitimate for
    some controllers, but it must still be safe against `%2e%2e`.
  - Following a redirect would leave the allow-list: the `Location` can
    point at any path. Raising keeps every upstream contact inside
    `fullmatch`. Keeping the numeric status still lets callers
    distinguish "the controller redirected me" from other unexpected
    states, which flavour detection needs.
- **Rejected:**
  - Leaving `%`/`..` to each pattern (one careless pattern reopens
    traversal).
  - Returning the 3xx as a normal `Response` (tempts the caller to follow
    `Location`, re-entering the client outside the allow-list).
  - Dropping the status and reporting a bare `upstream_unexpected`
    (flavour detection could no longer tell a redirect apart).

### D16. Bootstrap: an outsider cannot weaken validation and stay green (2026-11-06, Jon)
- **Threat model:** screenshots are a review convenience, not trusted
  fact; a faked screenshot is acceptable risk. What must be impossible: an
  outside bad actor (e.g. a fork PR) altering the testing/validation
  mechanism (workflows, the hygiene scan, the conformance suite, the
  evidence scripts, pytest/ruff config) and getting a mergeable green
  result. The owner (Jon) and his agents are trusted; outsiders are not.
- **What this changes (see also the `ci-guard` bootstrap amendment to
  D12):**
  - `ci.yml` is unprivileged: every job has `contents: read` only. It
    builds the text evidence, uploads the artifact, and aggregates `ci-ok`.
    UI check failures still fail CI.
  - `evidence-publish.yml` on `workflow_run` always runs the file from the
    default branch. It resolves the PR number through the API from trusted
    event data (never from the artifact), verifies the head SHA still
    matches, validates the artifact with main's `validate_artifact` (via
    main's `scripts/publish_evidence.py`, which also rebuilds the Web UI
    section with main's renderer), pushes only validated PNGs to the orphan
    `pr-evidence` branch, and posts or edits the single bot comment. Fork
    PRs get comments too. An invalid, stale, or ambiguous run publishes
    nothing.
  - `ci-guard.yml` on `pull_request_target` runs from the base commit and
    never touches PR code. `scripts/ci_guard.py` (stdlib only, unit-tested)
    lists the PR's changed files through the API — paginated, failing
    closed on error or when the PR may exceed GitHub's 3000-file listing
    cap — and fails the `ci-guard` check when a protected path is touched
    unless the PR author has admin, maintain, or write permission
    (collaborator permission from the API, not `author_association`).
  - The `main` ruleset requires `ci-ok` **and** `ci-guard`.
- **What is protected:** `.github/**` (workflows, ruleset, templates);
  `tests/test_hygiene.py` (the repo-hygiene scan);
  `tests/conformance/**` (the module conformance suite);
  `tests/test_pr_evidence.py`, `tests/test_ui_evidence.py`,
  `tests/test_publish_evidence.py`, `tests/test_test_results.py`,
  `tests/test_ci_guard.py` (the tests that pin the builders, validators,
  publisher, test-results parser and renderers, and guard);
  `tests/evidence/**` (the scenario list — removing a scenario hides a
  regression the same way editing the builder does);
  `scripts/pr_evidence.py`, `scripts/ui_evidence.py`,
  `scripts/publish_evidence.py`, `scripts/test_results.py`,
  `scripts/ci_guard.py` (evidence builders, validators, privileged publisher,
  test-results parser and renderers, guard);
  `pyproject.toml` (pytest/ruff config, dependency pins, entry points); and
  anywhere in the tree, `conftest.py`, `pytest.ini`, `setup.cfg`,
  `tox.ini`, `ruff.toml`, `.ruff.toml`, `sitecustomize.py`,
  `usercustomize.py`, `*.pth` (tooling config and interpreter start-up
  hooks), `.gitattributes` (`linguist-generated` or `-diff` can collapse
  or hide a file's diff in review), plus `tests/**/__init__.py` (flips
  pytest's import mode for the whole suite).
- **What is not protected, and why that is accepted:**
  - *Screenshots stay author-attested.* Validation proves the files are
    sane PNGs, never that the pixels are truthful. Faked pixels are the
    accepted risk in the threat model above.
  - *Trusted collaborators pass the guard by permission.* A PR from an
    author with write access may touch protected paths; the guard logs them
    as a notice. This is not an escalation: such an author can already push
    to `main`-adjacent branches and edit these files.
  - *Fork PRs simply cannot touch protected paths.* A fork that needs such
    a change must ask Jon (or his agents) to make it instead.
  - *New files are not blocked.* A new module needs new tests, so the
    guard cannot forbid new files outright. Residual vectors through new
    files were probed, not assumed (see below); they are caught by human
    review (gates 2 and 5), not by the guard: a new `conftest.py` is
    blocked, but in the full test run a new test module could still
    `import test_hygiene` and neuter it at import time (verified: the patch
    lands in the same module object pytest collected), or set module-level
    `pytest_plugins` to load a helper that drops tests from collection
    (verified: honored in test modules). Two things close this for the
    protected suites:
    - CI re-runs the protected suites (hygiene, conformance, evidence,
      publisher, test-results, guard tests) in a separate pytest process
      that collects only those files ("Protected suites, in isolation" in
      `ci.yml`), so no unprotected test module is ever imported alongside them.
    - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` in CI kills the entry-point
      variant, and `pyproject.toml` (protected) pins the installed
      distributions, so no new entry-point plugin can arrive that way.

    What is left is weakening *ordinary* tests (a module's own tests)
    through such tricks, which human review catches: a test file importing
    another test module, or declaring `pytest_plugins`, is an obvious red
    flag at gates 2 and 5.
- **TOCTOU:** the guard re-runs on every push (`synchronize`), and the
  ruleset requires branches to be up to date, so a merge needs a green
  `ci-guard` on the exact head being merged. A stale result cannot merge.
- **Rejected:**
  - *Status quo, documented honestly.* The old `evidence-comment` model
    ran the writer from the PR's own `ci.yml` for same-repo PRs, so a PR
    editing `ci.yml` controlled the writer; and fork PRs got no comment or
    screenshots at all. Keeping it would leave the exact hole this
    bootstrap closes.
  - *A ruleset bypass list.* The ruleset keeps no bypass actors, as
    before: an admin can still disable the ruleset in an emergency, which
    is an explicit, visible act.

### D17. Dashboard design bakeoff outcome (2026-11-07, Jon)
- **Decision:** use C's overall design, A's per-radio and per-client detail,
  and B's e-ink composition. Use the Slate / Blue palette in light and dark
  modes. The front end in `web/` brings these choices together; the A/B/C
  branches were not merged.
- **Why:** three options were built against the dashboard contract and
  fixtures: A, "control room" (detailed); B, "house at a glance"
  (e-ink-first); and C, "change-first" (leads with what changed since the
  last snapshot). Jon chose C's emphasis on changes, with A's detail and
  B's e-ink composition.
- **Rejected:**
  - Taking A, B, or C unchanged.
  - The first palette's muted tan/olive, which Jon rejected. The other
    palette options offered were Ink / Violet, Night / Cyan, and
    Porcelain / Coral; Jon chose Slate / Blue.
- **Expected to change:** Jon expects to tweak specific aspects over time
  for actionability and quality. Each change goes through a normal PR with
  screenshot evidence.

### D18. Agents take work only from Jon's issues (2026-11-07, Jon)
- **Decision:** agents act only on issues Jon opened or currently marked
  `ready`, checked by author, last `ready` event, and body edits after
  approval. Only Jon's issue text and comments are instructions; others'
  comments, linked pages, and non-Jon PR text are data. See AGENTS.md,
  "Working from issues," for the checks.
- **Why:** anyone can open an issue or comment in this public repo; without
  this rule, a stranger could instruct agents. This applies hard rule 8
  (untrusted input) to issues. The edit check catches body changes by
  anyone other than Jon after he approves it.
- **Assumption:** trusting Jon-opened issues without an edit check assumes
  Jon is the repo's only collaborator; adding one means revisiting this rule.
- **Rejected:** counting any collaborator's `ready`; following any issue text.
- **Status:** Jon calls this a two-way door: how we work for now, reversible
  if it proves too heavy. Revisit with a new entry that supersedes this one.

### D19. State independent maintenance and credit upstream work (2026-11-08)
- **Decision:** put an independence notice at the top of the README.
  Maintain upstream credits and references in `ATTRIBUTION.md`, with
  reusable announcement and response wording in `docs/COMMUNICATIONS.md`.
- **Why:** people should know who maintains the project, which upstream
  systems it is designed for, and where support comes from before they
  install or share it. Local execution must not imply that results sent to
  Muse stay on the Pi.
- **Scope:** documentation only. No hard rule, software license, runtime
  behaviour, or module output changes.
- **Rejected:** relying on a footer-only notice, using logos or badges that
  imply official status, and conflating software licensing with service
  access or brand permissions.

### D20. Preserve the personal introduction; group README notices at the end (2026-11-08, Jon)
- **Decision:** restore Jon's original four-paragraph introduction and put
  affiliation, privacy, and support notes after the license section at the
  end of the README. This supersedes D19's notice-placement decision.
- **Why:** preserve the author's personal voice and keep reference notices
  together. Upstream credits remain in `ATTRIBUTION.md`.
- **Rejected:** rewriting the personal introduction or placing affiliation
  notes before it.

### D21. Test results in the PR comment; bad test data degrades only its line (2026-11-07, Jon)
- **Decision:** each CI `test` job writes a results table to its job summary
  and uploads pytest's JUnit XML as `test-results-<matrix python>`. Main's
  publisher adds a one-line `**Tests:**` summary under the evidence comment
  heading. Pass/fail comes from the run attempt's GitHub API job
  conclusions; for duplicate jobs reporting the same Python version, the
  worst conclusion wins regardless of job order. Only `failure` and
  `timed_out` show as failed; other non-success conclusions show as
  `job <conclusion>`. Counts and failing names come from validated
  PR-produced XML and remain author-attested, like the text diffs. Failure
  messages and tracebacks are never published.
- **Bad data (option A):** a missing or invalid results file, or unusable
  job data, degrades only the Tests line: `results unavailable`,
  `no test jobs found`, or `no valid results file`. To fit the comment size
  budget, failing names are dropped first (counts and verdicts stay), then
  a fixed short line is used, and if even that doesn't fit, the line is left
  out. Test data never blocks the publish. The rest
  of the evidence comment still publishes. Invalid UI artifacts still
  refuse the whole publish.
- **Why:** the test summary is a one-line add-on whose pass/fail already
  comes from the trusted API. Refusing everything for bad test data would
  hide the screenshots and diffs a reviewer needs.
- **Rejected:**
  - Refusing the whole publish on bad test data, like the UI artifact.
  - Taking pass/fail from the XML instead of the API: PR-produced counts
    cannot establish whether the test job passed.

### D22. Optional deep red-team review on a different model family (2026-11-08, Jon)
- **Decision:** explicitly consider an optional deep review on a different
  model family for high-risk changes and record whether it ran or was
  skipped: a new module, or any change to http allow-lists, secrets,
  identifiers, envelope, storage, drop-ins, install, CI, or `ci-guard`.
  Same-family review in gates 2–3 is a strong reason to run it for those
  changes, not a trigger for low-risk changes. See REVIEW.md, "Optional:
  deep red-team review."
- **Why:** a model family reviewing its own code shares its blind spots.
  On issue #13 (`unifi_wifi`), Muse Spark wrote the code and did both
  adversarial reviews (gates 2 and 3). Each ran in a fresh session, but all
  used the same model family. Claude and Codex found the later bugs, not
  Spark's re-check. The project now uses an extra deep review on a different
  model family for high-risk changes; this makes considering it deliberate.
- **Rejected:** making it mandatory for every change (too costly for
  doc-only PRs); leaving it informal (then it never gets considered).
- **Status:** superseded in part by D30 (cross-family gate 2–3 reviews).
  Otherwise a two-way door: how we work for now, reversible if it proves
  too heavy or too light. Revisit with a new entry that supersedes this one.

### D23. Dashboard server config, saved-data status, and packaging (2026-11-08, Jon)
- **Decision:** `housesitter serve` uses the optional `housesitter.dashboard`
  section: `bind` (IP-literal string, default `"127.0.0.1"`), `port` (integer
  1–65535, default `8080`), `allowed_hosts` (up to 32 hostnames or IPs,
  default `[]`), and `house_label` (string up to 64 characters or null,
  default `null`). DASHBOARD §5 defines their meaning and reload behaviour.
  The server accepts GET and HEAD, clarifying D14's GET-only wording.
  V1 status comes from saved snapshots only: `ok` when readable data exists,
  `never` when no snapshots exist, and `error` when saved snapshots exist but
  none are readable. `last_attempt_at` equals `last_success_at`. Recording
  collection attempts and reporting their failures is **Planned** for a
  follow-up issue. `web/` stays at the repo root and ships as
  `housesitter.web` package data through setuptools' `package-dir` mapping.
- **Why:** saved results are enough for the read-only server without changing
  collection. Shipping the existing web directory makes pip installs usable
  while preserving the fixture evidence pipeline.
- **Rejected:** moving `web/` to `src/housesitter/web/`, which would break the
  base-vs-head evidence pipeline and `ci-guard`; adding attempt recording to
  the runner in this change.

### D24. Hidden identifiers stay as null fields on the dashboard (2026-11-08, Jon)
- **Decision:** with `housesitter.identifiers: "none"`, the dashboard view
  keeps every identifier key (client `id`, `ip`, `ap_id`; AP `id`;
  `summary.clients_by_ap[].ap_id`) with a `null` value. `invoke` output
  still leaves the keys out. DASHBOARD §4 lists the contract rules that
  don't hold in this mode.
- **Why:** the dashboard contract pins exact key sets, and the page reads
  those fields. Nulls keep the shape while showing no MACs or IPs. The cost
  is that pages can't match clients or APs between snapshots in this mode.
- **Rejected:** ignoring the setting on the dashboard (it would show MACs
  on a page anyone on the network can open, against the owner's choice);
  making the keys optional with a new contract version (more front-end work
  for a rarely used mode).

### D25. Checkout installer and optional dashboard service (2026-11-08, proposed, pending Jon)
- **Decision (proposed, pending Jon):** install from a git release tag using
  the script in that checkout. Run it as the owner's sudo-capable account,
  never root; use sudo for specific system steps. Provide `install`,
  `upgrade`, and `uninstall`, each with `--dry-run`. Offer a hardened
  systemd unit for `serve` through `--with-dashboard`, running as the SDK's
  run-as account with read-only access and no access to the secrets directory.
  Do everything inside that account's home as that account, never as root,
  before installing anything system-wide. Build and install from a root-owned
  copy of the checkout. Write the rendered unit straight to `sudo install`
  from stdin, without a temporary file. Never call sudo in `--dry-run`.
- **Why:** one reviewed checkout supplies both code and installation steps;
  the owner can preview changes. The existing run-as account can read its
  private saved data without widening directory permissions.
- **Rejected:** `curl | bash` (no local review step); a root-run script
  (unnecessarily broad privileges); pip from PyPI for now (release tags
  keep code and installer together); a separate dashboard user (it cannot
  read the mode 700 data directory); running home-directory steps as root
  (a symlink planted by the run-as account could redirect a root write).
  D11 remains open and unchanged.

### D26. Guard standard output for the CLI process except `serve` (2026-11-08, Jon)
- **Decision:** guard fds 1/2 and all four Python stdout/stderr streams
  after argument parsing and before loading the registry. Route `serve`
  before the guard: no SIGINT change, final-delivery watchdog, seal, or
  `os._exit` (Jon, 2026-11-09). Its request logs reach real stderr
  (systemd journal).
  For guarded subcommands, discard stray output in a Linux memfd reopened
  through `/proc/self/fd/<n>` for atomic shared position updates. Count
  bytes from that offset without flushing or locking module streams;
  report only the count. A daemon truncates the sink about every 100 ms,
  clearing `O_APPEND` first. Each fd has one owner; the trim thread owns
  its private duplicate. Flush original streams once at startup.
  At production exit, stop trimming, flush and close core-owned real
  streams and saved fds, close the core sink fd, and seal fds 1/2 to
  `/dev/null`. Never flush, close, or replace module-reachable streams.
  Call `os._exit(0)` only after success and complete delivery; otherwise
  hard-exit 1. Failed flushes and escaping exceptions use fixed diagnostics
  without exception text (ARCHITECTURE §4). For `invoke`/`describe`/`doctor`,
  an escaping exception can emit a replacement error envelope only before
  stdout's first attempted write; stderr lines do not mark stdout started.
  Timeout also seals and hard-exits 1.
  In production seal mode, best-effort set SIGINT to the OS default on the
  main thread after guard startup: like SIGTERM, it ends by signal without
  Python shutdown. A code-raised `KeyboardInterrupt` follows the exception
  path. Injected-stream callers default to restore and normal return,
  re-raising escaping exceptions; timeout still invokes the exit callback.
  Start the best-effort five-second final-delivery watchdog just before
  the first CLI stdout or stderr write, after sudo, file, network, and
  handler work; also arm it on timeout delivery and unexpected failure.
  If still alive when it fires, call `os._exit(1)`.
  If guard startup fails, `invoke`/`describe`/`doctor` refuse without loading
  the registry or running handlers, with `upstream_unexpected` and
  "cannot protect command output". Production arms the watchdog before
  delivery and calls `os._exit(1)`; in-process callers return 1. Other
  subcommands except `serve` warn and continue unguarded with no watchdog
  (Jon, 2026-11-08). Nested refusals discard output unless streams are
  injected, avoiding a second JSON document; nested calls without an
  explicit `seal=True` never arm the
  watchdog, which would end the outer process mid-handler.
  Missing or failed memfd creation or `/proc` reopen follows the same
  refusal path, without a non-Linux fallback. Keep the guard in the CLI,
  so direct runner calls still expose stray output to conformance tests.
- **Why:** import-time and late-thread output would otherwise reach Muse.
  A memfd avoids deliberately writing possible secrets to the SD card,
  cannot fill like a pipe when a C writer holds the GIL, and permits
  counting without a module-controlled flush. Sealing and hard exit avoid
  module flushes, finalizers, and threads hanging interpreter shutdown.
  Stdout tracking prevents replacement envelopes after partial delivery;
  the SIGINT policy and watchdog bound shutdown and delivery failures.
  `serve` has no Muse drop-in or JSON stdout consumer, runs no module
  handlers, and logs for the owner. It is long-running, so the watchdog
  would kill it.
- **Rejected:** handler-only guards; Python-stream swaps without fd
  redirection; logging redacted stray text; disk sinks; pipe/drain-thread
  sinks; flushing module streams to count; `/dev/null` during collection
  (no count); guarding in the runner (masks conformance failures); running
  module commands unprotected after only a warning.
- **Trade-offs:** import-time module output while `serve` loads the registry
  reaches its console. A live stdout or stderr reader too slow to finish
  delivery within five seconds is cut off with exit 1. Guarded module
  `print()` debugging is gone; use `ctx.log` or conformance tests. This is
  a guard rail, not a sandbox: modules can discover reserved real fds.
  Fatal interpreter output on fd 2 is lost; module-buffered text is not
  counted. Concurrent byte counts are exact except when append mode is
  set after a trim; clearing `O_APPEND` narrows but cannot close that gap
  (accepted by Jon, 2026-11-09). Memfd pages can be swapped. A GIL-holding
  writer can delay trimming and the runner timeout response: memory and duration
  can grow until it releases the GIL. Module threads and `atexit` hooks
  cannot outlive the guarded process. Children inheriting fds 1/2 can keep
  writing after exit without trimming; modules cannot spawn them under
  the import lint, and core's `sudo install` children receive real fds.
  Linux only: Jon confirmed there is no non-Linux target.

### D27. Install from main until releases are tagged (2026-11-09, Jon)
- **Decision:** the install guide clones `main` and upgrades with `git pull --ff-only`;
  there are no version tags yet. This narrows D25's "install from a git
  release tag" until the first release.
- **Why:** no releases exist, so `--branch v0.1.0` failed on the first real
  install.
- **Rejected:** tagging v0.1.0 now; we are not making versions yet.

### D28. README install is "tell Muse"; full guide in docs/INSTALL.md (2026-11-09, Jon)
- **Decision:** README lists requirements and one line to tell Muse;
  `docs/INSTALL.md` holds the guided-with-Muse steps and the manual steps.
  Muse does the unprivileged parts; the owner runs the one sudo command
  from a fresh clone in their own account, creates the UniFi account, and
  writes the password file.
- **Why:** the first install was done by a Muse agent; a long README mixed
  audiences.
- **Rejected:** passwordless sudo for Muse's account (gives Muse root;
  D11 stays open); Muse handling the password (rule 6).

### D29. unifi_wifi: WiFi clients only, no-AP devices listed separately, vendor never null (2026-11-10, Jon)
- **Decision:** `clients` contains WiFi clients only. Keep devices with
  missing, null, empty, or whitespace-only `ap_mac` separately in
  `no_ap_clients`, ordered by name then id, capped at 10 with a count-only
  warning. Devices beyond the cap are dropped (not returned or saved);
  `summary.no_ap_client_count` still counts them.
  De-duplicate by MAC; WiFi records win across lists. Vendor uses `oui`, or
  `"unknown"` for missing, null, non-string, or blank values, including after
  sanitising; apply this on `get` too. Never move or drop saved records.
  Clients with a non-blank but unmatched AP stay in `clients` with `ap_id`
  and `ap_name` null, without a warning. Dashboards show no-AP devices and
  unplaced clients compactly: collapsed, one line with a count.
- **Why:** the live check found bridged devices reported as wireless with
  no AP and no radio data. Mixing them in made the client list disagree
  with AP counts. Jon wants the data kept but not prominent. One vendor
  fallback gives missing and blank manufacturer values the same meaning.
- **Rejected:** dropping no-AP records (loses data Jon wants); keeping them
  in `clients` with `ap_id` null (counts disagree and shows non-WiFi devices
  as WiFi clients); a flag field on clients; leaving vendor null; dropping
  clients whose AP is not in `aps` (hides real WiFi clients); paging no-AP
  records (a cap is simpler and these lists are short); a cap of 20 (a full
  list of hostile emoji names could use about 49 KB of the 90 KB output budget,
  which cannot be paged).

### D30. Split work by model family (2026-11-11, Jon)
- **Decision:** split work by model family. Muse Spark (`muse-spark-1.3`,
  provider "Muse Code") writes code and tests, including adversarial
  testing that tries to break code. Codex (`gpt-6.1-sol`) handles the front
  end (`web/`) and documentation prose (`docs/`, `AGENTS.md`,
  `docs/DECISIONS.md` entries, module `SKILL.md` files, and `README.md`,
  leaving Jon's own introduction as he wrote it, D20). Claude handles
  orchestration, backend design, briefs, final verification, and PRs.
  For every doc, including `docs/ARCHITECTURE.md`, Claude supplies the
  facts and checks accuracy; Codex writes the prose.
  Every brief that starts work names its model. Gate 2 and gate 3 reviews
  use a model family other than the one that wrote the change, applying
  D22's reasoning to the required gates; the deep review in REVIEW.md
  stays optional. If the assigned agent is unavailable, or for a
  small fix found in review, another agent may do the work; the PR says so.
- **Why:** spread token use across providers and get review from another
  model family. Five audit-fix threads silently defaulted to Claude and
  hit Claude's usage limit together. Separate threads alone do not spread
  the work across providers; each brief must name its model.
- **Rejected:** keeping the split only in issue owner labels (agents not
  working from issues never see it); one model for everything (one
  provider's limit stops all work, and same-family review shares blind
  spots, see D22).
- **Status:** a two-way door: how we work for now, reversible if it proves
  too heavy or too light. Revisit with a new entry that supersedes this one.
- **Amended (2026-11-11, Jon):** the exception changes only who writes the
  change; gates 2 and 3 still use a different model family from the one
  that actually wrote each part. A small fix written by the reviewer's
  family also gets a look from Jon or another model family. The optional
  deep reviewer comes from a family that did not write the change,
  including when Codex wrote it.
