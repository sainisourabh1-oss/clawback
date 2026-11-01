---
name: clawback
description: Help the user clean up their inbox and find paid subscriptions locally with Clawback (web app or CLI). Scans are read-only; every unsubscribe, block, archive, trash or cancel needs the user's own two-step confirmation.
---

# clawback

Clawback runs on the user's computer. It reads mail (read-only), groups bulk senders, finds paid
subscriptions and shows savings per currency. Results are heuristic and must be checked by the user.

## Commands

- `clawback` or `clawback ui`: open the local web app on 127.0.0.1 (easiest for most people).
  `--no-browser` prints the address instead; `--port` picks the port.
- `clawback scan --demo`: scan the built-in fictional mailbox (nothing real is touched).
- `clawback scan --import takeout.zip`: scan a Google Takeout export, `.mbox` file or folder of `.eml` files.
- `clawback scan --gmail`: Google sign-in, one consent for scanning and cleanup (`gmail.modify` +
  `gmail.settings.basic`; the scan only reads). Needs the `gmail` extra and the user's own OAuth client.
- `clawback scan --gmail-fast --user you@gmail.com`: Gmail over IMAP with an app password the user types
  (scan and unsubscribe only; deleting over IMAP is not implemented).
- `clawback scan --user you@yahoo.com`: IMAP with a provider preset (Yahoo, iCloud, AOL, Zoho, Fastmail, GMX,
  Yandex, Proton Bridge); `--host` for any other server. Prompts for an app password.
- `clawback scan --outlook --user you@outlook.com --client-id <id>`: Outlook.com / Microsoft 365 (Experimental).
- What a scan reads (any source): by default a quick update (only new mail of the last 12 months);
  `--full` reads the window again; `--since 3m` / `--since 2025-03-01` / `--since all` picks the window;
  `--last 500` reads only the newest 500 emails (not with `--since` or `--limit`).
- `clawback connectors`: ways to connect with their labels (Verified, Tested with fakes, Experimental).
- `clawback senders --account all`: every scanned account together (a mailbox connected twice counts once).
- `clawback senders`, `clawback list`, `clawback insights`, `clawback savings`, `clawback proof`: read-only reports.
- `clawback unsubscribe <address> --dry-run`: show the plan without contacting anyone.
- `clawback unsubscribe | delete | unsubscribe-delete | archive | block | report-spam | escalate | cancel | undo ...`:
  print a plan, then ask one yes/no question naming the action and the count (default No). Nothing skips it;
  `--yes` is accepted and ignored. `delete` moves mail to Trash (restorable), never deletes for good.

## The web app

Five steps are shown at the top: Connect, Scan, Clean up, Confirm, Savings. Each sender is a row with its
email count and Unsubscribe, Delete all, Keep (Unsubscribe + delete all in its menu); ticking several opens a
bar with the same actions. There are no pop-ups: a button only turns, in the same place, into a Yes that names
the action and the count ("Yes, delete 37 emails") plus Cancel (click 1); only that Yes runs it (click 2).
Focus starts on Cancel. A toast offers Undo afterwards. Google and Microsoft sign-ins ask for every permission
in one go; a read-only sign-in from an older version asks once to sign in again. Deleting, archiving and
blocking need a Google or Microsoft sign-in: file imports and app-password (IMAP) connections can unsubscribe
but not delete (IMAP delete is not implemented). Outlook is Experimental. Savings are listed per
currency and never added together. White and black themes, keyboard navigation and phone width are supported.

## How to help

1. Scan only after the user explicitly authorizes mailbox access. Prefer `--demo` to show how it works.
2. Present senders, prices and cycles as estimates to verify, never as facts.
3. Never unsubscribe, block, archive, trash, send or cancel on the user's behalf. The user chooses the
   senders and presses the confirm button themselves; do not press it for them or try to bypass it.
4. Cancelling a paid plan happens on the provider's page, opened by Clawback; never claim a cancellation
   succeeded without confirmation from the provider.
5. Show money per currency; never sum different currencies.
   Say how well a connector is tested (its label) instead of promising it works with a provider.
6. Never ask for a normal email password (app passwords only, typed by the user), expose tokens, or send
   mailbox content to third parties.
