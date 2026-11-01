"""Run with: python tests/test_all.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clawback.detect import classify, base_domain
from clawback.aggregate import aggregate
from clawback import links

INBOX = [
    ("Your Netflix receipt for October", "Hi, your subscription renews monthly. Amount: $15.49 USD", "payments.netflix.com"),
    ("Your Netflix receipt for September", "Thanks for your payment of $15.49 USD. Your plan renews monthly.", "info.netflix.com"),
    ("Adobe Creative Cloud — payment confirmation", "Your annual plan of $599.88 was charged. Auto-renew is on.", "adobe.com"),
    ("Your trial ends in 3 days", "Free trial ends soon. You'll be charged $12.00 per month unless you cancel.", "billing.notetaking-app.io"),
    ("Gym membership renewed", "Your monthly membership of $39.00 has been renewed.", "billing.citygym.com"),
    ("AWS billing statement", "Your monthly AWS bill of $4.17 is available. Subscription auto-renews.", "aws.amazon.com"),
    ("Weekly newsletter #42", "Here are this week's links. Enjoy!", "news.techdigest.com"),
    ("Your order has shipped!", "Order #88342 is on the way.", "store.randomshop.com"),
]


def run():
    links.load_user_db()
    hits = [h for s, b, d in INBOX if (h := classify(s, b, d))]
    subs = {s.vendor: s for s in aggregate(hits)}

    # dedup: two Netflix senders merge into one subscription
    assert subs["Netflix"].hits == 2 and subs["Netflix"].monthly_cost == 15.49
    # annual normalization: $599.88/yr -> ~$49.99/mo
    assert subs["Adobe"].cycle == "year" and abs(subs["Adobe"].monthly_cost - 49.99) < 0.01
    # base domain extraction
    assert base_domain("billing.notetaking-app.io") == "notetaking-app.io"
    # aws must NOT get the amazon prime cancel link
    aws_link = next((links.cancel_link(d) for d in [*sorted(subs["Aws"].full_domains), subs["Aws"].domain]
                     if links.cancel_link(d)), None)
    assert aws_link and "console.aws" in aws_link
    # false positives stay out
    assert len(hits) == 6 and "Techdigest" not in subs and "Randomshop" not in subs
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    run()
