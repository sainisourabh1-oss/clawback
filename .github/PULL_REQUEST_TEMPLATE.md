## What this changes

<!-- What changes for the user, and why. Link the issue if there is one: "Fixes #123". -->

## How it was tested

<!-- Commands you ran and what you checked by hand (web app, CLI, which connector). -->

## Checklist

- [ ] `python -m pytest -q` passes locally.
- [ ] New or changed behaviour has tests, including cases that must **not** match or must be refused.
- [ ] Fixtures, screenshots and examples are synthetic: `.example` domains, invented names and amounts.
      No real emails, addresses, tokens, client files or mailbox data.
- [ ] **Safety rule kept:** nothing unsubscribes, sends, archives, trashes, filters, blocks, reports spam,
      deletes or cancels without a plan the user has seen **and** a separate confirm step (two clicks, or
      the CLI's yes/no that defaults to No). No new default-yes, one-click action, auto mode or flag that
      skips it. Delete stays move-to-Trash only.
- [ ] Still local-only: no telemetry, no remote fonts/scripts/CDNs, no new outbound requests except the ones
      the user confirms or their own mail provider.
- [ ] Wider permissions (OAuth scopes, Graph permissions), if any, are asked for only when the user confirms
      an action that needs them, and the README permissions tables are updated.
- [ ] New thresholds or detection rules are explained in `docs/HEURISTICS.md`; regional steps and links name
      their source and the date checked.
- [ ] Connector labels (Verified / Tested with fakes / Experimental) are unchanged, or the change is backed by
      a real-service run described above.
- [ ] User-visible changes have a line under "Unreleased" in `CHANGELOG.md`.
- [ ] If the UI changed: keyboard access, light and dark theme and 390 px width checked
      (`python scripts/ui_states.py . <folder>`), and screenshots regenerated if the README shows that screen.
