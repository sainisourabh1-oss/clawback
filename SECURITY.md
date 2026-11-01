# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 2.x (latest release and `main`) | Yes |
| 1.x and earlier | No: please upgrade |

## Reporting a vulnerability

Please report security problems **privately** through GitHub's private vulnerability reporting:
open the repository's **Security** tab and choose **Report a vulnerability**
([direct link](https://github.com/sainisourabh1-oss/clawback/security/advisories/new)). Only the
maintainers can see the report.

Please do **not** open a public issue, discussion or pull request for a vulnerability, and never post OAuth
tokens, app passwords, client secrets, mailbox contents or your `~/.clawback` folder anywhere. Redact them
from logs and screenshots.

Helpful to include: the Clawback version (`clawback version`), your operating system and Python version,
the connector involved (Takeout/mbox import, Gmail, IMAP, Outlook), steps to reproduce, and what an
attacker could achieve.

Clawback is maintained by volunteers. There is no bug bounty and no guaranteed response time; we
acknowledge reports as soon as we can and credit reporters in the advisory unless they prefer otherwise.

## In scope

- **Tokens and credentials.** OAuth tokens (stored under `~/.clawback/tokens/`), the OAuth client file
  you provide (`client_secret.json`), and app passwords (held in memory only and never written to disk).
  For example: a token written with loose file permissions, a token or password reaching a log, an export,
  an error message or the browser.
- **The local data folder `~/.clawback/`** (or the folder named by `CLAWBACK_HOME`). The SQLite database, activity log, exports and settings. For
  example: message bodies being stored when only metadata should be, or a crafted email that causes a write
  outside the data folder.
- **The local web server.** It must listen on `127.0.0.1` only, accept only `127.0.0.1` / `localhost`
  host names (DNS-rebinding protection), require the per-launch token on every state-changing request,
  refuse cross-origin requests and serve a strict Content-Security-Policy. Any way for a web page, another
  site or another device to read data from it or trigger an action is in scope.
- **The double confirmation.** Any way to run an unsubscribe, send, archive, trash, block, report-spam or
  deletion request without the user's two-click confirmation for that exact plan, or to change the plan after
  it was confirmed.
- **Outbound requests.** One-click unsubscribe POSTs must go only to public HTTPS addresses, with no cookies
  or credentials and at most three safe redirects (SSRF to local or private addresses is in scope), and
  links on unrelated domains must stay excluded until the user ticks "I checked this sender".
- **Malicious email content.** HTML, headers, attachments or Takeout/mbox files that lead to script
  execution in the web app, crashes that lose data, or misleading output.

## Out of scope

- Problems that need an attacker who already controls your computer or user account.
- Exposing the web app to a network on purpose (reverse proxy, port forwarding, changing the bind address).
- Vulnerabilities in Google, Microsoft or mail-provider services themselves; report those to the provider.
- Whether a third-party cancellation page or unsubscribe link is trustworthy. Clawback checks the address,
  not the site behind it; review pages before you act on them.

## Safe use

- Scanning is read-only. Revoke Clawback's access at any time from your Google account
  ([myaccount.google.com/permissions](https://myaccount.google.com/permissions)) or Microsoft account.
- Keep `~/.clawback/` private; it can contain tokens, a local database and notes. Deleting the folder
  removes everything Clawback stored.
- Keep the web app on `127.0.0.1`. Do not publish it through a reverse proxy or a public interface.
- An app password can read and send all your mail until you revoke it. Revoke it when you are done.
