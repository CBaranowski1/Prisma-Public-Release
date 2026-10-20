# Deploying the Prism backend on the NVIDIA DGX Spark (ARM64)

The DGX Spark (ARM64 / DGX OS) runs the shared Prism backend **and** the models;
Windows RDP users run the Plan 3c thin client pointed at the Spark's URL. The
backend is the same multi-arch Docker image proven on `linux/arm64` in Plan 3d
(every dependency resolves to a prebuilt aarch64 wheel — no source builds).

## Prerequisites (on the Spark / DGX OS)

- Docker + Docker Compose (DGX OS ships Docker).
- NVIDIA Container Toolkit (only needed if the **container** serves GPU models;
  pure host-served models do not require it):
  ```bash
  sudo apt install -y nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
  docker info | grep -i nvidia      # should show the nvidia runtime
  ```

## Bring up the backend stack

```bash
git clone <repo> && cd prisma
cp .env.example .env                # set model credentials; keep APP_BIND=127.0.0.1

# Optional GPU passthrough to the prisma container (only if the container itself
# serves models via Cookbook; host-served models don't need it). In .env:
#   COMPOSE_FILE=docker-compose.yml:docker/gpu.nvidia.yml

docker compose up -d --build        # builds the arm64 image natively on the Spark (fast; no QEMU)
curl -fsS http://127.0.0.1:7000/api/health
```

The image builds **natively** on the Spark (no emulation), so it is fast. Data
and logs persist under `./data` and `./logs` (bind mounts). Prisma Desktop is a
single-user, no-login application. Keep the host port bound to
`127.0.0.1`; obsolete admin credentials in an older `.env` do not restore the
retired login screen.

## Model serving on the Spark

The backend image is CPU-only; the GPU belongs to the **model** processes.

- **Default (matches the existing wiring): host Ollama.** Run Ollama on the Spark
  host; the backend reaches it via `host.docker.internal:11434`. Set
  `OLLAMA_BASE_URL` / `LLM_HOST` in `.env`. Co-located = backend↔model is local.
- **Alternatives:** Cookbook-launched vLLM / llama.cpp on the GPU, or an NVIDIA
  NIM container joined to the same Docker network (point `LLM_HOST` at it).

## Point the thin client at the Spark

On each Windows RDP client (Plan 3c), set the backend URL to the Spark — either
the `PRISM_URL` env var on the launch shortcut, or `%APPDATA%\Prism\client.json`:

```json
{"url":"http://127.0.0.1:7000"}
```

Prisma Desktop creates the SSH tunnel to the Spark and opens that private local
endpoint. Closing the desktop window does not stop the remote model process.

## Network / security

Do not expose the no-login application on LAN HTTP. Keep
`APP_BIND=127.0.0.1`, and access it through Prisma Desktop's SSH tunnel or a
manual `ssh -L 7000:127.0.0.1:7000 sparky@<spark-host>` port forward.

## Final verification (run when the Spark is in hand — Plan 3d Task 5)

1. `docker compose -f docker-compose.yml -f docker/gpu.nvidia.yml up -d --build`;
   confirm `nvidia-smi` works inside a GPU test container and the stack is healthy.
2. Serve a real model (Ollama pull+run, or Cookbook vLLM/llama.cpp on the GPU);
   confirm the `LLM_HOST`/`OLLAMA_BASE_URL` wiring.
3. From Prisma Desktop, open the SSH-tunneled Spark backend and run a real chat
   end-to-end against the GPU model; confirm latency is good.
4. Close and reopen Prisma Desktop; confirm the Spark containers and loaded model
   remain running.
