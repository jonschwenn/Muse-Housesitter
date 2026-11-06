# Decisions

Each entry records what was decided, why, and what was rejected. Open
decisions are listed first. Changing a decided entry needs Jon's approval
and a new entry that supersedes it. Entries are never edited away.

## Open

| # | Question | Proposal |
|---|----------|----------|
| D7 | **Proactive messages** (`musegadget send-user-msg`). | Not in v1, because it needs background activity. Revisit with a concrete need. |
| D10 | **UniFi API-key auth** instead of a View Only password account (module item U4). | Password first, because its endpoint support is known. Add the key option once U4 is verified on a real console. |
| D11 | **SDK run-as account with or without sudo.** Sudo is required to install. Whether Muse's run-as account keeps sudo afterwards is separate. With sudo, Muse can e.g. restart `musegadget` and install drop-ins itself. Without it, those steps stay with the owner. | Owner's choice, documented in README. Housesitter needs no sudo at run time. `doctor` reports which mode is in effect. |

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

### D5. Integrate through SDK drop-in commands, not by importing `musegadget` (2026-11-04, proposed by agent, pending Jon)
- **Why:** Drop-ins are the SDK's documented extension point for owner
  commands. They keep Housesitter testable without the SDK and immune to
  changes in SDK internals. The SDK's `AGENTS.md` says most of the package
  is internal and can change.
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
  *(superseded by the amendment below: now four jobs, with posting moved to
  `evidence-comment`)*:
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
