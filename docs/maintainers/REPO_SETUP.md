# Repository setup (settings that cannot live in files)

Everything else (README, community files, issue and pull request templates, Dependabot, CI and release
workflows) is in the repository. Apply the settings below once, after creating the repository. Commands
use the GitHub CLI (`gh` 2.96 was used to check the flags) from the repository root, signed in as an
admin of `sainisourabh1-oss/clawback`.

```bash
REPO=sainisourabh1-oss/clawback
```

## 1. Create the repository (private) and push

```bash
gh repo create "$REPO" --private \
  --description "Local, privacy-first email cleanup: bulk unsubscribe and delete with a two-click double confirmation, a paid-subscription finder with savings per currency, proof your unsubscribes worked, regional money finders and GDPR/DPDP/CCPA request drafts. Runs on your computer; nothing is uploaded." \
  --disable-wiki
git remote add origin https://github.com/sainisourabh1-oss/clawback.git
git push -u origin main
```

- **Description** (274 characters, limit 350): as in the command above.
- **Website**: leave empty for now (there is no project site). Do not point it at PyPI until the package
  is published.

## 2. Features and merge settings

```bash
gh repo edit "$REPO" \
  --enable-issues --enable-discussions --enable-projects=false --enable-wiki=false \
  --enable-squash-merge --enable-merge-commit=false --enable-rebase-merge=false \
  --squash-merge-commit-message pr-title-description \
  --delete-branch-on-merge --allow-update-branch
```

- **Discussions: on.** The issue chooser (`.github/ISSUE_TEMPLATE/config.yml`), SUPPORT.md, the feature
  request form and the README Roadmap link to Discussions for questions and ideas; with Discussions off
  those links show a 404. If you prefer them off, remove the "Questions and ideas" entry from
  `config.yml` and the Discussions links from SUPPORT.md, README.md and `feature_request.yml`.
- Wiki and Projects off: the docs live in the repository.

## 3. Topics (20, lowercase)

```bash
gh repo edit "$REPO" --add-topic email,email-cleanup,inbox-cleanup,unsubscribe,newsletter,spam,privacy,local-first,subscriptions,subscription-tracker,personal-finance,gmail,imap,mbox,google-takeout,gdpr,dpdp,ccpa,upi,python
```

## 4. Social preview

There is no API for this; use the web page.

1. Open `https://github.com/sainisourabh1-oss/clawback/settings` (Settings > General).
2. Under **Social preview**, choose **Edit > Upload an image...** and pick
   `docs/brand/social-preview.png` (1280 x 640 PNG, about 320 KB).

## 5. Security

```bash
# Dependabot alerts and security updates
gh api -X PUT "repos/$REPO/vulnerability-alerts"
gh api -X PUT "repos/$REPO/automated-security-fixes"

# Private vulnerability reporting (SECURITY.md, the Code of Conduct and the issue chooser point here)
gh api -X PUT "repos/$REPO/private-vulnerability-reporting"
gh api "repos/$REPO/private-vulnerability-reporting"        # expect {"enabled":true}
```

- GitHub documents private vulnerability reporting for **public** repositories. While the repository is
  private the call may be refused or have no visible effect; run it again right after making the
  repository public, and check the Security tab shows **Report a vulnerability**.
- After the repository is public, also turn on secret scanning and push protection (free for public
  repositories):

  ```bash
  gh repo edit "$REPO" --enable-secret-scanning --enable-secret-scanning-push-protection
  ```

## 6. Actions permissions

Workflows only need read access, except `release.yml`, which asks for `contents: write` in its own job.

```bash
# Default GITHUB_TOKEN: read-only, and Actions may not approve pull requests
gh api -X PUT "repos/$REPO/actions/permissions/workflow" \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false

# Allow only GitHub-made actions plus the PyPI publish action used (later) by release.yml
gh api -X PUT "repos/$REPO/actions/permissions" -F enabled=true -f allowed_actions=selected
gh api -X PUT "repos/$REPO/actions/permissions/selected-actions" \
  -F github_owned_allowed=true -F verified_allowed=false \
  -f "patterns_allowed[]=pypa/gh-action-pypi-publish@*"
```

Settings > Actions > General > "Fork pull request workflows from outside collaborators": keep
**Require approval for first-time contributors** (the default) or stricter.

## 7. Branch protection for `main` (suggested)

A ruleset that keeps `main` from being deleted or force-pushed, sends changes through pull requests
with resolved conversations, and requires five CI checks (Linux 3.10 and 3.13, Windows 3.12, macOS 3.12,
and the package build). Repository admins can bypass it, so a solo maintainer can still push a hotfix.

```bash
gh api -X POST "repos/$REPO/rulesets" --input docs/maintainers/ruleset-main.json
```

- Apply it **after** CI has run once on `main`, so the check names exist.
- Rulesets and branch protection on a **private** repository owned by a personal account may need a paid
  plan (GitHub Pro). If the call answers with an upgrade message, apply it once the repository is public.
- The check names come from the job names in `.github/workflows/ci.yml`; if you rename a job, update
  `docs/maintainers/ruleset-main.json`.

## 8. Releases and PyPI (when ready)

1. Set the version in `clawback/__init__.py` (for example `2.0.0`), move the CHANGELOG "Unreleased"
   notes under that version, merge to `main`.
2. Tag and push: `git tag v2.0.0 && git push origin v2.0.0`. `release.yml` checks that the tag matches
   `clawback.__version__`, builds, runs `twine check` and the wheel smoke test, and attaches the sdist
   and wheel to a GitHub Release with generated notes, then publishes those same files to PyPI.
3. PyPI (set up 2026-11-01): pypi.org project `clawback` trusts owner `sainisourabh1-oss`, repository
   `clawback`, workflow `release.yml`, environment `pypi` (no API token anywhere). The `pypi` environment
   only accepts the `main` branch and `v*` tags. Releases and `v*` tags are immutable, so to publish an
   already-released version run the workflow by hand:
   `gh workflow run release.yml -R "$REPO" --ref main -f tag=v2.0.0` (it uploads the release's own files).

## 9. Check

```bash
gh repo view "$REPO" --json description,repositoryTopics,hasDiscussionsEnabled,hasWikiEnabled,visibility
gh api "repos/$REPO/actions/permissions/workflow"
gh api "repos/$REPO/rulesets" --jq '.[].name'
```

Then open the repository page and check: the banner switches between light and dark with the GitHub
theme, the CI badge is green after the first run, **New issue** offers only the two forms plus the two
links, and the community profile (Insights > Community standards) lists README, Code of Conduct,
Contributing, License, Security policy, Support, issue templates and pull request template.
