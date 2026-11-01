# Contributing to Clawback

Thanks for helping. Clawback touches people's mailboxes and money, so the bar is "safe and honest first,
clever second". Please read the [hard rules](#hard-rules) before you open a pull request.

## Development setup

You need Python 3.10 or newer and Git.

```bash
git clone https://github.com/sainisourabh1-oss/clawback && cd clawback
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -e ".[gmail,test]"
```

Check it works:

```bash
python -m pytest -q                  # the whole suite: synthetic fixtures only, no network, no real mail
clawback scan --demo                 # the built-in fictional mailbox
clawback ui --no-browser             # serves the web app on 127.0.0.1 and prints the address (Ctrl+C to stop)
```

### Smoke tests in `scripts/`

These drive the real app and need a few extra packages. The two UI scripts use the installed
Microsoft Edge through Playwright.

```bash
pip install playwright build pillow
python scripts/ui_smoke.py . <folder>     # end-to-end UI walk with real local HTTPS one-click POSTs; saves screenshots
python scripts/ui_states.py . <folder>    # empty mailbox, paused/failed/stopped scans, long names, many currencies
python scripts/wheel_smoke.py             # builds sdist + wheel, installs the wheel in a fresh venv, serves the app
python scripts/bench_scan.py --messages 3000   # simulated scan-speed benchmark (Gmail quota rules, virtual clock)
python scripts/render_brand.py            # regenerates docs/brand/ (see docs/brand/README.md)
```

Use a scratch folder for `<folder>`. Only regenerate `docs/screenshots/` (`python scripts/ui_smoke.py .
docs/screenshots`) when your change alters what the app looks like, and commit the new images with it.

CI runs `pytest` on Linux, Windows and macOS with Python 3.10 to 3.13, builds the sdist and wheel, runs
`twine check` and `scripts/wheel_smoke.py`. Please run at least `python -m pytest -q` before you push.

## Hard rules

A pull request that breaks one of these will not be merged, however useful it is otherwise.

1. **Double confirmation for every action.** Nothing that unsubscribes, sends, archives, trashes, labels,
   filters, reports spam, blocks, deletes or cancels may run unless the user has (1) seen the plan for
   exactly those senders and (2) confirmed it with a separate, explicit second step (the web app's in-place
   Yes button naming the action and count, or the CLI's yes/no question that defaults to No). Do not add a
   default-yes, a "select all and go", an auto mode, a one-click action, a flag or setting that skips the
   second step, or a code path that runs an action without a confirmed plan. Delete means move to Trash,
   never a permanent delete. New actions go through the existing plan / digest / count / expiry checks in
   `actions.py`, with tests that show a wrong or missing count is refused.
2. **Read-only by default.** Scanning never changes the mailbox. Ask for wider permissions (OAuth scopes,
   Graph permissions) only at the moment the user confirms an action that needs them.
3. **Local only.** No telemetry, analytics, crash reporting, remote fonts, CDNs or calls to AI services.
   The web app must keep loading nothing from other servers, and the server must stay bound to
   `127.0.0.1`.
4. **No personal data in the repository.** Test fixtures, screenshots and examples are synthetic: senders
   use reserved domains such as `.example`, people and amounts are invented. Never commit real emails,
   addresses, tokens, OAuth client files, Takeout exports or mailbox databases.
5. **Honest labels and numbers.** Amounts are estimates and say so; currencies are never added together
   unless the user typed their own rates. Connector labels (Verified, Tested with fakes, Experimental) only
   move up with evidence: "Verified" means it ran against the real service. Cancellation links and revoke
   steps name their source and the date they were checked.
6. **Spam-like senders are never unsubscribed from.** Offer Block or Report spam instead.

## Coding style

- Python 3.10 syntax: the package must import on 3.10 (for example, no quotes of the same kind nested inside
  an f-string, which needs 3.12). CI tests 3.10 to 3.13.
- Standard library first. A new runtime dependency needs a good reason in the pull request.
- Follow the style of the file you are editing: type hints on public functions, `from __future__ import
  annotations`, short functions, docstrings that explain *why*.
- Heuristics are explainable: every threshold has a reason, written in
  [docs/HEURISTICS.md](docs/HEURISTICS.md) next to the rule.
- Web UI: plain HTML, CSS and JavaScript in `clawback/web/static/`, no build step and no external
  assets. Keep keyboard access, visible focus, reduced-motion support, WCAG AA contrast in light and dark,
  and a 390 px phone width working (`scripts/ui_states.py` checks these).
- User-facing text: plain words, say what will happen, never promise more than the code does.

## Tests

- Every bug fix comes with a test that fails before the fix.
- Every detection change comes with synthetic messages that should and should not match (false positives
  matter as much as misses).
- Tests must not touch the network or a real mailbox. Use the fakes in `tests/fakes.py` and
  `tests/fake_microsoft.py`, and the local HTTPS server pattern in the existing one-click tests.

## Adding a regional module

Regional modules live in `clawback/regions/`. Each one can recognise local recurring-payment notices
(bank mandate alerts, direct-debit notices), carry sourced steps to revoke a mandate, data-deletion letter
templates and verified cancellation pages.

1. Copy `clawback/regions/_template.py` to `clawback/regions/<code>.py` and fill in `detect()` and the
   `REGION` object (`base.py` documents the fields).
2. Add the module name to `MODULES` in `clawback/regions/__init__.py`.
3. Every legal citation and every revoke step names its source (official page). A `RevokeGuide` gets
   `verified=True`, a `source` and a `checked_on` date only when you checked the steps against that page;
   otherwise leave `verified=False` and the app labels the steps "general guidance".
4. Add tests in `tests/test_regions.py` (or a new file) with synthetic alerts modelled on the public format:
   a few that must match (merchant, amount, cycle) and look-alikes that must not (marketing that mentions
   "AutoPay", one-off payments).
5. Document the rules and their thresholds in `docs/HEURISTICS.md`.

## Adding or changing a heuristic

Detection lives mainly in `detect.py`, `aggregate.py`, `money.py`, `senders.py` and `prefilter.py`. Write
the reason for every number you add, prefer "unsure, please check" over a confident wrong answer, and
add both positive and negative fixtures.

## Pull requests

- Keep each pull request to one topic, with a description of what changes for the user.
- Fill in the checklist in the pull request template.
- Add a line to `CHANGELOG.md` under "Unreleased" for anything a user would notice.
- By contributing you agree that your contribution is licensed under the project's [MIT licence](LICENSE)
  and that you follow the [Code of Conduct](CODE_OF_CONDUCT.md).

Security problems: please do not open an issue; see [SECURITY.md](SECURITY.md).
