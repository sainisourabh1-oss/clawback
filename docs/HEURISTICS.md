# How Clawback decides (and why these numbers)

Clawback is a set of explainable heuristics, not a model. Every threshold
below is a deliberate choice with a reason, and every one has a test. When a
heuristic is unsure, Clawback says so in the UI instead of guessing.

## Paid subscriptions (`detect.py`, `aggregate.py`)

| Rule | Value | Why |
|---|---|---|
| A message is a candidate | amount in billing context **and** (billing subject **or** billing phrase in body), or billing subject **and** billing phrase | One signal alone is too weak: a price alone is marketing, "receipt" alone can be a shop order. Two independent signals cut false positives without needing a price (trial-ending notices often have none). |
| Bulk (List-Unsubscribe) mail | needs a billing **subject** | Marketing mail quotes prices and says "subscription". Real receipts that carry List-Unsubscribe still have billing subjects. |
| Newsletter footers | removed before matching | "Manage your subscription", "membership preferences", "unsubscribe" are footer words, not charges (review bug 6). |
| Word boundaries | every pattern | "industrial" must not match "trial" (review bug 4). |
| Billing cycle | words within ±80 characters of the price, or explicit phrases ("billed monthly", "annual plan") | A stray "month" or "Happy New Year" elsewhere must not set the cycle (review bug 5). 80 characters covers "Total: $9.99 (billed monthly)" and table layouts without reaching the next paragraph. |
| Cycle from receipt spacing | median gap: week 6–8 d, month 26–35 d, quarter 85–96 d, half-year 175–190 d, year 355–375 d | Covers 28–31-day months plus a few days of billing retries and time-zone drift; the windows never overlap. Shown as "from receipt spacing". Only charges count: trial countdown reminders are days apart but bill nothing, so they never give a "weekly" cycle. |
| One-off purchases | dropped | A single shop receipt with no recurring wording and no evenly spaced repeats is not a subscription. |
| Confidence | 0.2 + 0.25·subject + 0.1·body phrase (max 3) + 0.2·amount + 0.1·cycle, capped at 0.95 | Additive, so the UI can explain it; never 1.0 because email can lie. |
| Generic billers (Apple, Google Play, Stripe, Razorpay, Paddle, PayPal, …) | plan name from the body (`Plan:` / `Item:` lines or a known product name) | "Your receipt from Apple." names nothing; without this, iCloud+ and Apple Music merged into one row at the higher price (review bug 2). |
| Month abbreviations | stripped from subjects ("Oct", "Sept") | Otherwise each monthly receipt became its own subscription (review bug 1). |
| Price shown | the most recent one | A price rise appears in history and as an insight; taking the maximum hid decreases and mixed up plans. |
| Promotions | a promo subject ("Member benefit", "offer", "% off", "upgrade to") with no receipt subject is never a candidate | Real case: a Google One promo quoting ₹1,950/month and ₹10,000 store credit joined the ₹59 plan and set its price to ₹10,000. Prices come from receipts only. |
| No price **and** no billing signal | not a candidate; no price at all caps confidence at 0.4 ("check this") | Real case: Amazon "your nomination will renew" mails became two priceless "subscriptions". |
| Receipt kind | a receipt subject with a price is kind `receipt`, even if the body mentions trials or renewal | Google Play receipts carry boilerplate ("If you're in a free trial…") and were shown as "renewal,trial". |
| Tax line | "Includes GST/VAT/tax of X" is kept as a note next to the price; the Total line is preferred | Shows that ₹59.00 already includes ₹9.00 GST, so nobody adds it twice. |
| Plan names | runs of whitespace collapsed; "(by Developer)" removed | "Starter Business Email       Trial" vs "… Email Trial" split one trial into two rows. |
| Zero amounts | "$0.00", "Rs 0" are not prices | A bank's account notice mentioning AutoPay with an amount of 0.00 became an "Unknown merchant" mandate. |
| Rescans | rows the current rules no longer find are removed on the next completed scan, unless you reviewed them (another status, a note, your own dates) | An older version's mistakes must not linger as "suspected" after an upgrade. |
| Reminder countdowns | "ends in 7 days", "tomorrow", "today", "last chance" removed from the identity | One trial sends several countdown mails; they are one row. |

