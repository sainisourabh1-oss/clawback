"""Clawback command line. Running ``clawback`` with no arguments opens the local web app.

Every mailbox/outgoing action uses the same two steps as the web app:
1) you name the senders and the action on the command line; Clawback prints
   exactly what will happen; 2) you answer one plain question that names the
   action and the count ("Move 90 emails from 1 sender to Trash? [y/N]"),
   which defaults to No. Nothing skips it (``--yes`` is accepted for old
   scripts and ignored).
"""
from __future__ import annotations

import json
import sys
import webbrowser
from getpass import getpass
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__, actions, config, insights, links as links_mod, privacy, proof, providers as providers_mod, savings, tracker
from . import saved as saved_mod
from .actions import PlanError
from .money import format_money
from .sources.base import AuthError, OfflineSource, SourceError

app = typer.Typer(name="clawback", help="Clawback: claw back your inbox and your money. Local, privacy-first email cleanup.",
                  invoke_without_command=True, no_args_is_help=False, add_completion=False)
console = Console()


@app.callback()
def _default(ctx: typer.Context):
    """With no command, open the local web app."""
    if ctx.invoked_subcommand is None:
        from .web.server import serve
        serve()


# ------------------------------------------------------------------ helpers
def _label(account: str | None) -> str | None:
    """--account takes the account's label or its email address."""
    if not account or "@" not in account:
        return account
    from . import saved
    return saved.label_for_address(account) or account


def _name(account: str) -> str:
    """What to call an account on screen: its email address where known."""
    from . import saved
    return saved.display_names(set(tracker.scanned_accounts()) | {account}).get(account, account)


