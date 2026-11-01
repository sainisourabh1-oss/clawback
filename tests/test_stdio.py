"""Regression: real-Gmail run crashed with UnicodeEncodeError ('\u20b9') when
results were printed to a cp1252 / redirected stdout. Output must never crash
because of a character (rupee sign, emoji in a subject line)."""
import io
import os
import subprocess
import sys

from rich.console import Console

from clawback import stdio


def _run(tmp_path, extra_env):
    env = dict(os.environ, CLAWBACK_HOME=str(tmp_path / "home"), **extra_env)
    env.pop("PYTHONUTF8", None)
    return subprocess.run([sys.executable, "-m", "clawback", "scan", "--demo"], capture_output=True, env=env,
                          timeout=300)


def test_cp1252_redirected_output_does_not_crash(tmp_path):
    r = _run(tmp_path, {"PYTHONIOENCODING": "cp1252"})
    out = r.stdout.decode("cp1252", errors="replace") + r.stderr.decode("cp1252", errors="replace")
    assert r.returncode == 0, out
    assert "Traceback" not in out and "UnicodeEncodeError" not in out
    assert "219" in out  # the INR result row still printed (with ? for the sign)


def test_redirected_output_without_explicit_encoding_is_utf8(tmp_path):
    env = {"PYTHONIOENCODING": ""}
    r = _run(tmp_path, env)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert "\u20b9219" in r.stdout.decode("utf-8")


def _print_through(monkeypatch, env_encoding):
    if env_encoding:
        monkeypatch.setenv("PYTHONIOENCODING", env_encoding)
    else:
        monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    stdio.make_safe()
    Console(file=sys.stdout, width=80).print("❗ You have done a UPI txn. ₹6,250")
    sys.stdout.flush()
    return raw.getvalue()


def test_emoji_subject_on_redirected_cp1252_stream_becomes_utf8(monkeypatch):
    text = _print_through(monkeypatch, None).decode("utf-8")
    assert "❗ You have done a UPI txn. ₹6,250" in text


def test_emoji_subject_with_explicit_cp1252_is_replaced_not_raised(monkeypatch):
    text = _print_through(monkeypatch, "cp1252").decode("cp1252")
    assert "? You have done a UPI txn. ?6,250" in text
