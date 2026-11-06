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
   - **Why:** CI uses only GitHub's own `checkout` and `setup-python`
     actions. Nothing from the marketplace can run.
2. **Fork pull request workflows from outside collaborators:** "Require
   approval for all external contributors".
   - **Why:** a stranger's PR can't run code in your Actions until you
     approve it.
3. **Workflow permissions:** "Read repository contents and packages
   permissions". **Uncheck** "Allow GitHub Actions to create and approve pull
   requests".
   - **Why:** workflows get a read-only token by default. The one job that
     needs more (posting the evidence comment) asks for exactly that in
     the workflow file.

Click **Save** in each section.

## The `main` ruleset

A *ruleset* is GitHub's newer form of branch protection: a named set of
rules applied to matching branches. The rules for this repo are in
[`.github/rulesets/protect-main.json`](../.github/rulesets/protect-main.json).

### Step 1: import it (now)

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
once. Once the first PR with `.github/workflows/ci.yml` has run:

1. **Settings → Rules → Rulesets → protect-main**.
2. Check **Require status checks to pass**.
3. Check **Require branches to be up to date before merging**.
4. Click **Add checks**, type `ci-ok`, and select it. Choose **GitHub
   Actions** as the source, so no other app can report a fake `ci-ok`.
5. Click **Save changes**.

The same PR that adds CI also adds this rule to `protect-main.json`, so the
file stays the source of truth.

**Why only `ci-ok`:** it's a final job that passes only when every other CI
job passed. If the Python versions in the test matrix change later, the
ruleset doesn't need editing.

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
2. When the checks are green (once step 2 is done) and the conversations
   are resolved, click **Squash and merge**.
3. Edit the commit message if needed, and confirm. The branch deletes
   itself.