def _last_account() -> tuple[str, str] | None:
    db = tracker.connect()
    try:
        row = db.execute("SELECT account, source FROM scans WHERE error IS NULL AND finished_at IS NOT NULL "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        db.close()
    return (row["account"], row["source"]) if row else None


def _kind_of(account: str) -> str:
    db = tracker.connect()
    try:
        row = db.execute("SELECT source FROM scans WHERE account=? ORDER BY id DESC LIMIT 1", (account,)).fetchone()
    finally:
        db.close()
    return row["source"] if row else "offline"


def _action_source(account: str | None, host: str | None = None, user: str | None = None, allow_send: bool = False):
    if not account:
        last = _last_account()
        if not last:
            console.print("[red]No scanned account yet. Run `clawback scan ...` first.[/red]")
            raise typer.Exit(1)
        account = last[0]
    kind = _kind_of(account)
    if kind == "gmail":
        from .sources.gmail import GmailSource
        return GmailSource(account=account)
    if kind == "demo":
        from .demo import DemoSource
        return DemoSource(account)
    if kind == "outlook":
        from .sources.outlook import OutlookSource
        return OutlookSource(account, on_auth_url=_print_auth_url)
    if kind == "imap" and host and user:
        from .sources.imap import ImapSource
        return ImapSource(host, user, getpass("IMAP app password: "), account=account, allow_send=allow_send)
    return OfflineSource(account)


def _fail(exc: Exception, gmail: bool | None = None) -> None:
    """Print the error. The Gmail sign-in hint is shown for Gmail API errors
    only (``gmail`` True, or unknown and the error is not about IMAP)."""
    console.print(f"[red]{escape(str(exc))}[/red]")
    imap_error = "app password" in str(exc) or "refused the sign-in" in str(exc)
    if gmail or (gmail is None and isinstance(exc, AuthError) and not imap_error):
        console.print("[dim]Gmail: run `clawback scan --gmail` again to sign in. No Google Cloud project? "
                      "Use `clawback scan --import takeout.zip` instead.[/dim]")
    raise typer.Exit(2)


def _print_plan(plan) -> None:
    console.print(f"\n[bold]Review: {plan.action} - {len(plan.runnable)} of {len(plan.steps)} can run[/bold]"
                  + ("  [yellow](DRY RUN: nothing will be contacted)[/yellow]" if plan.dry_run else ""))
    for s in plan.steps:
        head = f"[bold]{s.label}[/bold] <{s.target}>" if s.label != s.target else f"[bold]{s.target}[/bold]"
        console.print(f"\n  {head}  method={s.method}" + (f"  contacts={s.contact}" if s.contact else "")
                      + (f"  emails={s.emails_affected}" if s.emails_affected else ""))
        for e in s.effects:
            console.print(f"    - {e}")
        for d in s.details:
            console.print(f"      [dim]{d}[/dim]")
        for w in s.warnings:
            console.print(f"    [yellow]! {w}[/yellow]")
        for n in s.notes:
            console.print(f"    [dim]i {n}[/dim]")
        if s.permission:
            console.print(f"    [yellow]needs permission: {s.permission}[/yellow]")
        if s.blocked:
            console.print(f"    [red]WILL NOT RUN: {s.blocked}[/red]")


def run_two_step(source, action: str, targets: list[str], options: dict, dry_run: bool, yes: bool) -> dict | None:
    """Step 1: build and print the plan. Step 2: one yes/no question naming the action and count. Always both."""
    try:
        plan = actions.build_plan(source, action, targets, options, dry_run=dry_run)
    except PlanError as exc:
        console.print(f"[red]{exc}[/red]"); raise typer.Exit(2)
    _print_plan(plan)
    if not plan.runnable:
        console.print("\n[yellow]Nothing in this selection can run.[/yellow]")
        return None
    perms = sorted({s.permission for s in plan.runnable if s.permission})
    if perms and not dry_run:
        console.print("\n[yellow]Grant the missing permission first (opens Google's consent page):[/yellow]")
        for p in perms:
            console.print(f"  - {p}")
        if not typer.confirm("Open the consent page now?", default=False):
            raise typer.Exit(1)
        for cap in {"unsubscribe": ["mailto"], "unsubscribe_trash": ["mailto", "trash"], "archive": ["archive"], "trash": ["trash"], "block": ["block"],
                    "report_spam": ["spam"], "escalate": ["block", "spam"], "send_request": ["send"]}.get(action, []):
            if source.missing_permission(cap):
                source.grant(cap)
        for opt, cap in (("also_archive", "archive"), ("also_trash", "trash"), ("also_block", "block")):
            if options.get(opt) and source.missing_permission(cap):
                source.grant(cap)
        plan = actions.build_plan(source, action, targets, options, dry_run=dry_run)
        _print_plan(plan)
    try:
        ok = typer.confirm("\n" + actions.question(plan), default=False)
    except typer.Abort:      # end of input (a script with nothing to answer) = No
        ok = False
    if not ok:
        actions.cancel_plan(plan.id)
        console.print("Not confirmed. Nothing was done.")
        raise typer.Exit(1)
    try:
        result = actions.confirm(source, plan.id, plan.count, plan.digest)
    except PlanError as exc:
        console.print(f"[red]Not confirmed: {exc}. Nothing was done.[/red]")
        raise typer.Exit(1)
    for r in result["results"]:
        mark = "[green]ok[/green]" if r["ok"] else "[red]failed[/red]"
        console.print(f"  {mark}  {r['label']}: {r.get('detail', '')}")
        if r.get("open"):
            console.print(f"       open: {r['open']}")
    console.print(f"\n{result['ok']} done, {result['failed']} failed. Log: `clawback log`.")
    return result


# ------------------------------------------------------------------ app / scanning
@app.command()
def ui(port: int = typer.Option(8765, min=0, max=65535, help="Local port (127.0.0.1 only)"),
       no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser window")):
    """Open the local web app (the easiest way to use Clawback)."""
    from .web.server import serve
    serve(port, open_browser=not no_browser)


@app.command()
def scan(
    gmail: bool = typer.Option(False, "--gmail", help="Sign in with Google: one consent for scanning and cleanup (gmail.modify + "
                               "gmail.settings.basic); the scan only reads. 10 to 15 min the first time for 3,000 "
                               "emails (Google limits API reads per minute); later scans take seconds"),
    gmail_fast: bool = typer.Option(False, "--gmail-fast", help="Fast scan (about a minute): Gmail over IMAP with a "
                                    "Google app password (needs 2-Step Verification; the app password can read and send "
                                    "all your mail until you revoke it). Use with --user"),
    xoauth2: bool = typer.Option(False, "--xoauth2", help="With --gmail-fast: sign in with Google instead of an app "
                                 "password (FULL mailbox permission; needs --accept-full-access)"),
    accept_full_access: bool = typer.Option(False, "--accept-full-access", help="Confirm you read the --xoauth2 warning"),
    outlook: bool = typer.Option(False, "--outlook", help="EXPERIMENTAL: Outlook.com / Hotmail / Microsoft 365 through "
                                 "'Sign in with Microsoft' (one consent for Mail.Read, Mail.ReadWrite, Mail.Send and "
                                 "MailboxSettings.ReadWrite; the scan only reads). Use with --user; the first time also "
                                 "--client-id (your own app registration, see README)"),
    client_id: str = typer.Option(None, "--client-id", help="With --outlook: Application (client) ID of your app registration"),
    tenant: str = typer.Option(None, "--tenant", help="With --outlook: common (default), consumers or organizations"),
    import_path: Path = typer.Option(None, "--import", help="Google Takeout .zip/.tgz, .mbox file, or folder of .eml files"),
    demo: bool = typer.Option(False, "--demo", help="Scan the built-in fictional demo mailbox"),
    provider: str = typer.Option(None, "--provider", help="IMAP preset: yahoo, icloud, aol, zoho, zoho_pro, fastmail, "
                                 "gmx, yandex, proton (Bridge) or other. Usually found from --user; see 'clawback connectors'"),
    host: str = typer.Option(None, help="IMAP host (overrides the preset), e.g. imap.mail.yahoo.com"),
    port: int = typer.Option(None, min=1, max=65535, help="IMAP port (overrides the preset)"),
    security: str = typer.Option(None, help="ssl or starttls (overrides the preset; certificates are always checked)"),
    cafile: Path = typer.Option(None, "--cafile", help="Extra certificate to trust, e.g. the one Proton Mail Bridge exports"),
    user: str = typer.Option(None, help="IMAP user / email address"),
    folders: str = typer.Option(None, help="IMAP folders, comma-separated (default: found from the server's own "
                                "folder list, e.g. Gmail's All Mail in any language, or INBOX plus Archive)"),
    account: str = typer.Option(None, help="With --gmail: which Gmail account (its address, or a saved label). "
                                "Default: your one saved Gmail sign-in, else a new sign-in named by its address"),
    since: str = typer.Option(None, "--since", help="Scan back to: 1m, 3m, 6m, 1y, 2y, all, or a date YYYY-MM-DD "
                              "(default: 1y)"),
    query: str = typer.Option("", "--query", help="Extra Gmail search terms (combined with --since)"),
    limit: int = typer.Option(None, min=1, help="Optional safety cap: read at most the newest N emails of the "
                              "--since window (default: no cap)"),
    last: int = typer.Option(None, "--last", min=1, max=1_000_000, help="Scan only your newest N emails, however "
                             "old (instead of a --since window); emails read before are reused"),
    full: bool = typer.Option(False, "--full", help="Fresh full scan: read every message of the window again "
                              "instead of only new ones"),
    export: Path = typer.Option(None, "--export", help="Also write detected subscriptions to this CSV"),
):
    """Read your mail (read-only), find paid subscriptions and bulk senders."""
    from .scan import DEFAULT_WINDOW, parse_since
    links_mod.load_user_db()
    if last is not None and (since is not None or limit is not None):
        console.print("[red]--last N already says what to read: leave out --since and --limit.[/red]")
        raise typer.Exit(2)
    try:
        window = None if last is not None else parse_since(DEFAULT_WINDOW if since is None else since)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    mode = "last" if last is not None else "full" if full else "quick" if since is None else "since"
    if last is not None:
        limit = last
    _report_abandoned()
    try:
        if demo:
            from .demo import DemoSource
            src = DemoSource()
        elif import_path:
            from .sources.mbox import MboxSource
            src = MboxSource(import_path)
        elif outlook:
            from . import ms_oauth
            from .sources.outlook import OutlookSource
            if not user:
                console.print("[red]Add --user you@outlook.com[/red]")
                raise typer.Exit(2)
            saved = ms_oauth.load_client() or {}
            cid = client_id or saved.get("client_id")
            if not cid:
                console.print("[red]First time: add --client-id (README: Outlook.com explains the 5-minute app "
                              "registration).[/red]")
                raise typer.Exit(2)
            if not ms_oauth.valid_client_id(cid):
                console.print("[red]The client ID looks like 00000000-0000-0000-0000-000000000000.[/red]")
                raise typer.Exit(2)
            t = tenant or saved.get("tenant") or "common"
            ms_oauth.save_client(cid, t)
            console.print("[dim]Outlook (Microsoft Graph) is EXPERIMENTAL: tested with fakes only.[/dim]")
            src = OutlookSource(user, cid, t, on_auth_url=_print_auth_url)
            src.connect()
        elif gmail:
            src = _gmail_source(account, query)
        elif gmail_fast:
            from .sources.imap import ImapSource
            if not user:
                console.print("--gmail-fast needs --user you@gmail.com")
                raise typer.Exit(2)
            if xoauth2:
                from .sources.gmail import FULL_MAIL_WARNING, imap_oauth_token
                console.print(f"[yellow]{FULL_MAIL_WARNING}[/yellow]", markup=True)
                if not accept_full_access:
                    console.print("Add --accept-full-access to continue, or drop --xoauth2 to use an app password.")
                    raise typer.Exit(2)
                src = ImapSource(GMAIL_IMAP_HOST, user, None, account=saved_mod.account_label(user),
                                 oauth_token=imap_oauth_token(user, on_auth_url=_print_auth_url))
            else:
                console.print(f"[dim]{APP_PASSWORD_HELP}[/dim]")
                src = ImapSource(GMAIL_IMAP_HOST, user, getpass("Google app password (16 letters): ").replace(" ", ""),
                                 account=saved_mod.account_label(user))
        elif user and (host or provider or providers_mod.for_address(user)):
            from .sources.imap import ImapSource
            try:
                st = providers_mod.imap_settings(provider, user, host, port, security)
            except providers_mod.PresetError as exc:
                console.print(f"[red]{exc}[/red]")
                if provider == "outlook" or (providers_mod.for_address(user) or {}).get("id") == "outlook":
                    console.print("Use: clawback scan --outlook --user " + user + " --client-id <your app's client ID>")
                raise typer.Exit(2)
            p = st["provider"] or {}
            label = providers_mod.label_text(providers_mod.label_of(st["connector"]))
            console.print(f"[dim]{p.get('name') or st['host']}: {st['host']}:{st['port']} ({st['security']}). "
                          f"Label: {label}.[/dim]")
            if p.get("enable_first"):
                console.print(f"[dim]{p['enable_first']}[/dim]")
            if (p.get("app_password") or {}).get("url"):
                console.print(f"[dim]App password: {p['app_password']['url']}[/dim]")
            chosen = [f.strip() for f in folders.split(",") if f.strip()] if folders else None
            prompt = "Bridge password (Bridge > Mailbox details): " if p.get("auth") == "bridge" else "App password: "
            src = ImapSource(st["host"], user, getpass(prompt), chosen, account=saved_mod.account_label(user), port=st["port"],
                             security=st["security"], cafile=str(cafile) if cafile else None, provider=p or None,
                             users=st["users"])
        else:
            console.print("Choose a source: --import takeout.zip (easiest), --gmail, --user you@provider (IMAP preset), "
                          "--host/--user (any IMAP) or --demo")
            raise typer.Exit(1)
        from .scan import (HINT_FIRST_API, HINT_NEXT, HINT_TEXT, HINT_UPGRADE, HINT_UPGRADE_API, reads_everything,
                           speed_hint, upgrade_reread)
        full_read = reads_everything(src.kind, src.account, not full)
        upgrade = full_read and not full and upgrade_reread(src.kind, src.account)
        running = speed_hint(src.kind, "running", full_read, upgrade=upgrade)
        if running in {HINT_FIRST_API, HINT_UPGRADE_API, HINT_UPGRADE}:
            console.print(f"[dim]{HINT_TEXT[running]}[/dim]")
        result = _scan_with_progress(src, limit, window, not full, mode=mode, last=last)
    except (SourceError, AuthError) as exc:
        _fail(exc, gmail=False if (gmail_fast or host) else (True if gmail else None))
    except OSError as exc:
        console.print(f"[red]Scan failed: {escape(str(exc))}[/red]")
        if host or gmail_fast:
            console.print("[dim]IMAP: check the host, use an app password, and make sure IMAP is enabled.[/dim]")
        console.print(f"[dim]{SCAN_FAILED_NOTE}[/dim]")
        raise typer.Exit(2)
    except (typer.Exit, typer.Abort, KeyboardInterrupt):
        raise
    except Exception as exc:  # noqa: BLE001 - never end a scan without saying why
        console.print(f"[red]Scan failed: {escape(type(exc).__name__)}: {escape(str(exc)) or 'no details'}[/red]")
        console.print(f"[dim]{SCAN_FAILED_NOTE}[/dim]")
        raise typer.Exit(2)
    window_text = (f"of your newest {last:,}" if last is not None
                   else f"back to {window.date().isoformat()}" if window else "all time")
    console.print(f"Read {result.messages:,} emails ({window_text}) from {result.senders:,} senders"
                  + (f", {result.reused:,} already read by an earlier scan" if result.reused else "")
                  + (f" ({result.skipped} spam/trash/sent skipped)" if result.skipped else "")
                  + f". Account: [bold]{escape(_name(result.account))}[/bold]")
    if result.bodies is not None:
        console.print(f"[dim]Full text was read for {result.bodies:,} possible receipts/billing emails; headers only for the rest.[/dim]")
    if result.partial:
        console.print("[yellow]Scan cancelled: the results below are PARTIAL (everything read so far was kept; "
                      "scan again to continue).[/yellow]")
    if result.capped and last is None:
        console.print(f"[yellow]Stopped at the --limit of {limit:,} emails before reaching {window_text}: "
                      "older mail in the window was NOT read.[/yellow]")
    console.print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(result.categories.items())))
    hint = speed_hint(src.kind, "done", full_read, {"partial": result.partial, "capped": result.capped,
                                                    "reused": result.reused})
    if hint == HINT_NEXT:   # the rescan counts are already in the "Read ..." line above
        console.print(f"[dim]{HINT_TEXT[hint]}[/dim]")
    _subs_table(tracker.list_subscriptions(account=result.account))
    if export:
        from .export import to_csv
        to_csv(result.subscriptions, export)
        console.print(f"[green]Exported to {export}[/green]")
    console.print("[dim]Next: `clawback senders`, `clawback unsubscribe ADDRESS`, or `clawback` for the web app.[/dim]")


