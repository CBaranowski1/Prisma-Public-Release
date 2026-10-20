# Deploy readiness checklist

A scannable pre-flight for a private, single-user Prisma Docker deployment.
Everything below is wired in `docker-compose.yml` / `Dockerfile`; this page
documents the switches and the required loopback boundary.

## Database — SQLite default is worst-case concurrency

- [ ] **Know the default.** `DATABASE_URL` defaults to
      `sqlite:///./data/app.db` (`docker-compose.yml`). SQLite is a single
      file with a single writer at a time—appropriate for the supported
      single-user deployment, but a poor option for concurrent writes:
      under multi-request-in-flight load, writers serialize and
      contention shows up as request latency or `database is locked`
      errors.
- [ ] **Use Postgres for high write concurrency.** Set `DATABASE_URL` to an
      external PostgreSQL instance if the single-user workload still has many
      simultaneous writers:
      ```bash
      # .env — PLACEHOLDER credentials only, never commit real ones
      DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DB
      ```
      This is already fully supported (`core/database.py` uses SQLAlchemy,
      not SQLite-specific code) — no code change required, only the env var.
- [ ] Confirm `GET /api/ready` reports `checks.database.ok: true` against
      the new `DATABASE_URL` before cutting traffic over (see Healthcheck
      section below).

## Vector memory — external Chroma

- [ ] **Know the default.** Docker Compose already runs a dedicated
      `chromadb` service and points the app at it via
      `CHROMADB_HOST=chromadb` / `CHROMADB_PORT=8000`
      (`docker-compose.yml`) — this is already the production-shaped
      setup, not a dev shortcut.
- [ ] **Point at an external/managed Chroma instance instead** (e.g. a
      shared Chroma deployment across multiple app instances) by
      overriding the same two env vars:
      ```bash
      # .env
      CHROMADB_HOST=your-chroma-host
      CHROMADB_PORT=8000
      ```
- [ ] If running the bundled `chromadb` service, confirm its named volume
      (`chromadb-data`) is backed up — vector memory is not reconstructed
      automatically if the volume is lost.

## Docker HEALTHCHECK

- [ ] The image now ships a `HEALTHCHECK` (see `Dockerfile`) that curls
      the existing `GET /api/ready` endpoint (`src/readiness.py`) every
      30s, with a 30s start period and 3 retries before a container is
      marked unhealthy. No new endpoint or package was added — `curl` was
      already installed and `/api/ready` already existed.
- [ ] Verify the healthcheck is configured on a built image:
      ```bash
      docker inspect --format='{{json .Config.Healthcheck}}' <image-or-container>
      ```
- [ ] Verify a running container's live health status:
      ```bash
      docker inspect --format='{{.State.Health.Status}}' <container>
      # or, for the compose stack:
      docker compose ps
      docker compose logs prisma
      ```
      `healthy` means `/api/ready` returned 200 (database reachable, data
      dir present and writable). `unhealthy` means it returned 503 — check
      `docker compose logs prisma` for the failing check.
- [ ] Because the app service now has its own `HEALTHCHECK`, any future
      service that adds `depends_on: prisma: condition: service_healthy`
      in `docker-compose.yml` will wait on a meaningful signal instead of
      just "container started."

## Runtime layout — what's live vs. what's ignored

- [ ] Search caches, analytics, and the dedicated search error log are
      runtime state under `PRISM_DATA_DIR` (`data/search_cache/` and
      `data/search/` by default). Application source remains read-only,
      which allows the Kubernetes deployment to use
      `readOnlyRootFilesystem: true`.

## Before flipping traffic to a hardened deploy

- [ ] `DATABASE_URL` points at Postgres (or a documented, accepted SQLite
      single-writer tradeoff for a genuinely single-user deployment).
- [ ] `CHROMADB_HOST` / `CHROMADB_PORT` point at the intended Chroma
      instance (bundled or external).
- [ ] `APP_BIND=127.0.0.1`; the no-login desktop build is reachable only
      through loopback or the documented SSH tunnel.
- [ ] No Ingress, LoadBalancer, NodePort, or public reverse proxy exposes the
      Prisma application.
- [ ] `docker compose ps` shows the `prisma` container `healthy`.
- [ ] `curl -fsS http://127.0.0.1:7000/api/ready` returns 200 from outside
      the container.
