"""Real-Gmail finding D: bank alerts were "spam" and GitHub, Aadhaar, NSE,
broker and Google security mail were "newsletter". Rule: transactional mail is
its own category ("important"), never suggested, kept by default, and every
action on it needs an explicit acknowledgement. Marketing from a company you
have an account with is "marketing" (newsletter), not spam."""
import pytest

import realmail as R
from helpers import write_mbox
from clawback import actions, insights, tracker
from clawback.scan import run_scan
from clawback.sources.mbox import MboxSource


@pytest.fixture
def scanned(tmp_path):
    raws = (R.sbi_neft_alerts() + R.bobcard_statements() + R.github_notifications() + R.government()
            + R.broker_mail() + R.google_security() + R.hdfc_marketing() + R.indian_marketing()
            + [R.phishing_bank()] + R.plain_newsletter())
    src = MboxSource(write_mbox(tmp_path / "imp.mbox", raws), account="imp")
    res = run_scan(src, region="IN")
    return src, res, {s["address"]: s for s in tracker.list_senders("imp")}


IMPORTANT = ["neftinfo.itps@alerts.sbi.bank.in", "donotreply@mybobcard.co.in", "notifications@github.com",
             "no-reply@sampark.gov.in", "aadhaar@uidai.gov.in", "nse_alerts@nse.co.in", "contract.notes@angeltrade.in",
             "no-reply-account-statement@reportsmailer.zerodha.net", "no-reply@accounts.google.com",
             "noreply-accounts@google.com"]
MARKETING = ["information@mailers.hdfcbank.bank.in", "offers@billdesk.com", "marketing@angelone.in",
             "offers@icashcard.in", "news@digest.example"]


@pytest.mark.parametrize("address", IMPORTANT)
def test_transactional_senders_are_important(scanned, address):
    _, _, senders = scanned
    assert senders[address]["category"] == "important", senders[address]
    assert senders[address]["important"], "the UI must be able to say why"


@pytest.mark.parametrize("address", MARKETING)
def test_marketing_from_known_companies_is_marketing_not_spam(scanned, address):
    _, _, senders = scanned
    assert senders[address]["category"] == "newsletter"
    assert senders[address]["flags"] == []


def test_lookalike_security_alert_stays_spam(scanned):
    _, _, senders = scanned
    s = senders["alerts@hdfc-secure-verify.example"]
    assert s["category"] == "spam" and any("DMARC" in f for f in s["flags"])


def test_important_senders_are_never_suggested(scanned):
    _, res, senders = scanned
    assert res.categories["important"] == len(IMPORTANT)
    assert not set(IMPORTANT) & {s["address"] for s in tracker.list_senders("imp", "newsletter")}
    assert not set(IMPORTANT) & {x["address"] for x in insights.not_opened("imp")}


@pytest.mark.parametrize("action", ["unsubscribe", "archive", "trash", "block", "report_spam", "escalate"])
def test_every_action_on_important_mail_needs_acknowledgement(scanned, action):
    src, _, _ = scanned
    plan = actions.build_plan(src, action, ["neftinfo.itps@alerts.sbi.bank.in"])
    step = plan.steps[0]
    assert step.blocked and step.ack == "important" and "important sender" in step.blocked
    assert plan.to_dict()["runnable"] == 0


def test_acknowledged_important_sender_runs_with_a_warning(scanned):
    src, _, _ = scanned
    plan = actions.build_plan(src, "unsubscribe", ["notifications@github.com"],
                              options={"acknowledged_important": ["notifications@github.com"]}, dry_run=True)
    step = plan.steps[0]
    assert not step.blocked
    assert any("you chose to include it anyway" in w for w in step.warnings)


def test_acknowledging_one_sender_does_not_unlock_another(scanned):
    src, _, _ = scanned
    plan = actions.build_plan(src, "archive", ["notifications@github.com", "aadhaar@uidai.gov.in"],
                              options={"acknowledged_important": ["notifications@github.com"]})
    by = {s.target: s for s in plan.steps}
    assert by["aadhaar@uidai.gov.in"].ack == "important"


def test_bank_marketing_and_bank_alerts_split_by_address(scanned):
    _, _, senders = scanned
    assert senders["information@mailers.hdfcbank.bank.in"]["category"] == "newsletter"
    assert senders["neftinfo.itps@alerts.sbi.bank.in"]["category"] == "important"