def _gmail_source(account: str | None, query: str):
    """The Gmail API source for --gmail. A saved account (by label or
    address) reconnects with its own sign-in; with no --account the one
    saved Gmail sign-in is used. Otherwise Google sign-in runs and the account
    is named by the address Google reports - or, when that address is already
    saved here, its existing account is reopened instead of a new one."""
    from . import saved
    from .sources.gmail import GmailSource
    gmail_saved = [r for r in saved.saved_accounts(False) if r["kind"] == "gmail"]
    label = _label(account)
    if label is None and len(gmail_saved) == 1:
        label = gmail_saved[0]["account"]
    elif label is None and len(gmail_saved) > 1:
        names = saved.display_names({r["account"] for r in gmail_saved})
        console.print("[red]More than one Gmail account is saved here. Choose one with --account:[/red]")
        for r in gmail_saved:
            console.print(f"  --account {escape(r['address'] or r['account'])}   ({escape(names[r['account']])})")
        console.print("[dim]Or --account new to sign in to another Gmail account.[/dim]")
        raise typer.Exit(2)
    if label and label in {r["account"] for r in gmail_saved}:
        src = GmailSource(account=label, query=query, on_auth_url=_print_auth_url)
        src.service()
        return src
    src = GmailSource(account="new-sign-in", query=query, token_file=saved.pending_token_path(),
                      on_auth_url=_print_auth_url)
    src.service()
    label = saved.adopt_gmail_signin(src)
    if account and "@" in account and saved.label_for_address(account) != label and account.lower() != label:
        console.print(f"[yellow]You signed in as {escape(_name(label))}, not {escape(account)}: scanning "
                      f"{escape(_name(label))}.[/yellow]")
    return src


