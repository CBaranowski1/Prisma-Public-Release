# Run the public Prisma source

This branch is a **single-user, no-login application**. Its `app.py` overrides
`AUTH_ENABLED` to `false`. Setting that variable to `true` does not restore
login. Bind it to loopback; do not expose its privileged API to the internet.

## Python installation

Use Python 3.11 for the test-compatible baseline. In a fresh checkout:

```sh
python -m venv .venv
# Linux / macOS:
. .venv/bin/activate
# Windows PowerShell instead:
# .\.venv\Scripts\Activate.ps1
python -m pip install --require-hashes -r requirements.txt
python server_main.py --host 127.0.0.1 --port 7000
```

Open `http://127.0.0.1:7000`. Stop the foreground server with Ctrl+C.
The default database is SQLite. An explicit `DATABASE_URL` selects another
SQLAlchemy backend; PostgreSQL requires its driver and separate migration /
integration verification. Do not place live database credentials in Git.

## Docker

```sh
docker compose up --build
```

Keep the supplied loopback host binding. Container-internal `0.0.0.0` is not
permission to publish the host port. For remote use, use an authenticated
private access layer or SSH tunnel, not a public unauthenticated port.

## Models and private knowledge

Configure your own Ollama, vLLM, llama.cpp or other supported endpoint in the
application. Real model inference needs a reachable model service. Private
knowledge, memories, model weights and vault contents are not included.
Set your own `PRISMA_BRAIN_VAULT` or configure the brain path in the UI.
The brain/council source interfaces and generic configurations remain shipped.

## Tests

```sh
python -m pip install --require-hashes -r requirements-dev.txt
python -m pytest -q
```

Tests do not certify GPU hardware, every remote provider, PostgreSQL feature
parity, native desktop packaging or complete shell isolation. See
`SECURITY.md`, `THREAT_MODEL.md` and `PUBLIC-RUNTIME-NOTES.md`.
