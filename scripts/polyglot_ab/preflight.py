"""Fail-loud preflight: prove the model endpoint, ruby, and Postgres actually
work BEFORE the eval runs, so a dead endpoint can't masquerade as 'low skill'.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit

def fatal(m): print("[FATAL] "+m); sys.exit(1)
def ok(m): print("[ok] "+m)


def validated_http_base(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        fatal("endpoint must be an http(s) URL without embedded credentials")
    return value.rstrip("/")

ap = argparse.ArgumentParser()
ap.add_argument("--model", required=True)
ap.add_argument("--sql-backend", default="postgres")
ap.add_argument("--app", type=int, default=0)
ap.add_argument("--langs", default="ruby,postgres")
a = ap.parse_args()

host = validated_http_base(os.getenv("OLLAMA_HOST", "http://localhost:11434"))
try:
    with urllib.request.urlopen(host + "/api/tags", timeout=10) as r:  # nosec B310
        names = [m.get("name", "") for m in json.loads(r.read()).get("models", [])]
except Exception as e:
    fatal(f"Ollama not reachable at {host}: {e}  (is `ollama serve` running?)")
if not any(n == a.model or n.startswith(a.model + ":") or n.startswith(a.model) for n in names):
    fatal(f"model '{a.model}' not found in Ollama. Have: {names}. Run:  ollama pull {a.model}")
ok(f"ollama has {a.model}")
try:
    body = json.dumps({"model": a.model, "prompt": "ok", "stream": False,
                       "options": {"num_predict": 1}}).encode()
    req = urllib.request.Request(host + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:  # nosec B310
        json.loads(r.read())
    ok("model answered a probe generate")
except Exception as e:
    fatal(f"model '{a.model}' did not answer a generate: {e}")
_langs = [x.strip() for x in a.langs.split(",") if x.strip()]
if "ruby" in _langs:
    try:
        out = subprocess.run(["ruby", "-e", "puts 2+2"], capture_output=True, text=True, timeout=30)
        assert out.stdout.strip() == "4"
        ok("ruby executes")
    except FileNotFoundError:
        fatal("ruby not found on PATH. Install Ruby (https://rubyinstaller.org) OR run Postgres-only with --langs postgres")
    except Exception as e:
        fatal(f"ruby not working: {e}")
if "postgres" in _langs and a.sql_backend == "postgres":
    try:
        import psycopg
        dsn = os.getenv("POLYGLOT_PG_DSN", "postgresql:///polyglot_ab")
        with psycopg.connect(dsn, connect_timeout=10) as c:
            assert c.execute("SELECT 1").fetchone()[0] == 1
        ok("postgres reachable + queryable")
    except Exception as e:
        fatal(f"postgres not reachable via POLYGLOT_PG_DSN={os.getenv('POLYGLOT_PG_DSN','postgresql:///polyglot_ab')}: {e}")
else:
    print("[warn] sql-backend=sqlite: portable check only, NOT a true Postgres proficiency read.")
if a.app:
    url = validated_http_base(os.getenv("PRISM_URL", "http://localhost:7000"))
    try:
        with urllib.request.urlopen(url + "/api/council/config", timeout=10):  # nosec B310
            pass
        ok("app reachable (council arm)")
    except Exception as e:
        fatal(f"app not reachable at {url} for the council arm: {e}")
    if not os.getenv("PRISM_API_TOKEN"):
        fatal("PRISM_API_TOKEN not set — required for the council arm.")
print("PREFLIGHT PASSED")