## Money (`money.py`)

* Currencies supported for parsing and display: the ten most-traded currencies
  in the BIS Triennial Central Bank Survey 2025 (USD, EUR, JPY, GBP, CNY, CHF,
  AUD, CAD, HKD, SGD), INR, and NZD, SEK, NOK, DKK, KRW, MXN, BRL, ZAR, PLN.
* Number formats: `1,234.56`, `1.234,56`, `1 234,56`, `1'234.56`, Indian
  grouping `1,23,456.00`, and no-decimal JPY/KRW. A separator followed by
  exactly 1–2 digits is the decimal point; 3 digits means thousands.
* Certainty labels: **explicit** (ISO code or an unambiguous symbol such as
  `€`, `₹`, `A$`, `CA$`, `HK$`, `S$`, `US$`), **inferred** (an ambiguous
  symbol resolved by the sender's country domain, an ISO code elsewhere in
  the email, or your region setting), **assumed** (bare `$` with nothing else
  to go on: shown as "currency assumed"), **ambiguous** (`¥` that cannot be
  told apart: kept as "¥ (JPY or CNY, unclear)" and never converted).
* Different currencies are **never added**. A combined total appears only if
  you switch it on and type your own dated rates.
* "Billing context" = words like total/amount/charged/paid within 60
  characters before the amount; "save $20", "discount", "cashback" right
  before an amount make it ineligible.

## Senders (`senders.py`)

* Grouped by exact sender address; the most recent message's
  List-Unsubscribe header is used (old links expire).
* Category order: **paid** (has a detected subscription) > **important**
  (transactional, see below; loses only to identity spoofing) > **spam-like**
  (score ≥ 3) > **newsletters & marketing** (List-Unsubscribe, List-Id,
  `Precedence: bulk/list`, a Gmail Promotions/Social/Forums tab, or a
  promotional subject; the Gmail Updates tab alone is not enough: Google
  describes it as confirmations, receipts, bills and statements, and since
  2024 Gmail and Yahoo require marketing bulk mail to carry List-Unsubscribe) >
  **other** (personal or account mail, never suggested).
* **Important / transactional** (`important.py`; a real mailbox test showed
  bank NEFT alerts and card statements as "spam" and GitHub, Aadhaar, NSE,
  broker and Google security mail as "newsletter"). Per message: government
  domain 3; transactional subject (OTP, security/sign-in alert, debited/
  credited/UPI/NEFT, account/card/demat statement, contract note, trades
  executed) 3; account notice subject (order placed/shipped/delivered, refund,
  payment failed/declined/unsuccessful, bill, account data shared with an
  app, access turned off or paused, sign-in requested, access/authentication
  code, email or identity verification, KYC, bank details changed, credit
  report/profile, portfolio statement, loan summary, policy copy, square-off /
  trading window notices, domain expiry, bounce) 3, not counted when the
  subject is promotional ("% off", "member benefit", ...); work notification (notifications@ on GitHub/GitLab/… or an
  `[owner/repo]` subject) 3; regulated finance domain (`.bank.in`, `.fin.in`,
  `.bank`) or market infrastructure 2; receipt/confirmation subject 2;
  account mailbox name (order-update@, failed-payments@, billing@,
  noreply-accounts@, accountalerts@, portfolio-update@, otp@ ...) 2;
  alerts@/statements@-style mailbox 1; no List-Unsubscribe 1; Promotions tab
  −2. Important at ≥ 3 on **any** message: wrongly keeping a bank address
  costs one extra tick, wrongly suggesting it hides fraud alerts. A regulated
  domain alone (2) is not enough, so bank marketing stays marketing.
  Identity signals (provider spam folder, DMARC fail, unsafe link, display-name
  spoof, brand in name) still make it spam-like. Never suggested, shown with
  "Keep"; every action needs "I understand, include it anyway" per sender.
  A List-Unsubscribe header never outweighs an account subject (3 alone is
  enough), so a security notice with a preferences link is still important.
  Mixed senders (mostly promotions, one payment-failure notice) are important:
  the "any message" rule above. A storage-full or welcome notice is not an
  account notice in this sense and does not move a promotional sender.
