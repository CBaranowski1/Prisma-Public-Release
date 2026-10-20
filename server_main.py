"""Packaged-server entrypoint (frozen by PyInstaller as prism-server).

Runs the FastAPI app under uvicorn, after creating the data dir + DB schema.
In dev you can run it directly: `python server_main.py --host 127.0.0.1 --port 7000`.
"""
import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(prog="prism-server")
    parser.add_argument("--host", default=os.getenv("PRISM_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PRISM_PORT", "7000")))
    args = parser.parse_args()

    # Hand app.py the ACTUAL resolved bind host before it decides whether
    # PRISMA_DEV_SWITCHER may seed dev accounts (SEC-21 D-10). Absence of this
    # var (e.g. the Dockerfile's `CMD uvicorn app:app --host 0.0.0.0` and
    # start-macos.sh's bare-uvicorn path both bypass server_main.py entirely)
    # is treated by app.py as "unknown -- assume exposed" (fail closed). Must
    # be set BEFORE `bootstrap()`/`from app import app` so the app-import-time
    # startup gate can read it.
    os.environ["_PRISM_RESOLVED_BIND_HOST"] = args.host

    # Data dirs + schema BEFORE importing the app (the app touches the DB on import).
    from src.server_bootstrap import bootstrap
    bootstrap()

    import uvicorn
    from app import app  # FastAPI instance

    # Pass the app object (not the "app:app" import string): frozen builds can't
    # re-import by string, and we never want the reloader in production.
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
