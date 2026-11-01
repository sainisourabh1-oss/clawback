from clawback.aggregate import aggregate
from clawback.detect import Hit, base_domain

def test_inr_and_annual_normalization():
    s=aggregate([Hit('Example','billing.example.co.in','Annual plan','₹1,200 per year','receipt',.8)])[0]
    assert s.currency=='INR' and s.cycle=='year' and s.monthly_cost==100.0
    assert s.domain=='example.co.in'

def test_separate_currencies_are_not_combined():
    hits=[Hit('Example','example.com','USD plan','$10 monthly','receipt',.8),Hit('Example','example.com','EUR plan','€9 monthly','receipt',.8)]
    assert len(aggregate(hits))==2

def test_domain_invalid_and_suffix():
    assert base_domain('')==''
    assert base_domain('mail.example.co.uk')=='example.co.uk'

def test_indian_mandate_vocabulary_is_detected():
    from clawback.detect import classify
    hit=classify('UPI AutoPay mandate confirmed','Your recurring payment mandate is active. ₹499 monthly.','payments.example.in')
    assert hit is not None and hit.kind=='mandate'