* "Not opened in N months" (the bulk "Select senders not opened" button) also
  leaves out any sender with an account signal in ANY stored subject or an
  account mailbox name (`important.account_mail_reason`), whatever its stored
  category; and after each scan, stored newsletter senders the scan did not
  read again are re-checked against the current rules (`scan.recheck_stored`).
* Spam-like means unsolicited or deceptive. Marketing from a company you have
  an account with is marketing.
* Spam-like score: strong = 3, medium = 2, weak = 1; threshold 3, so one
  strong signal is enough, two medium signals are enough, weak signals alone
  never are.
  * strong: your provider filed it as spam; DMARC fail; unsubscribe link is a
    raw IP, punycode, URL shortener or local host; display name shows a
    different address.
  * medium: DKIM/SPF fail without DMARC fail; unsubscribe link on an unrelated
    domain (see "Unsubscribe link trust" below); a brand in the display
    name that is not in the sending domain; no valid sender address.
  * weak: Reply-To on another domain; spam-style subject wording; bulk mail
    from a free webmail address.
* Organisation = registrable domain, using a curated subset of the Public
  Suffix List (`co.in`, `bank.in`, `gov.in`, `co.uk`, `gov.uk`, `com.au`, …).
  Without `bank.in`, every Indian bank collapsed into one organisation.
* Unsubscribe link trust, in order (a real mailbox test showed the old
  "differs from sender" warning firing on ordinary email providers):
  1. same organisation, including the same name under another suffix
     (`sbi.co.in` vs `sbi.bank.in`; names of 3+ letters, since 2-letter names
     collide too easily): nothing shown;
  2. a known email provider: an info note "unsubscribe is handled by
     <provider>". The list is `clawback/data/email_providers.json`, and every
     domain in it carries a dated source: the provider's documentation names
     it (`doc`), it is the provider's own website (`site`), or public DNS ties
     it to the provider (`dns`, with what was seen). Examples: SendGrid's
     unsubscribe host `emailinboundprocessing.com`/`.eu` (its MX is
     `mx.sendgrid.net`, the host SendGrid's Inbound Parse docs tell you to
     use), Mailchimp `mailchimpapp.net`, CleverTap `clevertap-prod.com`, Zoho
     Campaigns `maillist-manage.in`/`zcsend.in`, Netcore `ftrans03.com`.
     A domain we could not source (for example `list-unsubscribe.me`) is
     deliberately left off: rule 3 still covers it when it signed the email,
     otherwise it stays a warning. `tests/test_link_trust.py` fails if a
     domain has no source;
  3. a domain that passed DKIM or SPF on that email: info note. Only the
     topmost `Authentication-Results` header is read (it is added by your
     provider; lower ones can be written by the sender), with
     `ARC-Authentication-Results` as the fallback;
  4. anything else: one short warning per domain, "unsubscribe goes to
     <domain>, which isn't the sender (<sender>) or a known email provider"
     (medium spam signal; unsubscribe needs "I checked this sender"). A link
     and a `mailto:` on the same domain give one sentence and one signal:
     counting them twice used to push ordinary newsletters over the spam
     threshold (2 + 2 >= 3). The web UI shows the sentence as one line with
     the explanation folded underneath.
* Unsubscribe method order: RFC 8058 one-click (needs
  `List-Unsubscribe-Post: List-Unsubscribe=One-Click` **and** an HTTPS URI) >
  `mailto:` > web page.
* Emails per month = count ÷ observed span × 30.44, span floored at 30 days
  (3 emails in one afternoon is "3 a month", not "2,000 a month").

## Safety rules (`actions.py`, `oneclick.py`)

* Two steps, always: a plan (contacts nothing) and a separate confirmation
  quoting the plan id, its SHA-256 digest and the plan's count (the web
  app's Yes button names the action and the count; the CLI asks one yes/no
  question, default No). Nothing is typed. Plans expire after 15 minutes
  and run once.
* Never unsubscribe from spam-like senders (it confirms your address is
  read); offer Block / Report spam instead.
* A link on a domain unrelated to the sender is excluded until you tick
  "I checked this sender".
