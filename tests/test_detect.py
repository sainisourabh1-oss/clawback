from clawback.detect import classify


def test_netflix_receipt():
    hit = classify(
        "Your Netflix receipt for October",
        "Thanks for your payment. Your subscription renews monthly. Amount: $15.49 USD",
        "payments.netflix.com",
    )
    assert hit is not None
    assert hit.vendor == "Netflix"
    assert hit.amount and "15.49" in hit.amount
    assert hit.kind in {"receipt", "renewal"}


def test_trial_detection():
    hit = classify(
        "Your trial ends soon",
        "Your free trial period ends in 3 days. You will be charged $9.99 per month unless you cancel.",
        "billing.exampleapp.com",
    )
    assert hit is not None
    assert hit.kind == "trial"


def test_random_newsletter_ignored():
    hit = classify("Weekly newsletter", "Here are this week's links. Enjoy!", "news.site.com")
    assert hit is None