GMAIL_IMAP_HOST = "imap.gmail.com"
APP_PASSWORD_HELP = ("Create an app password at https://myaccount.google.com/apppasswords (needs 2-Step Verification). "
                     "It can read and send all your mail until you revoke it there; Clawback only reads.")


def _print_auth_url(url: str) -> None:
    console.print(f"\nIf your browser didn't open, visit: {url}\n", soft_wrap=True, markup=False, highlight=False)


SCAN_FAILED_NOTE = ("Nothing in your mailbox was changed. Emails read before the failure are kept; "
                    "run the same scan again to continue.")


def _report_abandoned() -> None:
    """A scan whose process was closed or killed could not say why; say it now."""
    for r in tracker.close_abandoned():
        console.print(f"[yellow]The previous scan of {escape(r['account'])} (started {escape(r['started_at'])}, "
                      f"{r['messages']:,} emails read) did not finish.[/yellow]")
        console.print(f"[dim]{escape(tracker.ABANDONED_TEXT)} Nothing in your mailbox was changed.[/dim]")


def _scan_with_progress(src, limit, since, incremental, mode=None, last=None):
    """Run the scan in a worker thread with a live progress bar. Ctrl+C
    cancels and keeps what was read (the result is marked partial)."""
    import threading
    from rich.progress import BarColumn, Progress, TextColumn
    from .progress import ScanControl, describe
    from .scan import run_scan
    control = ScanControl()
    box: dict = {}
    latest: dict = {}

    def work():
        try:
            box["result"] = run_scan(src, limit=limit, progress=latest.update, cancel=control, since=since,
                                     incremental=incremental, mode=mode, last=last)
        except BaseException as exc:  # re-raised in the main thread
            box["error"] = exc

    worker = threading.Thread(target=work, daemon=True)
    with Progress(TextColumn("[bold]Scanning[/bold] (read-only)"), BarColumn(), TextColumn("{task.description}"),
                  console=console, transient=True) as bar:
        task = bar.add_task("connecting…", total=None)
        worker.start()
        while worker.is_alive():
            try:
                worker.join(0.2)
            except KeyboardInterrupt:
                control.cancel()
                bar.update(task, description="cancelling, keeping what was read…")
                continue
            if latest:
                snap = dict(latest)
                text = describe(snap) + (f" · {snap['senders']:,} senders" if snap.get("senders") else "")
                bar.update(task, total=snap["total"] or None, completed=snap["done"], description=text)
    if "error" in box:
        raise box["error"]
    return box["result"]


