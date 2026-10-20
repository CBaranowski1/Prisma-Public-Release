#!/usr/bin/env bash
# Build + boot the arm64 CPU stack (emulated) and assert the backend works.
set -euo pipefail
COMPOSE="docker compose -f docker-compose.arm64-smoke.yml"
BASE="http://127.0.0.1:7099"

cleanup() { $COMPOSE down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== build + up (emulated arm64; slow) =="
$COMPOSE up -d --build

echo "== wait for /api/health (up to ~5 min under emulation) =="
for i in $(seq 1 150); do
  if curl -fsS "$BASE/api/health" >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS "$BASE/api/health" | grep -q healthy && echo "HEALTH OK"

echo "== verify the container is actually arm64 =="
arch=$($COMPOSE exec -T prisma uname -m)
echo "container arch: $arch"; [ "$arch" = "aarch64" ] || { echo "NOT arm64"; exit 1; }

echo "== first-run setup -> re-login (auth on) =="
curl -fsS -X POST "$BASE/api/auth/setup" -H 'content-type: application/json' \
  -d '{"username":"admin","password":"hunter2pass"}' | grep -q '"ok":true' && echo "SETUP OK"
curl -fsS -X POST "$BASE/api/auth/login" -H 'content-type: application/json' \
  -d '{"username":"admin","password":"hunter2pass"}' | grep -q '"ok":true' && echo "LOGIN OK"

echo "== fastembed embeddings work on arm64 (onnxruntime/aarch64) =="
$COMPOSE exec -T prisma python -c "from fastembed import TextEmbedding; m=TextEmbedding('sentence-transformers/all-MiniLM-L6-v2'); v=list(m.embed(['hello']))[0]; assert len(v)==384, len(v); print('EMBED OK dim', len(v))"

echo "== chroma reachable from the backend container =="
$COMPOSE exec -T prisma python -c "import os,httpx; h=os.environ['CHROMADB_HOST']; p=os.environ['CHROMADB_PORT']; r=httpx.get(f'http://{h}:{p}/api/v2/heartbeat', timeout=10); print('CHROMA', r.status_code); assert r.status_code==200"

echo "ALL ARM64 SMOKE CHECKS PASSED"
