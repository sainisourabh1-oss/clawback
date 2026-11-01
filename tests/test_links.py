import json

import pytest

from clawback import links


def test_builtin_link_and_neutral_subdomain():
    assert links.cancel_link("netflix.com") == "https://www.netflix.com/cancelplan"
    assert links.cancel_link("billing.spotify.com") == links.BUILTIN["spotify.com"]


def test_aws_does_not_match_amazon_prime():
    assert "console.aws" in links.cancel_link("aws.amazon.com")


def test_save_link_requires_https_and_normalizes_domain(tmp_path):
    path = tmp_path / "links.json"
    links.save_link(" Example.COM ", "https://example.com/account", path)
    assert json.loads(path.read_text()) == {"example.com": "https://example.com/account"}
    with pytest.raises(ValueError):
        links.save_link("example.com", "http://example.com/account", path)


def test_load_missing_db_resets_user_links(tmp_path):
    links._user_db = {"stale.example": "https://stale.example"}
    links.load_user_db(tmp_path / "missing.json")
    assert links._user_db == {}

def test_malicious_saved_link_is_not_returned(tmp_path):
    import json
    p = tmp_path / 'links.json'
    p.write_text(json.dumps({'example.com': 'javascript:alert(1)'}), encoding='utf-8')
    links.load_user_db(p)
    assert links.cancel_link('example.com') is None
    links.load_user_db(tmp_path / 'missing.json')

def test_builtin_official_cross_domain_links_remain_available():
    links.load_user_db()
    assert links.cancel_link('openai.com') == links.BUILTIN['openai.com']
    assert links.cancel_link('anthropic.com') == links.BUILTIN['anthropic.com']


def test_bug7_regional_and_global_services_are_covered():
    for domain in ("amazon.in", "amazon.co.uk", "amazon.de", "amazon.com", "apple.com", "youtube.com", "spotify.com",
                   "microsoft.com", "adobe.com", "canva.com", "openai.com", "anthropic.com", "linkedin.com",
                   "audible.com", "dropbox.com", "nytimes.com", "disneyplus.com", "netflix.com"):
        assert links.cancel_link(domain), domain
    assert links.cancel_link("payments.amazon.in") == "https://www.amazon.in/gp/primecentral"


def test_every_builtin_link_is_https_and_documents_its_verification():
    seen = set()
    for e in links.builtin_entries():
        assert e["url"].startswith("https://")
        assert e["verified_on"] and e["how"] and e["service"]
        assert e["domain"] not in seen
        seen.add(e["domain"])


def test_google_receipts_map_to_play_or_one():
    assert "play.google.com" in links.find_cancel_link("google.com", "Google Play: YouTube Premium")
    assert "one.google.com" in links.find_cancel_link("google.com", "Google: Google One 100 GB")
    assert links.find_cancel_link("examplebank.example", "CricStream Plus", merchant="CricStream Plus") is None


def test_user_link_cannot_point_elsewhere(tmp_path):
    p = tmp_path / "links.json"
    p.write_text(json.dumps({"netflix.com": "https://evil.example/login"}), encoding="utf-8")
    links.load_user_db(p)
    assert links.cancel_link("netflix.com") == links.BUILTIN["netflix.com"]
    links.load_user_db(tmp_path / "missing.json")