def _subs_table(rows) -> None:
    rows = [r for r in rows if r["status"] != "dismissed"]
    loans = [r for r in rows if r["status"] == "loan"]
    rows = [r for r in rows if r["status"] != "loan"]
    if loans:
        console.print("Loans & EMIs (not cancellable here; not counted as subscriptions or savings):")
        for r in loans:
            price = format_money(r["amount"], r["currency"]) if r["amount"] is not None else "?"
            console.print(f"  #{r['id']} {r['vendor']}  {price}/{r['cycle']}")
    if not rows:
        console.print("[yellow]No paid subscriptions detected.[/yellow] (Detection is heuristic; check your statements.)")
        return
    t = Table(title="Paid subscriptions (estimates)")
    for c in ("ID", "Vendor", "Price", "Cycle", "Per month", "Status", "Evidence"):
        t.add_column(c)
    for r in rows:
        price = format_money(r["amount"], r["currency"]) if r["amount"] is not None else "?"
        if r["currency_certainty"] in {"assumed", "ambiguous"}:
            price += f" ({r['currency_certainty']})"
        if r["tax_note"]:
            price += f" ({r['tax_note']})"
        monthly = format_money(r["monthly_cost"], r["currency"]) if r["monthly_cost"] is not None else "?"
        t.add_row(str(r["id"]), r["vendor"], price, f"{r['cycle']} ({r['cycle_source']})" if r["cycle"] != "unknown" else "unknown",
                  monthly, r["status"], f"{r['hits']} email(s)")
    console.print(t)


@app.command()
def senders(category: str = typer.Option(None, help="newsletter, spam, important, paid or other"),
            account: str = typer.Option(None, help="Account (email address or label), or 'all' for every scanned account together "
                                        "(a mailbox connected twice is counted once)"),
            top: int = typer.Option(50, min=1)):
    """List bulk senders with counts, last received, unopened share and unsubscribe method."""
    from .senders import best_method
    every = (account or "").lower() == "all"
    if every:
        from . import combined
        rows = combined.senders(category)
    else:
        rows = tracker.list_senders(_label(account) or (_last_account() or ("default",))[0], category)
    rows = [r for r in rows if category or r["category"] in {"newsletter", "spam"}][:top]
    t = Table(title="Senders (all accounts)" if every else "Senders")
    if every:
        from . import saved
        names = saved.display_names({a for r in rows for a in r["accounts"]})
    for c in ("Sender", "Category", "Emails", "Last", "Unopened", "Method", "Status", "Warnings") + (("Accounts",) if every else ()):
        t.add_column(c)
    for r in rows:
        m = best_method(type("S", (), {"list_unsubscribe": r["unsubscribe"], "one_click": bool(r["one_click"]),
                                     "body_unsub": r.get("body_unsub") or ""}))
        unread = f"{round(r['unread_count'] / r['read_known'] * 100)}%" if r["read_known"] else "?"
        t.add_row(r["address"], r["category"], str(r["message_count"]), (r["last_seen"] or "")[:10], unread,
                  m[0] if m else "none", "kept" if r["kept"] else r["status"], "; ".join(r["flags"][:2]),
                  *((", ".join(names.get(a, a) for a in r["accounts"]),) if every else ()))
    console.print(t)


