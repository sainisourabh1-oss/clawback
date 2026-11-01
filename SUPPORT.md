# Getting help with Clawback

Clawback is a volunteer-run open-source project. There is no paid support, but here is the quickest way to
an answer.

1. **Check the docs first.** The [README](README.md) covers installing, each way to connect (and how well each
   is tested), the double confirmation, privacy and an FAQ. [docs/HEURISTICS.md](docs/HEURISTICS.md)
   explains how senders, subscriptions and amounts are detected.
2. **Try the demo.** `clawback scan --demo` (or *Try the demo* in the app) shows how everything works on a
   fictional mailbox, without touching your mail.
3. **Questions and ideas:** start a thread in
   [Discussions](https://github.com/sainisourabh1-oss/clawback/discussions).
4. **Something is broken:** open a [bug report](https://github.com/sainisourabh1-oss/clawback/issues/new/choose).
   Include `clawback version`, your operating system, Python version and the connector you used.
5. **Security problem:** do not open an issue. Follow [SECURITY.md](SECURITY.md).

## Before you share anything

Never paste OAuth tokens, app passwords, `client_secret.json`, email contents, real email addresses or
files from your `~/.clawback` folder into an issue or discussion. Replace names and addresses with made-up
ones (for example `news@shop.example`) and blur screenshots.

## What we cannot help with

- Cancelling a subscription or getting a refund: that happens with the provider. Clawback opens the
  cancellation page or shows the steps; it cannot act for you.
- Problems with your Google, Microsoft or mail-provider account (sign-in, app passwords, quotas): those
  providers' help pages are linked in the README.
- Legal advice about data-deletion requests. The DPDP/GDPR/CCPA letters are drafts, not legal advice.