* One-click POST: HTTPS only, public IP addresses only, no cookies,
  Authorization or Referer, 15 s timeout, at most 3 redirects and only to
  HTTPS URLs that pass the same checks. No automatic retries.
* An unsubscribe is never shown as undoable: the sender has the request and
  Clawback cannot take it back. The log says "to resubscribe, use the
  sender's site". Archive, Trash, Spam and filters keep their undo.
* No List-Unsubscribe header: not a dead end. Clawback looks for an
  unsubscribe link in the email body ("Link in email", `extract.find_unsubscribe_link`):
  an anchor whose text (or image alt/title) says unsubscribe / opt out /
  manage, update or configure (email, notification, subscription)
  preferences / stop these emails / no longer wish to receive, or the same
  in Spanish, French, German, Portuguese, Italian, Dutch, Polish, Russian,
  Hindi ("सदस्यता समाप्त"), Japanese, Chinese, Korean, Turkish, Indonesian;
  else the link nearest those words (within 400 characters of the HTML
  source); else a link whose path says unsubscribe. Hidden text (style,
  script, `display:none`, HTML comments) never counts, and only http(s)
  links are taken. The link gets the same trust check as a header link
  (an `http:` one is blocked). It is never fetched: you open it in your
  browser after the two-click confirmation. Bodies are only read for this
  where the scan did not read them already (Gmail API: one `format=full`
  request per sender, paced like a scan; IMAP: one `BODY.PEEK[]`; Outlook:
  one `$value`), when you tick a row or press "Look in latest email", and in
  the background after a scan for newsletters still showing "None". The
  result is saved per sender, so each is read once. Otherwise Clawback
  offers the provider's own email-settings page when it knows one
  (`data/email_settings.json`, each entry dated) or Block.
* Company targets: acting on `pinterest.com` covers every sending address
  under that registrable domain (discover@, recommendations@ …); the UI
  groups them as one company.

## Did it work? (`proof.py`)

* Grace: 2 days for one-click (Google's sender FAQ recommends fulfilling
  unsubscribe requests within 48 hours); 10 business days otherwise
  (CAN-SPAM, 15 U.S.C. 7704(a)(4)(A)). Weekends are skipped; public holidays
  are not, because they differ by country.
* "Honoured" needs evidence: mail observed past the grace end and none from
  that sender. Otherwise "Waiting".

## Insights (`insights.py`)

* "Not opened in N months" (default 3): at least **3** messages in the window,
  none opened. Three is the smallest count that shows a habit rather than one
  missed email.
* Renewal/trial reminders: 14 days ahead; dates you typed win over dates read
  from email.

## India mandate alerts (`regions/india.py`)

* Needs mandate wording (UPI AutoPay, e-mandate, standing instruction,
  e-NACH or a UMRN, pre-debit) **and** a merchant name, plus an INR amount or
  a known bank/UPI sending domain. Generic "set up AutoPay" marketing has
  neither.
* One subscription per **merchant**, across all bank alerts, even when the
  bank changes its sending domain (Indian banks moved to `*.bank.in`); alerts
  without an amount join that merchant's priced row.
* VPAs, email addresses and URLs are removed before looking for mandate
  wording, and a merchant can never start with "VPA". Real case: a one-off
  "Money sent! Rs.… to VPA autopay.<payment gateway>@…" became a "VPA
  autopay" mandate.
* A mandate's "Maximum transaction amount" is a cap, not the price, and a
  registration's "Current transaction amount: 0.00" charges nothing (NACH:
  "the mandate has a value of Rs. X" is a cap too; "At MNTH frequency" is
  monthly): such an
  alert gives the merchant with no price. Real case: a card e-mandate
  registration became a row named after the bank at the cap amount.
* Merchant names end at "/" too ("towards Example Finance Ltd/12345678 with
  UMRN …").
* Loans: loan/EMI wording, or a merchant name containing bank, finance,
  finserv, capital, credit, fincorp, NBFC or lending, gives kind `loan` and
  status `loan`. Loans are
  listed under "Loans & EMIs (not cancellable here)" and never count in
  "you are paying" or savings. Real case: a monthly loan EMI from one bank
  to another lender.