@app.command()
def connectors(sources: bool = typer.Option(False, "--sources", help="Also show the official page behind each preset")):
    """Ways to connect, each with an honest label, and the IMAP presets."""
    t = Table(title="Ways to connect")
    for c in ("Connector", "Label", "What the label means"):
        t.add_column(c)
    for c in providers_mod.connectors():
        lab = providers_mod.data()["labels"][c["label"]]
        t.add_row(c["name"], lab["text"], lab["detail"])
    console.print(t)
    p = Table(title="IMAP presets (clawback scan --user you@provider; --provider to choose)")
    for c in ("Preset", "IMAP", "SMTP", "Sign-in", "Checked"):
        p.add_column(c)
    for pr in providers_mod.providers():
        if "imap" not in pr:
            continue
        i, sm = pr["imap"], pr.get("smtp") or {}
        auth = {"oauth_only": "passwords switched off: use --outlook", "bridge": "Bridge password",
                "app_password": "app password"}.get(pr["auth"], "password or app password")
        checked = min(src["checked_on"] for src in pr["sources"])
        p.add_row(f"{pr['id']} ({pr['name']})", f"{i['host']}:{i['port']} {i['security']}",
                  f"{sm.get('host', '')}:{sm.get('port', '')} {sm.get('security', '')}", auth, checked)
    console.print(p)
    if sources:
        for pr in providers_mod.providers():
            for src in pr.get("sources", []):
                console.print(f"[bold]{pr['id']}[/bold] {escape(src['url'])} ({src['checked_on']}): {escape(src['says'])}",
                              markup=True, highlight=False, soft_wrap=True)


# ------------------------------------------------------------------ actions (two steps)
def _act(action, targets, account, options, dry_run, yes, host=None, user=None, allow_send=False):
    src = _action_source(_label(account), host, user, allow_send)
    try:
        run_two_step(src, action, targets, options, dry_run, yes)
    except (SourceError, AuthError) as exc:
        _fail(exc)


_yes = typer.Option(False, "--yes", "-y", hidden=True, help="Accepted for old scripts; never skips the final question")
_dry = typer.Option(False, "--dry-run", help="Show and log what would happen; contact nothing")
_acct = typer.Option(None, "--account", help="Account: its email address or label (default: last scanned)")
_imp = typer.Option([], "--include-important", help="Include this sender even though it looks important "
                    "(bank/card alerts, statements, security, government, work mail). Repeat per sender.")


@app.command()
def unsubscribe(addresses: list[str], account: str = _acct, also_archive: bool = typer.Option(False, help="Also archive their old mail (Gmail)"),
                also_trash: bool = typer.Option(False, help="Also move their old mail to Trash (Gmail)"),
                also_block: bool = typer.Option(False, help="Also filter their future mail out of the inbox (Gmail)"),
                ack: list[str] = typer.Option([], "--ack", help="Include a sender whose link domain differs (after checking it)"),
                include_important: list[str] = _imp, dry_run: bool = _dry, yes: bool = _yes, host: str = typer.Option(None, help="IMAP host (to send mailto unsubscribes)"),
                user: str = typer.Option(None, help="IMAP user"), allow_send: bool = typer.Option(False, help="IMAP: allow SMTP sending")):
    """Unsubscribe from senders: one-click POST, else email, else open the page for you."""
    _act("unsubscribe", addresses, account, {"also_archive": also_archive, "also_trash": also_trash, "also_block": also_block,
                                             "acknowledged": ack, "acknowledged_important": include_important},
         dry_run, yes, host, user, allow_send)


@app.command("unsubscribe-delete")
def unsubscribe_delete(addresses: list[str], account: str = _acct,
                       ack: list[str] = typer.Option([], "--ack", help="Include a sender whose link domain differs (after checking it)"),
                       include_important: list[str] = _imp, dry_run: bool = _dry, yes: bool = _yes):
    """Unsubscribe AND move all their mail to Trash, in one go (Gmail/Outlook sign-in; emails can be restored).
    A sender with no safe way to unsubscribe is only deleted."""
    _act("unsubscribe_trash", addresses, account, {"acknowledged": ack, "acknowledged_important": include_important},
         dry_run, yes)


@app.command()
def archive(addresses: list[str], account: str = _acct, include_important: list[str] = _imp, dry_run: bool = _dry,
            yes: bool = _yes):
    """Archive all known mail from these senders (Gmail; undo available)."""
    _act("archive", addresses, account, {"acknowledged_important": include_important}, dry_run, yes)


@app.command("delete")
@app.command()
def trash(addresses: list[str], account: str = _acct, include_important: list[str] = _imp, dry_run: bool = _dry,
          yes: bool = _yes):
    """Delete all known mail from these senders: moves it to Trash, never deletes for good (Gmail/Outlook; undo available)."""
    _act("trash", addresses, account, {"acknowledged_important": include_important}, dry_run, yes)


@app.command()
def block(addresses: list[str], account: str = _acct, mode: str = typer.Option("archive", help="archive or trash future mail"),
          include_important: list[str] = _imp, dry_run: bool = _dry, yes: bool = _yes):
    """Create a Gmail filter for future mail from these senders (undo deletes it)."""
    _act("block", addresses, account, {"block_mode": mode, "acknowledged_important": include_important}, dry_run, yes)


@app.command("report-spam")
def report_spam(addresses: list[str], account: str = _acct, include_important: list[str] = _imp, dry_run: bool = _dry,
                yes: bool = _yes):
    """Report these senders' mail as spam (Gmail; undo available)."""
    _act("report_spam", addresses, account, {"acknowledged_important": include_important}, dry_run, yes)


@app.command()
def escalate(addresses: list[str], account: str = _acct, include_important: list[str] = _imp, dry_run: bool = _dry,
             yes: bool = _yes):
    """For senders that ignored your unsubscribe: filter to Trash + report spam."""
    _act("escalate", addresses, account, {"acknowledged_important": include_important}, dry_run, yes)


