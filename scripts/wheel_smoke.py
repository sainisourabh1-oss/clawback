"""Release check: build sdist + wheel, install the WHEEL into a fresh venv
outside the repo, and smoke-test it from another directory.

Fails if any non-.py file under clawback/ is missing from the wheel or the
sdist, or if the installed command cannot start, scan the demo, or serve the
web app and its static files.

Usage: python scripts/wheel_smoke.py   (needs: pip install build)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import venv
import zipfile
from pathlib import Path

# Progress lines contain currency symbols; keep them printable when output is piped on Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=True, text=True, encoding="utf-8", errors="replace", capture_output=True, **kw)


def resources() -> set[str]:
    out = set()
    for p in (ROOT / "clawback").rglob("*"):
        if p.is_file() and p.suffix not in {".py", ".pyc"} and "__pycache__" not in p.parts:
            out.add(p.relative_to(ROOT).as_posix())
    return out


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="clawback-wheel-"))
    dist = tmp / "dist"
    run([sys.executable, "-m", "build", "--outdir", str(dist), str(ROOT)])
    wheel = next(dist.glob("*.whl"))
    sdist = next(dist.glob("*.tar.gz"))
    need = resources()
    in_wheel = set(zipfile.ZipFile(wheel).namelist())
    missing_wheel = sorted(need - in_wheel)
    with tarfile.open(sdist) as t:
        in_sdist = {re.sub(r"^[^/]+/", "", n) for n in t.getnames()}
    missing_sdist = sorted(need - in_sdist)
    print(f"resources: {len(need)}; wheel: {wheel.name}; sdist: {sdist.name}")
    assert "clawback/data/providers.json" in in_wheel, "provider presets missing from the wheel"
    if missing_wheel or missing_sdist:
        print("MISSING from wheel:", missing_wheel)
        print("MISSING from sdist:", missing_sdist)
        return 1

    env_dir = tmp / "venv"
    venv.create(env_dir, with_pip=True)
    bindir = env_dir / ("Scripts" if os.name == "nt" else "bin")
    py = bindir / ("python.exe" if os.name == "nt" else "python")
    exe = bindir / ("clawback.exe" if os.name == "nt" else "clawback")
    run([str(py), "-m", "pip", "install", "--quiet", str(wheel)])
    work = tmp / "elsewhere"
    work.mkdir()
    env = dict(os.environ, CLAWBACK_HOME=str(tmp / "home"), PYTHONIOENCODING="utf-8", PYTHONPATH="", PYTHONUNBUFFERED="1")
    run([str(exe), "--help"], cwd=work, env=env)
    out = run([str(exe), "version"], cwd=work, env=env).stdout
    assert "Clawback v" in out, out
    out = run([str(exe), "scan", "--demo"], cwd=work, env=env).stdout
    assert "Read" in out and "Streamora" in out, out
    proc = subprocess.Popen([str(exe), "ui", "--no-browser", "--port", "0"], cwd=work, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    try:
        import threading
        found: list[str] = []
        lines: list[str] = []

        def reader():
            for line in proc.stdout:
                lines.append(line)
                m = re.search(r"(http://127\.0\.0\.1:\d+/)", line)
                if m and not found:
                    found.append(m.group(1))
        threading.Thread(target=reader, daemon=True).start()
        deadline = time.time() + 60
        while time.time() < deadline and not found:
            time.sleep(0.2)
        url = found[0] if found else None
        assert url, "server did not start; its output:" + chr(10) + "".join(lines)
        page = urllib.request.urlopen(url, timeout=10).read().decode("utf-8")
        assert "<title>Clawback</title>" in page and "csrf-token" in page
        for asset in ("static/app.js", "static/icons.js", "static/app.css", "static/i18n/en.json", "static/logo.svg",
                      "static/favicon.svg", "static/fonts/OFL.txt"):
            body = urllib.request.urlopen(url + asset, timeout=10).read()
            assert len(body) > 100, asset
        # the bundled font: real WOFF2 bytes with the font type, from the installed wheel
        for font in ("static/fonts/inter-latin.woff2", "static/fonts/inter-latin-ext.woff2"):
            r = urllib.request.urlopen(url + font, timeout=10)
            body = r.read()
            assert body[:4] == b"wOF2" and r.headers["Content-Type"] == "font/woff2", font
        assert all(a in page for a in ('src="/static/icons.js"', 'href="/static/fonts/inter-latin.woff2"'))
        state = urllib.request.urlopen(url + "api/state", timeout=10).read().decode("utf-8")
        assert '"version"' in state
        # connect UI: presets and honest labels come from the installed data file
        import json as _json
        st = _json.loads(state)
        ids = {p["id"] for p in st["connectors"]["providers"]}
        assert {"yahoo", "icloud", "outlook", "proton"} <= ids, ids
        labels = {c["id"]: c["label"] for c in st["connectors"]["connectors"]}
        assert labels["gmail_api"] == "verified" and labels["outlook_graph"] == "experimental", labels
        js = urllib.request.urlopen(url + "static/app.js", timeout=10).read().decode("utf-8")
        assert all(k in js for k in ("imap-provider", "ms-go", "labelBadge", "__all__")), "connect UI missing"
        out = run([str(exe), "connectors"], cwd=work, env=dict(env, COLUMNS="250")).stdout
        assert "Verified" in out and "Experimental" in out and "imap.mail.yahoo.com" in out, out
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    print("WHEEL SMOKE TEST PASSED:", wheel.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
