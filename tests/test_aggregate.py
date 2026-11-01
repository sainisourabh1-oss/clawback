from clawback.aggregate import aggregate
from clawback.detect import Hit, base_domain


def test_annual_price_normalizes_to_monthly():
    result = aggregate([Hit("Adobe", "adobe.com", "Annual plan", "$599.88 annual", "receipt", 0.8)])[0]
    assert result.cycle == "year"
    assert result.monthly_cost == 49.99


def test_multi_label_suffix_domain():
    assert base_domain("billing.example.co.uk") == "example.co.uk"
    assert base_domain("payments.example.co.in") == "example.co.in"


def test_zero_cost_is_not_missing():
    result = aggregate([Hit("Example", "example.com", "Free plan receipt", "$0.00 monthly", "receipt", 0.5)])[0]
    assert result.monthly_cost == 0.0


def test_distinct_plans_from_same_vendor_are_not_merged():
    hits = [
        Hit("Example", "billing.example.com", "Example Premium monthly receipt", "$10.00 monthly", "receipt", .8),
        Hit("Example", "billing.example.com", "Example Business monthly receipt", "$40.00 monthly", "receipt", .8),
    ]
    result = aggregate(hits)
    assert len(result) == 2
    assert {s.monthly_cost for s in result} == {10.0, 40.0}


def test_monthly_receipts_with_changing_dates_still_merge():
    hits = [
        Hit("Netflix", "payments.netflix.com", "Your Netflix receipt for September 2026", "$15.49 monthly", "receipt", .8),
        Hit("Netflix", "info.netflix.com", "Your Netflix receipt for October 2026", "$15.49 monthly", "receipt", .8),
    ]
    result = aggregate(hits)
    assert len(result) == 1
    assert result[0].hits == 2


def test_monthly_and_annual_plans_are_separate():
    result = aggregate([
        Hit("Example", "example.com", "Example Premium subscription", "$10 monthly", "receipt", .8),
        Hit("Example", "example.com", "Example Premium subscription", "$100 annual", "receipt", .8),
    ])
    assert len(result) == 2
    assert {s.cycle for s in result} == {"month", "year"}


def test_generic_subjects_with_different_prices_do_not_merge():
    hits = [
        Hit("Example", "billing.example.com", "Payment receipt", "$10.00 monthly", "receipt", .7),
        Hit("Example", "billing.example.com", "Payment receipt", "$40.00 monthly", "receipt", .7),
    ]
    result = aggregate(hits)
    assert len(result) == 2
    assert {s.monthly_cost for s in result} == {10.0, 40.0}