@app.command()
def undo(entry_ids: list[int], account: str = _acct, yes: bool = _yes):
    """Undo logged actions (restore from Trash/Archive/Spam, delete a filter)."""
    _act("undo", [str(i) for i in entry_ids], account, {}, False, yes)


@app.command()
def cancel(subscription_ids: list[int], account: str = _acct, yes: bool = _yes):
    """Open the verified cancellation page for tracked subscriptions (you cancel there)."""
    _act("open_cancel", [str(i) for i in subscription_ids], account, {}, False, yes)


# ------------------------------------------------------------------ tracker
@app.command("list")
def list_tracked(account: str = _acct):
    """List tracked paid subscriptions."""
    _subs_table(tracker.list_subscriptions(account=_label(account)))
    console.print("Mark one: clawback review ID confirmed|dismissed|cancelled|suspected")


@app.command()
def review(identifier: int, status: str = typer.Argument(..., help="confirmed, dismissed, cancelled or suspected")):
    """Record what a detected subscription really is (cancelled ones count toward savings)."""
    try:
        tracker.set_status(identifier, status)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]"); raise typer.Exit(2)
    console.print(f"[green]Subscription {identifier} marked {status}.[/green]")


@app.command()
def dates(identifier: int, trial_end: str = typer.Option(None, help="YYYY-MM-DD"), renewal_date: str = typer.Option(None, help="YYYY-MM-DD"),
          notes: str = typer.Option(None, help="Private note")):
    """Set trial/renewal dates and a private note."""
    try:
        tracker.update_dates(identifier, trial_end, renewal_date, notes)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]"); raise typer.Exit(2)
    console.print("[green]Tracker updated.[/green]")


@app.command()
def reminders(days: int = typer.Option(14, min=0)):
    """Upcoming trial ends and renewals (entered or detected in email)."""
    rows = insights.upcoming(None, days=days)
    if not rows:
        console.print(f"Nothing due in the next {days} day(s)."); return
    for r in rows:
        console.print(f"{r['date']}  {r['vendor']} {r['kind']} in {r['days']} day(s)  ({r['source']})")


@app.command()
def history(identifier: int):
    """Recorded price changes for a subscription."""
    rows = tracker.price_history(identifier)
    if not rows:
        console.print("No price history recorded yet."); return
    for r in rows:
        console.print(f"{r['observed_on']}  {format_money(r['amount'], r['currency'])}  ({r['source']})")


def insights_cmd(account: str = _acct):
    """Price increases, renewals soon and newsletters you never open."""
    acct = _label(account) or (_last_account() or ("default",))[0]
    data = insights.all_insights(acct)
    for r in data["price_increases"]:
        if r.get("kind") == "intro":
            console.print(f"[yellow]Intro price ended[/yellow] {r['vendor']}: {r['old_display']} -> {r['new_display']}")
        else:
            console.print(f"[yellow]Price up[/yellow] {r['vendor']}: {r['old_display']} -> {r['new_display']} ({r['percent']}%)")
    for r in data["upcoming"]:
        console.print(f"[cyan]Soon[/cyan] {r['vendor']} {r['kind']} {r['date']} ({r['source']})")
    for r in data["not_opened"]:
        console.print(f"[magenta]Never opened[/magenta] {r['address']}: {r['count']} emails in {r['months']} months")
    if not any(data[k] for k in ("price_increases", "upcoming", "not_opened")):
        console.print("No insights right now.")


app.command("insights")(insights_cmd)


@app.command()
def duplicates():
    """Possible duplicate charges (same price, currency and cycle) for manual review."""
    groups: dict = {}
    for r in tracker.list_subscriptions():
        if r["status"] in {"suspected", "confirmed"} and r["amount"] is not None:
            groups.setdefault((round(float(r["amount"]), 2), r["currency"], r["cycle"]), []).append(r)
    found = [g for g in groups.values() if len(g) > 1]
    if not found:
        console.print("No same-price/currency/cycle candidates. This does not prove there are no duplicates."); return
    for g in found:
        console.print(" | ".join(f"#{r['id']} {r['vendor']} {format_money(r['amount'], r['currency'])}/{r['cycle']}" for r in g))
    console.print("[yellow]Matching amounts are a clue, not proof.[/yellow]")


@app.command("savings")
def savings_cmd(account: str = _acct):
    """Estimated savings: per currency, never added across currencies."""
    s = savings.compute(_label(account))
    if not s["money"]:
        console.print("No cancelled subscriptions with a known price yet. Mark one with `clawback review ID cancelled`.")
    for m in s["money"]:
        console.print(f"[green]{m['display']['yearly']}/year[/green]  ({m['display']['monthly']}/month, {m['display']['so_far']} so far)")
    if s["converted"]:
        console.print(f"Combined: {s['converted']['display']}/year - {s['converted']['label']}")
    e = s["emails"]
    console.print(f"{e['senders_silenced']} sender{'s' if e['senders_silenced'] != 1 else ''} silenced, about {e['per_month']} fewer emails a month ({e['avoided_so_far']} so far).")
    console.print(f"[dim]{s['label']}[/dim]")


