## What and why

<!-- One paragraph. Link the spec for a new module: docs/modules/<id>.md -->

## Before / after evidence

<!-- CI posts one comment with before/after diffs of every scenario on the
fixture house (docs/REVIEW.md, "Evidence"): text diffs for command output,
screenshots for any web UI. Explain every changed scenario here.
Never include output or screenshots from a real house. -->

| Scenario | Changed? | Explanation |
|----------|----------|-------------|
|          |          |             |

### Muse view (optional)

<!-- Screenshot of a Muse chat using the changed command, driven by the fixture house only. -->

## Review gates

- [ ] 1. Automated checks green (Python 3.9 and newest, ruff, conformance, hygiene)
- [ ] 2. Independent agent review: findings and dispositions below
- [ ] 3. Threat and privacy review (if required): checklist results below
- [ ] 4. Spec-vs-code review: false or unverifiable claims below
- Deep review:
  <!-- Applies to a new module; changes to http allow-lists, secrets,
  identifiers, envelope, storage, drop-ins, install, CI, or ci-guard. For
  those changes, gates 2–3 using the author's model family is a strong
  reason to run it. Same-family review alone does not put low-risk changes
  in scope. Use exactly one form:
  Deep review: ran (<model>, <effort>), N findings
  Deep review: skipped: <reason>
  Out-of-scope PRs delete the line. See docs/REVIEW.md,
  "Optional: deep red-team review". -->
- [ ] 5. Jon's sign-off

### Findings

<!-- One line each: gate, finding, then `fixed` / `won't fix: <reason>` / `not a problem: <reason>` -->

## Not tested

<!-- E.g. "live controller not available". Be specific. -->
