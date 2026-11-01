"""Real-Gmail finding E: "link domain differs from sender" fired for ordinary
email providers. Rule: a link is fine when it belongs to the sender's own
organisation (any suffix), is a known email provider (info note), or belongs
to a domain that passed DKIM/SPF on the email in the receiving provider's
topmost Authentication-Results (info note). Only other hosts are warnings."""
import pytest

import realmail as R
from helpers import write_mbox
from clawback import actions, tracker
from clawback.detect import base_domain
from clawback.scan import run_scan
from clawback.senders import authenticated_domains, check_link, related_domains
from clawback.sources.mbox import MboxSource


@pytest.fixture
def scanned(tmp_path):
    raws = list(R.esp_senders().values())
    src = MboxSource(write_mbox(tmp_path / "esp.mbox", raws), account="esp")
    run_scan(src, region="IN")
    return src, {s["address"]: s for s in tracker.list_senders("esp")}


@pytest.mark.parametrize("address, provider", [
    ("ideas@explore.pinterest.com", "SparkPost (spmta.com)"),
    ("news@notionpress.com", "Brevo (sendibm1.com)"),
    ("news@wallet.example", "SendGrid (emailinboundprocessing.com)"),
    ("news@shop.example", "newesp.example, which signed this email"),
])
def test_known_providers_and_signed_domains_are_info_not_warnings(scanned, address, provider):
    _, senders = scanned
    s = senders[address]
    assert s["flags"] == [] and s["category"] == "newsletter"
    assert any(provider in n for n in s["notes"]), s["notes"]


def test_same_organisation_on_another_suffix_is_not_flagged(scanned):
    _, senders = scanned
    s = senders["offers@alerts.sbi.bank.in"]
    assert s["flags"] == [] and s["notes"] == []


def test_unknown_unsigned_host_is_a_worded_warning(scanned):
    _, senders = scanned
    flag = senders["events@webinar.example"]["flags"][0]
    assert flag == "unsubscribe goes to clickfarm.example, which isn't the sender (webinar.example) or a known email provider"


def test_sendgrid_inbound_link_and_mailto_give_one_calm_note(scanned):
    """Real-mailbox finding: emailinboundprocessing.com/.eu (SendGrid) was
    warned about twice, once for the link and once for the mailto, and the
    two medium signals pushed ordinary newsletters into Spam."""
    _, senders = scanned
    s = senders["news@wallet.example"]
    assert s["flags"] == [] and s["category"] == "newsletter"
    assert s["notes"] == ["unsubscribe is handled by SendGrid (emailinboundprocessing.com), an email provider"]


def test_link_and_mailto_on_one_unknown_domain_are_one_warning_and_not_spam(scanned):
    _, senders = scanned
    s = senders["news@gadgets.example"]
    assert s["flags"] == ["unsubscribe goes to oddhost.example, which isn't the sender (gadgets.example) "
                          "or a known email provider"]
    assert s["spam_score"] == 2 and s["category"] == "newsletter"


def test_unsourced_provider_domain_stays_a_warning_but_alone_is_not_spam(scanned):
    """list-unsubscribe.me has no public owner we could cite, so it is not on
    the provider list: still a warning (needs "I checked this sender"), but
    one warning alone never makes a sender spam."""
    _, senders = scanned
    s = senders["do-not-reply@post.binance.com"]
    assert len(s["flags"]) == 1 and "list-unsubscribe.me" in s["flags"][0]
    assert s["category"] == "newsletter"


def test_forged_lower_authentication_results_is_ignored(scanned):
    _, senders = scanned
    s = senders["deals@promo.example"]
    assert "evil-tracker.example" not in s["signed"]
    assert any("a known email provider" in f for f in s["flags"])


def test_plan_info_note_does_not_block_but_warning_needs_ack(scanned):
    src, _ = scanned
    plan = actions.build_plan(src, "unsubscribe", ["ideas@explore.pinterest.com", "events@webinar.example"])
    by = {s.target: s for s in plan.steps}
    ok, bad = by["ideas@explore.pinterest.com"], by["events@webinar.example"]
    assert not ok.blocked and ok.warnings == [] and "SparkPost" in ok.notes[0]
    assert bad.blocked and bad.ack == "sender"
    plan2 = actions.build_plan(src, "unsubscribe", ["events@webinar.example"],
                               options={"acknowledged": ["events@webinar.example"]})
    assert not plan2.steps[0].blocked and "possible phishing" in plan2.steps[0].warnings[0]


def test_registry_suffixes_keep_banks_apart():
    assert base_domain("alerts.sbi.bank.in") == "sbi.bank.in"
    assert base_domain("alerts.hdfcbank.bank.in") == "hdfcbank.bank.in"
    assert base_domain("no-reply@sampark.gov.in".split("@")[1]) == "sampark.gov.in"
    assert not related_domains("alerts.sbi.bank.in", "alerts.hdfcbank.bank.in")
    assert related_domains("alerts.sbi.co.in", "alerts.sbi.bank.in")
    assert not related_domains("xy.com", "xy.net")  # two-letter labels are too collision-prone


def test_authenticated_domains_reads_dkim_and_spf_passes_only():
    auth = ("mx.google.com; dkim=pass header.i=@mail.brand.example header.s=s1; dkim=fail header.d=bad.example; "
            "spf=pass (google.com: domain of b@esp.example designates 192.0.2.1) smtp.mailfrom=b@esp.example; dmarc=pass")
    assert authenticated_domains(auth) == {"mail.brand.example", "esp.example"}
    assert check_link("brand.example", "https://click.esp.example/u", {"esp.example"})[0] == "info"
    assert check_link("brand.example", "https://click.other.example/u", {"esp.example"})[0] == "warn"
    assert check_link("brand.example", "http://click.esp.example/u", {"esp.example"})[0] == "unsafe"


def test_every_known_provider_domain_has_a_dated_https_source():
    """Item 2a of the first real-mailbox fix run: nothing on the provider list
    without a citation (provider docs, the provider's own site, or DNS)."""
    import json
    import re
    from datetime import date
    from importlib import resources
    raw = json.loads(resources.files("clawback").joinpath("data/email_providers.json").read_text(encoding="utf-8"))
    seen: dict[str, str] = {}
    for p in raw["providers"]:
        assert p["name"].strip()
        covered: set[str] = set()
        for src in p["sources"]:
            assert src["kind"] in {"doc", "site", "dns"}, src
            assert src["url"].startswith("https://"), src
            assert src["says"].strip(), src
            date.fromisoformat(src["checked_on"])
            covered |= set(src["covers"])
        for d in p["domains"] + p.get("patterns", []):
            assert d in covered, f"{p['name']}: {d} has no source"
        for d in p["domains"]:
            assert d not in seen, f"{d} listed for both {seen[d]} and {p['name']}"
            assert d == d.lower() and re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", d)
            seen[d] = p["name"]
    # The real-mailbox offenders from the fix run are on the list.
    assert seen["emailinboundprocessing.com"] == seen["emailinboundprocessing.eu"] == "SendGrid"
    for d in ("mailchimpapp.net", "clevertap-prod.com", "maillist-manage.in", "zcsend.in", "mjt.lu",
              "getvero.com", "adobe-campaign.com"):
        assert d in seen
