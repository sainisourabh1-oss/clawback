# Changelog

## 2.0.0 – 2026-11-01

Clawback 2.0 (formerly "unsubscribe"): a local web app and CLI for bulk unsubscribe and delete with a
two-click double confirmation, a paid-subscription finder with savings per currency, and proof that
unsubscribes worked. The data folder moves to `~/.clawback` (v1 `~/.unsubscribe` data is copied once).

### No pop-ups: two clicks in place, a calmer screen

This replaces the confirm sheet described in "Simpler cleanup after a scan" below.

- After ticking senders or pressing an action nothing opens: no dialog, sheet or overlay. Click 1 turns the
  row's (or the selection bar's) buttons, in the same place, into **Yes, delete 120 emails** and **Cancel**,
  with one or two plain lines underneath and the per-sender detail folded. Only that Yes acts. Focus starts
  on Cancel, so Enter alone never acts; Escape, a click elsewhere or 8 seconds untouched put the buttons
  back (an open "What exactly happens" keeps it while it is read). The server still checks the plan's
  count, digest, expiry and single use.
- Scan options, settings and results are inline too. An old read-only Google or Microsoft sign-in is the
  one place that asks again: the row says "Needs permission" with **Allow cleanup**, or the notice's **Sign
  in again to allow cleanup**; one consent, then the same row comes back with its Yes.
- Compact layout: the five steps sit in the header, a one-line summary card, folded heads-up, counts in one
  column, glass panels. A soft moving colour background is painted into a tiny canvas 8 times a second
  (measured in headless Edge, which composites in software: 9 to 13 % of one core for the whole browser,
  against about 40 % for the CSS-animated version it replaces), stops while the tab is hidden and is a
  still picture under reduced motion. Text on the translucent panels is checked for WCAG AA over the brightest point of each colour
  blob, in light and dark.
- Honest limits up front: the connect screen says that file imports and app-password (IMAP) connections
  can scan and unsubscribe but not delete (deleting over IMAP is not implemented), and the results say it
  again in one visible line. README has a table of what each connection can do.
- Phones: the header is opaque (nothing scrolls through it), the selection bar keeps its count and close
  button on one line, and the account picker never covers the app name.
- Privacy: test fixtures modelled on a real mailbox now use invented amounts and a different bank.

### Simpler cleanup after a scan

This replaces the typed confirmation phrase described in the entries below.

- One row per sender, most emails first, with its count and three buttons: **Unsubscribe**, **Delete all**,
  **Keep**; the row's menu adds **Unsubscribe + delete all**, Block and Archive. Spam-like senders get
  Delete all and Block, never Unsubscribe. A search box, a sort (most emails, least opened, most recent,
  name), "All" and "Select the ones you haven't opened" sit above the list; ticking several opens a bar
  ("3 selected · 161 emails") with the same actions.
