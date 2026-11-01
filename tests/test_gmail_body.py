import base64
from clawback.gmail_oauth import _body_from_payload

def enc(s): return base64.urlsafe_b64encode(s.encode()).decode().rstrip('=')

def test_plain_text_mime_decoding():
    assert 'renews monthly' in _body_from_payload({'mimeType':'text/plain','body':{'data':enc('Plan renews monthly')}})

def test_multipart_prefers_plain_text():
    payload={'mimeType':'multipart/alternative','parts':[
        {'mimeType':'text/html','body':{'data':enc('<b>HTML</b>')}},
        {'mimeType':'text/plain','body':{'data':enc('Plain text evidence')}}]}
    assert _body_from_payload(payload)=='Plain text evidence'


def test_gmail_query_does_not_filter_by_subject_keywords():
    from clawback.gmail_oauth import _gmail_query
    query = _gmail_query("newer_than:365d")
    assert "subject:" not in query
    assert "newer_than:365d" in query
    assert "-in:spam" in query and "-in:trash" in query


def test_html_fallback_ignores_script_and_style_content():
    payload = {'mimeType':'text/html','body':{'data':enc('<style>subscription renews monthly</style><script>charged every month</script><p>Account settings</p>')}}
    text = _body_from_payload(payload).lower()
    assert 'account settings' in text
    assert 'subscription renews monthly' not in text
    assert 'charged every month' not in text
