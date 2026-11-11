# GitHub setup

How the `jonschwenn/Muse-Housesitter` repository is configured, and why.
These settings are applied by hand in the GitHub web UI. This file is the
record of what they should be (D12). If you change a setting, update this
file in the same PR.

## Merge settings

**Settings → General → Pull Requests**

1. **Uncheck** "Allow merge commits".
2. **Keep checked** "Allow squash merging". Set its default commit message
   to "Pull request title and description".
3. **Uncheck** "Allow rebase merging".
4. **Check** "Automatically delete head branches".

**Why:** each PR lands on `main` as one commit, authored by you and named
after the PR. Merged branches clean themselves up.

## Actions settings

**Settings → Actions → General**

1. **Actions permissions:** choose "Allow jonschwenn, and select
   non-jonschwenn, actions and reusable workflows", then check only "Allow
   actions created by GitHub".
   - If your page offers "Require actions to be pinned to a full-length
     commit SHA", check it.
   - **Why:** CI uses only GitHub's own actions (`checkout`,
     `setup-python`, `upload-artifact`, `download-artifact`). Nothing from
     the marketplace can run.
2. **Fork pull request workflows from outside collaborators:** "Require
   approval for all external contributors".
   - **Why:** a stranger's PR can't run code in your Actions until you
     approve it.
3. **Workflow permissions:** "Read repository contents and packages
   permissions". **Uncheck** "Allow GitHub Actions to create and approve pull
   requests".
   - **Why:** workflows get a read-only token by default. The jobs that
     need more ask for exactly that in the workflow file
     (`evidence-publish.yml`: `actions: read` wherever it fetches
     artifacts, `contents: write` only on the push job,
     `pull-requests: write` only on the comment job; `ci-guard.yml`:
     `contents: read` and `pull-requests: read`).
     The publisher's `build` job uses its existing `actions: read` to
     download the test-results artifacts and read job conclusions from the
     API; no permission was widened.

The publisher serialises runs with a per-PR concurrency group keyed by
head repository owner and branch, without cancelling the active run. It
checks the head again before pushing images and before posting. Artifact
refusals produce a fixed notice with a reason code instead of silently
leaving out the evidence; stale or ambiguous PR lookups and moved heads
still stop without writing. These controls add no permissions (D34).

Click **Save** in each section.

## The `main` ruleset

A *ruleset* is GitHub's newer form of branch protection: a named set of
rules applied to matching branches. The rules for this repo are in
[`.github/rulesets/protect-main.json`](../.github/rulesets/protect-main.json).

### Step 1: import it for a fresh repository

1. Download the JSON file. On GitHub, open
   `.github/rulesets/protect-main.json` on the branch that contains it and
   click **Download raw file**.
2. Go to **Settings → Rules → Rulesets**.
3. Click the **New ruleset** dropdown, then **Import a ruleset**, and pick
   the file.
4. Check the form. It should show:
   - name `protect-main`
   - enforcement **Active**
   - target **Default branch**
   - **no bypass list**
   - these rules:
     - Restrict deletions
     - Block force pushes
     - Require linear history
     - Require a pull request before merging, with 0 approvals,
       conversation resolution required, and Squash as the only merge
       method
     - Require status checks to pass: `ci-ok` **and** `ci-guard` from
       GitHub Actions, branches up to date. The JSON file already lists
       both; each check only becomes selectable in steps 2 and 3 after it
       has run at least once. If the ruleset already exists, do steps 2
       and 3 by hand instead of importing again.
5. Click **Create**.

What each rule does:

| Rule | Effect |
|------|--------|
| Restrict deletions | `main` can't be deleted. |
| Block force pushes | `main`'s history can't be rewritten. |
| Require linear history | No merge commits on `main`. |
| Require a pull request | Nobody, including you and any agent using your SSH key, can push straight to `main`. **0 approvals**, because GitHub doesn't let you approve your own PR. Merging the PR *is* your sign-off. Unresolved review comments block the merge. |
| No bypass list | The rules apply to everyone. As admin you can still edit or disable the ruleset in an emergency, which is an explicit, visible act. |

### Step 2: require CI (after the first CI run)

GitHub only offers a status check in this list after it has run at least
once. For a fresh repository, run `.github/workflows/ci.yml` before
selecting `ci-ok`:

1. **Settings → Rules → Rulesets → protect-main**.
2. Check **Require status checks to pass**.
3. Check **Require branches to be up to date before merging**.
4. Click **Add checks**, type `ci-ok`, and select it. Choose **GitHub
   Actions** as the source, so no other app can report a fake `ci-ok`. In
   the JSON this is `integration_id` 15368, GitHub Actions' app id.
5. Click **Save changes**.

`protect-main.json` already contains this rule, so the file stays the
source of truth. Importing the file again would apply it too, but GitHub
creates a second ruleset when you import, so edit the existing one instead.

**Why only `ci-ok` (plus `ci-guard`):** `ci-ok` is a final job that
passes only when every other CI job passed. If the Python versions in the
test matrix change later, the ruleset doesn't need editing. `ci-guard` is
the guard workflow's single job: it passes only when the PR touches no
protected path, or its author has write access. `evidence-publish.yml` is
deliberately **not** required: it only posts the evidence comment and never
blocks a merge.

### Step 3: require ci-guard or re-apply the ruleset

Both `ci-ok` and `ci-guard` are required now. For a fresh repository, put
`ci-guard.yml` on the target branch and let it run before selecting the
check: the workflow runs from that branch, and a required check that has
never run blocks every PR. To add the check by hand:

1. **Settings → Rules → Rulesets → protect-main**.
2. Click **Add checks**, type `ci-guard`, and select it. Choose **GitHub
   Actions** as the source, so no other app can report a fake `ci-guard`.
   In the JSON this is `integration_id` 15368, GitHub Actions' app id.
3. Click **Save changes**.

To re-apply the ruleset after editing `protect-main.json`, use the GitHub
CLI (the ruleset id is looked up, not hard-coded; `protect-main.json` is
the source of truth, so the file and the ruleset must match afterwards):

```sh
ID=$(gh api repos/jonschwenn/Muse-Housesitter/rulesets \
  --jq '.[] | select(.name=="protect-main") | .id')
gh api -X PUT repos/jonschwenn/Muse-Housesitter/rulesets/$ID \
  --input .github/rulesets/protect-main.json
```

Jon applied the ruleset change on 2026-11-07. The live `protect-main`
ruleset matches `protect-main.json` exactly: both `ci-ok` and `ci-guard`
are required from GitHub Actions (`integration_id` 15368), branches must
be up to date, and there are no bypass actors. After re-applying, verify
these settings on the ruleset page.

### Optional: require signed commits

Edit `protect-main`, check **Require signed commits**, and save. This works
without setting up any keys, because squash merges made in the GitHub web
UI are signed by GitHub, and those are the only commits that land on
`main`.

### Check that it works

From any clone:

```sh
git checkout main && git commit --allow-empty -m "should be rejected" && git push origin main
```

GitHub should refuse the push with `GH013: Repository rule violations`.
Afterwards, clean up the local commit with `git reset --hard origin/main`.

## Merging a PR

1. Open the PR. Read the description, the evidence comment, and the gate
   results.
2. When both required checks are green and the conversations
   are resolved, click **Squash and merge**.
3. Edit the commit message if needed, and confirm. The branch deletes
   itself.
