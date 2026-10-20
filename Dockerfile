FROM python:3.15-slim

ARG APP_UID=1000
ARG APP_GID=1000

# System deps. tmux is required by Cookbook for background downloads/serves.
# openssh-client is required for Cookbook remote server tests, setup, probes,
# downloads, and serves from Docker installs.
# git/cmake are required when Cookbook builds llama.cpp on first llama.cpp
# launch inside Docker.
# nodejs/npm provide npx for the optional built-in Browser MCP server.
# gosu is retained for the explicit root-compatibility path. The image
# itself runs unprivileged by default.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    curl \
    git \
    nodejs \
    npm \
    tmux \
    openssh-client \
    gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN groupadd --gid "${APP_GID}" prisma \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" \
        --home-dir /app --no-create-home --shell /bin/sh prisma

# Install Python deps first (layer cache)
COPY requirements.txt .
# The base image's bundled pip/setuptools can lag current security fixes.
# Upgrade both before installing application dependencies because Cookbook
# intentionally retains pip for privileged, operator-requested runtime installs.
RUN python -m pip install --no-cache-dir --upgrade \
        "pip>=26.1.2" \
        "setuptools>=83.0.0" \
    && python -m pip install --no-cache-dir --require-hashes -r requirements.txt

# Copy app code
COPY . .

# Create explicit runtime directories. Application source stays root-owned.
# The host-mounted brain vault lives at /app/data/brains, so recursively
# chowning the shipped brains/ tree here only bloats every application rebuild.
RUN mkdir -p data logs .cache/huggingface .local .ssh \
    && chown -R "${APP_UID}:${APP_GID}" \
        data logs .cache .local .ssh

# The entrypoint supports the secure non-root default plus an explicit
# root compatibility override for repairing legacy bind-mount ownership.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 7000

# Consumes the existing GET /api/ready endpoint (src/readiness.py) — 200 only
# when every critical check (database, data_dir) passes, 503 otherwise. curl
# is already installed above; no new package needed. Makes docker-compose's
# `depends_on: condition: service_healthy` meaningful for this service.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://localhost:7000/api/ready || exit 1

ENV PRISMA_SKIP_ENV_FILE=1
ENV HOME=/app
USER ${APP_UID}:${APP_GID}

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7000"]
