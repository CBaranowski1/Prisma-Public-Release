# DGX Spark deployment pathway

Prisma supports two private DGX Spark layouts:

1. **Co-located stack (recommended for one Spark):** Prisma, ChromaDB,
   SearXNG, ntfy, and vLLM run as one Docker Compose project. Prisma reaches
   vLLM only through the private Compose network.
2. **Model-only server:** vLLM runs on the Spark while Prisma stays on another
   machine and reaches it through a private tunnel or TLS endpoint.

Database-configured model endpoints still take precedence. The DGX environment
is the deployment fallback when no endpoint is selected.

NVIDIA's [agent-ready Qwen3.6 35B vLLM recipe](https://build.nvidia.com/spark/vllm/agent-ready-qwen35b)
documents the model-specific serving flags used by these Compose files. The
[container-runtime guide](https://docs.nvidia.com/dgx/dgx-spark/nvidia-container-runtime-for-docker.html)
documents the preinstalled GPU runtime. Review the selected model card before
changing model-specific vLLM arguments.

## Prerequisites

On the Spark:

```bash
docker --version
docker compose version
nvidia-smi
docker run --rm --gpus=all \
  nvcr.io/nvidia/cuda:13.0.1-devel-ubuntu24.04 \
  nvidia-smi
```

Keep `DGX_VLLM_IMAGE` pinned to the reviewed digest shown below. Revalidate the
deployment before intentionally updating that digest.

## Option A: run the complete stack on the Spark

Clone or copy the whole repository to the Spark. The application code is built
into the Prisma image; the model cache, brain, database, and logs remain in
persistent host mounts and are not stored in a running container's writable
layer.

```bash
git clone <repository-url> prisma
cd prisma
cp .env.example .env
mkdir -p deploy/dgx-spark/cache data logs
chmod 600 .env
```

Set at least these values in the root `.env`:

```dotenv
DGX_MODEL=nvidia/Qwen3.6-35B-A3B-NVFP4
DGX_API_KEY=replace-with-a-long-random-secret
DGX_VLLM_IMAGE=vllm/vllm-openai@sha256:ffb2d59b1c059a5bd8d781320c9f5189de8293693b7d95da54befddaa54abf52
DGX_HF_CACHE=./deploy/dgx-spark/cache
DGX_GPU_MEMORY_UTILIZATION=0.30
DGX_MAX_MODEL_LEN=32768
DGX_MAX_NUM_SEQS=2
DGX_MAX_NUM_BATCHED_TOKENS=4096
HF_HUB_OFFLINE=0

AUTH_ENABLED=false
LOCALHOST_BYPASS=false
APP_BIND=127.0.0.1
```

The defaults intentionally cap vLLM at 30% of the Spark's unified memory and a
32K context. The former 40% / 262K profile could make a single 35B NVFP4 model
appear to consume most of system RAM because CPU and GPU share the same 128 GB
pool. Raise these values only after measuring `available` memory under load.

Generate the DGX API secret with `openssl rand -hex 32`. Never commit the
populated `.env`.

Render and launch the merged project:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker/dgx-spark.yml \
  config --quiet

docker compose \
  -f docker-compose.yml \
  -f docker/dgx-spark.yml \
  up -d --build

docker compose \
  -f docker-compose.yml \
  -f docker/dgx-spark.yml \
  ps
```

The overlay injects these values into Prisma:

```dotenv
DGX_SPARK_BASE_URL=http://vllm:8000/v1
DGX_SPARK_API_KEY=${DGX_API_KEY}
DGX_SPARK_MODEL=${DGX_MODEL}
DGX_SPARK_MAX_MODEL_LEN=${DGX_MAX_MODEL_LEN}
DGX_SPARK_ALLOW_INSECURE_NO_AUTH=false
```

`vllm` has no host-published port in this layout. It is reachable by Prisma at
the Compose service name but not directly from the LAN. The existing Prisma
port also remains loopback-only unless `APP_BIND` is deliberately changed.

Follow model startup:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker/dgx-spark.yml \
  logs -f vllm
```

The public NVIDIA checkpoint downloads anonymously; no Hugging Face token is
stored in `.env` or passed into the container. The first start downloads the
complete model into the Spark host directory
`~/prisma/deploy/dgx-spark/cache`; it is not stored only in the container.
NVIDIA's NVFP4 build is approximately 22 GB, so leave additional disk space for
metadata and temporary download files. Prisma waits for the vLLM health check
before starting.

After the first successful startup, confirm that the cache is populated:

```bash
du -sh deploy/dgx-spark/cache
```

Then set `HF_HUB_OFFLINE=1` in `.env` and recreate the services. vLLM will load
the model entirely from the Spark's local cache:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker/dgx-spark.yml \
  up -d --force-recreate vllm prisma
```

Verify from inside the private network without printing the API key:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker/dgx-spark.yml \
  exec prisma python -c \
  "import os,httpx; r=httpx.get('http://vllm:8000/v1/models',headers={'Authorization':'Bearer '+os.environ['DGX_SPARK_API_KEY']},timeout=30); r.raise_for_status(); print(r.json())"

curl -fsS http://127.0.0.1:7000/api/ready
```

Open Prisma Desktop and check `/api/diagnostics/integrations`. It
reports DGX reachability and credential source but never returns the secret.
If a saved default model endpoint exists, select the Spark endpoint/model in
Settings or clear that default so the DGX fallback is used.

To stop the project without deleting persisted data:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker/dgx-spark.yml \
  down
```

Do not add `--volumes` unless permanent data deletion is intended.

## Option B: run only vLLM on the Spark

Use this layout when Prisma remains on another machine.

```bash
cd deploy/dgx-spark
cp .env.example .env
chmod 600 .env
mkdir -p cache
# Edit .env: set the Qwen model and a long random DGX_API_KEY.
docker compose config --quiet
docker compose up -d
docker compose ps
```

The model-only stack binds to `127.0.0.1` by default, runs as a non-root user,
drops Linux capabilities, uses a read-only root filesystem, and passes the API
key to vLLM's `--api-key` option.

For a quick encrypted connection, keep vLLM on loopback and create an SSH
tunnel from the Prisma host:

```bash
ssh -N -L 8000:127.0.0.1:8000 <spark-user>@<spark-host>
```

Point a natively running Prisma instance at the tunnel:

```dotenv
DGX_SPARK_BASE_URL=http://127.0.0.1:8000/v1
DGX_SPARK_API_KEY=the-same-long-random-secret
DGX_SPARK_MODEL=nvidia/Qwen3.6-35B-A3B-NVFP4
DGX_SPARK_ALLOW_INSECURE_NO_AUTH=false
```

For a permanent remote deployment, use a TLS reverse proxy or private overlay
network, restrict the firewall to the Prisma host, and use its HTTPS address.
Do not expose port 8000 directly to an untrusted LAN or the internet.

The embedding model is optional. Configure `DGX_SPARK_EMBEDDING_MODEL` only
when that model is actually served. `DGX_SPARK_EMBEDDING_URL` can point to a
separate authenticated OpenAI-compatible embedding server.

Prisma rejects non-HTTP(S) URLs, URL-embedded credentials, query strings, and
fragments. Authentication is required by default. The escape hatch
`DGX_SPARK_ALLOW_INSECURE_NO_AUTH=true` is for isolated development only and
should remain false in deployment.

For an internal certificate authority, mount its PEM bundle and set
`LLM_CA_BUNDLE`. TLS verification remains enabled.