@app.command("proof")
def proof_cmd(account: str = _acct):
    """Did your unsubscribes work? Honoured / still sending / waiting."""
    acct = _label(account) or (_last_account() or ("default",))[0]
    rows = proof.refresh(acct)
    if not rows:
        console.print("No unsubscribes recorded yet."); return
    for r in rows:
        color = {"honoured": "green", "still_sending": "red", "pending": "yellow"}[r["verdict"]]
        console.print(f"[{color}]{r['verdict']:<13}[/{color}] {r['address']}  grace ends {r['grace_end'][:10]}"
                      + (f", {r['after_grace']} email(s) after it" if r["after_grace"] else ""))
    if any(r["verdict"] == "still_sending" and r["status"] == "unsubscribed" for r in rows):
        console.print("Escalate the ones still sending with: clawback escalate ADDRESS")


@app.command()
def log(account: str = _acct, limit: int = typer.Option(50, min=1)):
    """The local audit log of every action (with undo IDs)."""
    for e in tracker.audit_entries(_label(account), limit=limit):
        from .actions import REVERSIBLE_OPS
        op = (e["undo"] or {}).get("op") if isinstance(e["undo"], dict) else None
        if op in REVERSIBLE_OPS and e["ok"] and not e["undone_at"] and not e["dry_run"]:
            undo_hint = "  (undo: clawback undo %d)" % e["id"]
        elif e["action"] == "unsubscribe" and e["ok"] and not e["dry_run"]:
            undo_hint = "  (cannot be undone; to resubscribe, use the sender's site)"
        else:
            undo_hint = ""
        console.print(f"#{e['id']} {e['ts']} {e['action']} {e['target']} {'ok' if e['ok'] else 'FAILED'}"
                      f"{' (dry run)' if e['dry_run'] else ''} {e['result']}{undo_hint}")


keep_app = typer.Typer(help="Keep-list: senders that are never suggested or touched.")
app.add_typer(keep_app, name="keep")


@keep_app.command("add")
def keep_add(pattern: str, note: str = ""):
    console.print(f"Keeping {tracker.keep_add(pattern, note)}")


@keep_app.command("remove")
def keep_remove(pattern: str):
    tracker.keep_remove(pattern); console.print("Removed.")


@keep_app.command("list")
def keep_list():
    for p in tracker.keep_patterns():
        console.print(p)


@app.command("export")
def export_cmd(path: Path):
    """Export your local tracker (subscriptions, senders, keep-list, log) to JSON."""
    path.write_text(json.dumps(tracker.export_json(), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    console.print(f"[green]Exported to {path}[/green]")


@app.command("import")
def import_cmd(path: Path):
    """Merge a JSON export into this computer's tracker (local decisions win)."""
    try:
        counts = tracker.import_json(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError) as exc:
        console.print(f"[red]{exc}[/red]"); raise typer.Exit(2)
    console.print(f"Imported: {counts}")


@app.command()
def link(domain: str, url: str):
    """Save your own HTTPS cancellation link for a domain."""
    try:
        links_mod.save_link(domain, url)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]"); raise typer.Exit(2)
    console.print(f"[green]Saved cancel link for {domain}[/green]")


@app.command("privacy")
def privacy_cmd(account: str = _acct, draft_domain: str = typer.Option(None, "--draft", help="Draft a deletion request for this domain"),
                law: str = typer.Option("gdpr", help="dpdp, gdpr, uk-gdpr or ccpa"), name: str = typer.Option("", help="Your name"),
                email: str = typer.Option(None, help="Email address the company knows you by"),
                to: str = typer.Option("", help="The company's privacy contact address")):
    """Companies that have your address; draft a DPDP/GDPR/CCPA deletion request (never sent without confirmation)."""
    acct = _label(account) or (_last_account() or ("default",))[0]
    if draft_domain:
        try:
            d = privacy.draft(acct, draft_domain, law, name, email or "", to)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]"); raise typer.Exit(2)
        console.print(f"[dim]{privacy.DISCLAIMER}[/dim]\n\nDraft #{d['id']} - {d['subject']}\n\n{d['body']}")
        console.print(f"Send it (two-step confirmation): clawback send-request {d['id']}")
        return
    for e in privacy.footprint(acct)[:100]:
        console.print(f"{e['emails']:>6}  {e['company']:<24} {e['domain']}  since {(e['first_seen'] or '')[:10]}")


@app.command("send-request")
def send_request(request_ids: list[int], account: str = _acct, yes: bool = _yes):
    """Send drafted data-deletion requests (two-step confirmation)."""
    _act("send_request", [str(i) for i in request_ids], account, {}, False, yes)


@app.command("settings")
def settings_cmd(region: str = typer.Option(None), currency: str = typer.Option(None, help="Home currency, e.g. INR, USD, EUR"),
                 show_converted: bool = typer.Option(None, help="Show a combined total using your own rates"),
                 rates_file: Path = typer.Option(None, help="JSON: {\"base\":\"INR\",\"date\":\"YYYY-MM-DD\",\"rates\":{\"USD\":83.2}}")):
    """Show or change region, home currency and optional user-supplied rates."""
    changes = {}
    if region:
        changes["region"] = region.upper()
    if currency:
        changes["home_currency"] = currency.upper()
    if show_converted is not None:
        changes["show_converted_total"] = show_converted
    if rates_file:
        changes["rates"] = json.loads(rates_file.read_text(encoding="utf-8"))
    try:
        data = config.save_settings(changes) if changes else config.load_settings()
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]"); raise typer.Exit(2)
    console.print_json(json.dumps(data))


@app.command()
def version():
    """Show version."""
    console.print(f"Clawback v{__version__}")


def main() -> None:
    from .stdio import make_safe
    make_safe()
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
