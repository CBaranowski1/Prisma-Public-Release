#!/bin/sh
# Entrypoint that fixes the #1 self-host footgun: a Docker container
# that runs as root writes root-owned files into bind-mounted host
# volumes, and the host user (or a non-root service user) then can't
# update them — silently breaking skill extraction, prefs saves, mail
# attachments, etc.
#
# The image runs as a non-root user by default. If an operator explicitly
# starts it as root, PUID/PGID remain available to repair legacy mounts
# before privileges are dropped with gosu.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
CURRENT_UID="$(id -u)"
CURRENT_GID="$(id -g)"

if [ "$CURRENT_UID" = "0" ]; then
    if ! getent group "$PGID" >/dev/null 2>&1; then
        groupadd -g "$PGID" odysseus
    fi
    if ! getent passwd "$PUID" >/dev/null 2>&1; then
        useradd -u "$PUID" -g "$PGID" -M -s /bin/sh -d /app odysseus
    fi
else
    PUID="$CURRENT_UID"
    PGID="$CURRENT_GID"
fi

# Repair ownership on every writable path the app touches at runtime.
#
# Runtime caches and analytics live below /app/data; the source tree is
# intentionally immutable. The image build owns only explicit runtime
# directories as the app user.
# The root compatibility path repairs that same allowlist; source code
# remains root-owned and is never recursively made writable at startup.
if [ "$CURRENT_UID" = "0" ]; then
    for dir in \
        /app/data \
        /app/logs \
        /app/.cache \
        /app/.local \
        /app/.ssh; do
        if [ -d "$dir" ]; then
            find "$dir" -not -uid "$PUID" -print0 2>/dev/null \
                | xargs -0 -r chown "$PUID:$PGID" 2>/dev/null || true
        fi
    done
fi

# Cookbook installs vllm/etc. via `pip install --user`, which pulls
# nvidia-cuda-* wheels into /app/.local but does not set CUDA_HOME or
# symlink /usr/local/cuda. vllm 0.22+ then crashes during engine init
# when FlashInfer tries to JIT a sampler kernel ("Could not find nvcc",
# then "CUDA compiler and toolkit headers are incompatible" on the
# mixed cuda-nvcc 13.3 / cuda-runtime 13.0 wheel combo).
#
# Auto-set CUDA_HOME if a pip-installed nvcc is present, and disable the
# FlashInfer JIT sampler — sampler only, no impact on attention path.
# No-op when vllm isn't installed.
#
# Checked layouts (all are real pip-wheel install paths):
#   nvidia/cu13        — nvidia-nvcc-cu13 (CUDA 13.x wheel style)
#   nvidia/cu12        — nvidia-nvcc-cu12 (CUDA 12.x wheel style)
#   nvidia/cuda_nvcc   — nvidia-cuda-nvcc-cu12 (older cu12 sub-package style)
for cu in \
    /app/.local/lib/python*/site-packages/nvidia/cu13 \
    /app/.local/lib/python*/site-packages/nvidia/cu12 \
    /app/.local/lib/python*/site-packages/nvidia/cuda_nvcc; do
    if [ -x "$cu/bin/nvcc" ]; then
        export CUDA_HOME="$cu"
        break
    fi
done
# Disable the FlashInfer JIT sampler unconditionally — it is sampler-only
# and has no impact on the attention path, but requires nvcc + matching
# CUDA headers at startup. Without this, vLLM crashes with "Could not find
# nvcc" even when the GPU itself is fully visible to the container.
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

# Make Cookbook-installed Python CLIs visible after `pip install --user`.
# vLLM and helper scripts land here because /app is the non-root user's HOME.
export PATH="/app/.local/bin:$PATH"

# Run first-time setup as the app user so data/ files get the right ownership.
# setup.py is idempotent — skips auth.json / .env if they already exist.
# || true so a setup failure never prevents the container from starting.
if [ "$CURRENT_UID" = "0" ]; then
    gosu "$PUID:$PGID" python /app/setup.py || true
else
    python /app/setup.py || true
fi

# Drop root and run the actual app. `gosu` is preferred over `su` /
# `sudo` because it cleans up the process tree (no extra shell layer)
# so signals (SIGTERM from `docker stop`) reach uvicorn directly.
if [ "$CURRENT_UID" = "0" ]; then
    exec gosu "$PUID:$PGID" "$@"
fi
exec "$@"