- No typing. Click 1 opens a confirm sheet in plain words ("They go to Gmail Trash; you can restore them
  for 30 days", "This can't be undone from Clawback" for unsubscribing); click 2 is its button, which names
  the action and the count ("Yes, delete 120 emails"). Focus starts on Cancel, so Enter alone never acts.
  The server checks that count with the plan's digest, expiry and single use. One tick on the sheet turns
  Unsubscribe into Unsubscribe + delete all.
- Delete all moves mail to Trash (Outlook: Deleted Items), never a permanent delete. Afterwards a toast
  offers **Undo**; a result sheet (also with Undo) appears only when a page must be finished or something
  failed. Rows show "120 deleted"; undo clears it. A sender with no safe way to unsubscribe is only deleted.
- CLI: one yes/no question naming the action and count, default No; no answer does nothing. New
  `clawback delete` and `clawback unsubscribe-delete`; `--yes` is accepted and ignored.
- Permissions in one go: a Google sign-in asks for `gmail.modify` and `gmail.settings.basic` together
  (never `https://mail.google.com/`, which can delete for good); Microsoft asks for Mail.Read,
  Mail.ReadWrite, Mail.Send and MailboxSettings.ReadWrite together. The connect cards list what each is for
  before the provider's page opens. A read-only sign-in saved by an older version still scans and asks
  once ("Sign in again to allow cleanup"), never again after. File imports and app-password connections
  show no Delete, with one line saying why.
- A calmer results page: a one-line summary replaces the count tiles, heads-up reminders fold into one line,
  and Important mail, Did it work?, Privacy and History (was Activity) move under **More**. The savings card
  shows only once there is something saved. Works at 390 px (the sheet rises from the bottom), 1920 px, dark,
  by keyboard and with reduced motion.

### Fixes from the first manual test on a real mailbox

- Every sender row has its own main action next to Keep: Unsubscribe for newsletters, Block for
  spam-like senders, "Cancel page" (or "How to stop" for a bank/UPI mandate) for paid subscriptions,
  plus a small "more" menu (archive, trash, block, report). Each opens the same review and typed
  confirmation for that one sender; the tick-and-bar flow for many senders stays. Works by keyboard
  (Enter opens, arrows move, Escape closes) and on a phone.
- Calmer link-trust notes. Known email providers now come from `data/email_providers.json`, where every
  domain has a dated source (provider docs, its own site, or public DNS); added SendGrid's
  `emailinboundprocessing.com`/`.eu`, Mailchimp, CleverTap, Zoho Campaigns, Mailjet, Netcore, Intercom,
  Adobe Campaign and others, and dropped domains no source could be found for. A link and a mailto on the
  same unknown domain are now one short warning (and one spam signal, so they no longer push ordinary
  newsletters into Spam); the explanation is folded under the row.
- "How: None" no longer means a dead end when the email itself has an unsubscribe link: Clawback finds the
  link in the body (multilingual, including Hindi), shows it as "Link in email" with its domain and a Copy
  button, applies the same trust check, and opens it in your browser only after the review and typed
  confirmation (never fetched by Clawback). Bodies are read read-only, one recent email per sender, when you
  tick or open the row and in the background after a scan (Gmail API paced like a scan, IMAP, Outlook);
  mbox imports and the demo find it during the scan.
- A refused sign-in no longer looks like a finished, empty scan. IMAP and Gmail app-password connections
  sign in once before the new account replaces the one on screen; a new account whose first scan fails
  (and that has no saved results) is dropped and you are taken back to the account you were looking at,
  with an alert naming the failed one. An account without results says "Not scanned yet" with a Scan now
  button instead of a row of zeros, and every connected account is listed in `/api/state`.
- Accounts this computer already knows are offered again after a restart: the Connect screen lists them
  (and the account picker has a "Saved accounts" group), each with one-click Open. An account with a saved
  Google or Microsoft sign-in reconnects with it; one with results only (an app-password account, whose
  password is never saved, or an import) opens its saved results, and "Connect again to scan" replaces a scan
  that could not work. The Gmail label field suggests a label that is not taken and warns, in plain words,
  when what you type would sign in a new, empty account while a saved Gmail sign-in exists.
- Errors read as sentences. The plain reason and the fix come first, with web addresses such as the
  app-password page as real links; what the mail server said, or the raw exception, sits under a folded
  "Technical details". A refused connect shows under its own button instead of in a toast that disappears,
  and an unexpected failure says so in words instead of showing a Python exception name.
- Account mail is no longer offered for unsubscribing. Order and delivery updates, failed or declined
  payments, bills, "you shared account data with an app", access turned off, sign-in requests, verification
  codes, credit reports, portfolio statements and broker trading notices were filed under Newsletters &
  marketing (Gmail's Updates tab counted as "bulk"), and "Select senders not opened" could tick them. They are
  now Important (a List-Unsubscribe header no longer outweighs such a subject; a mostly-promotional sender that
  ever sent a payment-failure or security notice is Important too). The Updates tab alone no longer makes a
  sender a newsletter. "Not opened" never includes a sender with any account signal, and senders stored by an
  older version are re-checked after every scan. Classifier version 7: the next scan reads the window again.
- A more polished look for the web app (styles only; every action, wording and the typed confirmation are
  unchanged). What you saved sits on a deep teal "You've clawed back" card in both themes; the count tiles
  have their own colours and open their tab; tabs are pills; each row's own action is a soft teal button so
  the selection bar stays the loud one; selected rows get a teal edge; text fields show a teal focus halo,
  as does the confirmation box once the phrase matches. A pixel-dot grid (from the pixel logo) sits behind
  the connect art. All colours still pass WCAG AA in light and dark (checked by the tests); still no
  request to any other server.
- You choose what each scan reads. *Scan again* (and the first scan of a Gmail, IMAP or Outlook mailbox)
  offers a quick update (recommended: only mail not read yet), a fresh full scan of the last 12 months,
  everything since a date you pick, or only your newest N emails, each with a time estimate worked out
  from what earlier scans saved. The CLI has the same choices: `--full`, `--since`, and the new
  `--last N` (not combined with `--since` or `--limit`). "The newest N" now means the newest N on every
  source and every scan: IMAP ranks all folders by arrival date and counts mail already read (a second
  "newest 5" used to read the next 5 older ones), an imported export picks the newest by date rather than
  the first N in the file, and Outlook counts mail already read. A date in the future is refused; one
  before 1971 means all mail. After an update that sorts mail better, the picker and the running scan
  say why this one scan reads everything again. The results say what the last scan covered ("your newest
  500 emails"), and whether it was stopped early, and *Try again* after a failed scan repeats the same
  choice.

### Brand and app icons

- New pixel-art envelope mark: app logo, favicon (SVG and a multi-size `favicon.ico`), the shareable
  savings card, pixel-art welcome and "Cleanup done" art that respects reduced motion, and a light and
  dark README banner, feature strip, social preview and flow animation rendered from the demo data
  (`scripts/render_brand.py`).

### Repository and release

- Community files: contributing guide, code of conduct, security policy, support page, `CITATION.cff`,
  issue and pull-request templates, Dependabot.
- CI tests Python 3.10 to 3.13 on Linux, Windows and macOS, byte-compiles the package on each version,
  builds the sdist and wheel with `twine check`, installs the wheel in a fresh venv and audits dependencies.
  A tag workflow (`release.yml`) checks the tag matches `clawback.__version__`, builds, smoke-tests the
  wheel and creates the GitHub release; publishing to PyPI stays off.
- README rewritten: themed banner and feature strip, a quick start, connector
  test labels, Gmail scope classes, a comparison, FAQ and roadmap. `docs/maintainers/REPO_SETUP.md`
  lists the repository settings. The old `docs/REVIEW.md` and `docs/ROADMAP.md` are gone (the roadmap is
  in the README).

### Connectors

- IMAP presets for Yahoo, iCloud, AOL, Zoho (personal and own-domain), Fastmail, GMX, Yandex and Proton
  Mail Bridge, each value cited with the provider's page and the date it was read
  (`clawback/data/providers.json`). Typing an address picks the preset; server details sit under
  "Technical details". `clawback scan --user you@provider` (`--provider`, `--port`, `--security`, `--cafile`).
- STARTTLS as well as SSL/TLS, certificates always checked (an extra certificate can be trusted for Bridge).
  Plain messages for a wrong server name, nothing listening, TLS or certificate failures, and refused
  sign-ins (with the provider's own app-password page).
- Folders are chosen from the server's special-use flags (RFC 6154), so non-English folder names work;
  iCloud's short user name is tried first. SMTP uses the preset's port and STARTTLS where needed.
- Outlook.com says plainly that passwords stopped working on 16 Sep 2024 and offers
  **Sign in with Microsoft** (Experimental): Microsoft Graph, PKCE public client with your own app
  registration, Mail.Read by default, write permissions only when an action needs them.
- Honest labels everywhere (app, CLI `clawback connectors`, README): Verified, Tested with fakes, Experimental.
- **All accounts** view: results of every scanned account together; the same Gmail through the API and IMAP
  counts once (matched by address and Gmail message ids); savings per currency; actions split per mailbox
  and confirmed with one typed phrase, each part through the usual checks. `clawback senders --account all`.
- Switching between mailboxes keeps each one's "Last scan ... emails from ... senders" line (it came only from
  the scan that ran last).

### Detection fixes from a real read-only Gmail rescan

- Rows an older version detected that the current rules no longer find are removed on the next completed scan
  (unless you reviewed them: another status, a note or your own dates). Before, an upgrade left the old mistakes
  listed as "suspected".
- Trial countdown reminders ("ends in 7 days", "ends tomorrow") no longer get a weekly billing cycle from their
  spacing.
- A zero amount ("$0.00") is never a price, so a bank account notice that mentions AutoPay is not a mandate.
- India: a card e-mandate registration names its merchant ("Your payment for X is registered") and its
  "Maximum transaction amount" is no longer taken as the price; NACH debits with a UMRN are recognised, and a
  merchant name ends at "/" (so a lender with a reference number lands in Loans & EMIs).
- One row per mandate merchant even when the bank changes its sending domain; a NACH acceptance mail names the
  lender ("Issued to …"), its "value of" is a cap, and "MNTH/QURT/YRLY" frequency codes are read.

### Scans always say why they stopped

- A scan is recorded when it starts and completed when it ends, with a heartbeat while it runs. An error raised
  anywhere in the scan (also after reading) is stored with it.
- A scan whose process was closed or killed (it cannot report anything itself) is recognised at the next start:
  `clawback scan` prints what happened, and the web app shows it in the failed-scan alert with *Try again*.
- `clawback scan` prints a plain "Scan failed: …" reason for any unexpected error instead of ending without one.
- Python 3.10 and 3.11: fixed an f-string that only Python 3.12 could read (the package did not import there).

### Web app redesign

- New look for the local web app, built for people who are not technical: a welcome screen that says
  what Clawback will and will not do, cards for each way to connect, summary tiles that open their tab,
  sender avatars, an unopened-share meter, and friendly empty states.
- The double confirmation is easier to see: the action bar says nothing happens before review and a
  typed confirmation; the review dialog shows its two steps, and a live line says when the typed phrase
  matches (Confirm stays disabled until then).
- Savings show one figure per currency, side by side; never added together.
- Dates read the same everywhere ("27 Oct 2026" in the reader's own locale order) and never split across
  lines; a calendar date is shown as the same day in every time zone.
- The review plan is in plain words ("Ask Daily Deals to unsubscribe you automatically"), still naming what
  will be contacted; the protocol detail (HTTPS POST, RFC 8058, full link) sits under "Technical details".
  The CLI prints the same detail dimmed.
- On a phone the tab row fades at the edge that has more tabs, snaps to tabs, and keeps the selected tab in view.
- Scan screen: percentage, progress bar, Paused state with Resume, Stop. A failed scan shows its error
  at the top of the results with "Try again" and "Use another mailbox"; a stopped scan says its results
  may be incomplete.
- Bundled Inter font (SIL Open Font License) and Clawback's own SVG icons and illustrations, served by the
  local server; the page still loads nothing from other servers.
- Accessibility: WCAG AA contrast checked for the text and control colour pairs in light and dark, visible focus outline,
  Home/End and arrow keys on tabs, reduced-motion setting respected, no horizontal scroll at 390 px.
- `tests/test_ui_quality.py` checks contrast, that every string and colour token exists, that bundled
  assets are served and shipped, that the page requests nothing from other servers, and server response time.
- New `scripts/ui_states.py` drives edge cases in a real browser (empty mailbox, scan running/paused/failed,
  very long names, many currencies, dark, phone width); `scripts/ui_smoke.py` now fails on console errors
  or requests to other servers; `scripts/wheel_smoke.py` checks the font and icons ship in the wheel.

## 1.0.2

- Added `--query` to the Gmail scan command so users can explicitly choose the Gmail search window/filter.
- Made generic receipt subjects with different parsed prices separate candidates instead of silently merging them. Price changes in generic-subject emails may still split into separate candidates; review them manually.
- Hardened CSV exports against spreadsheet formula injection in text fields.
- HTML-only message extraction now ignores script, style, and noscript sections to reduce hidden-content false positives.
- Tightened cached OAuth token file permissions where supported and added retries for Gmail API requests; messages deleted between listing and retrieval are skipped.
- Added regression tests for the above changes.

## 1.0.1

- Fixed subscription identity so distinct product/plan subjects and billing cycles are not automatically merged by domain and currency alone.
- Added a safe SQLite schema migration for the new subscription identity key while preserving price history and evidence.
- Removed restrictive Gmail subject-only server-side filtering; messages are now screened by the local classifier after body extraction, with spam/trash excluded by default.
- Added regression tests for multiple plans from one provider, changing monthly receipt dates, billing-cycle separation, and Gmail query construction.

## 1.0.0

- Added local SQLite subscription tracker, review statuses, notes, trial/renewal dates, reminders and price history.
- Added local read-only dashboard and duplicate-price candidates report.
- Improved Gmail body extraction and pagination.
- Added INR parsing and separated currencies in cost summaries.
- Hardened cancellation-link lookup and local file permissions.
- Added regression tests, CI, security policy and updated documentation.
