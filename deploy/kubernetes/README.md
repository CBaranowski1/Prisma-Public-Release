# Kubernetes deployment

This Kustomize base deploys one Prisma replica and one Chroma replica in a
Pod Security `restricted` namespace. Prisma uses a read-only root filesystem,
UID/GID 1000, dropped capabilities, no privilege escalation, no Kubernetes API
token, health probes, resource bounds, persistent app data, and network
policies. The Service is internal-only (`ClusterIP`).

## Required operator inputs

1. Build and push the Prisma image. DGX Spark is ARM64, so use a multi-platform
   image if this workload will run on the Spark or a mixed cluster:

   ```bash
   docker buildx build \
     --platform linux/amd64,linux/arm64 \
     -t registry.example.com/prisma:0.9.1 \
     --push .
   ```

2. Replace `ghcr.io/replace-me/prisma:0.9.1` in `kustomization.yaml` with
   that immutable tag or, preferably, a reviewed digest.

3. Create the required secret. Do not apply `secret.example.yaml`; Kubernetes
   Secrets are only base64-encoded unless your cluster enables encryption at
   rest. Prefer an external secret manager. For a controlled bootstrap:

   ```bash
   kubectl apply -f deploy/kubernetes/namespace.yaml
   cp deploy/kubernetes/secret.example.yaml /secure/path/prisma-secret.yaml
   chmod 600 /secure/path/prisma-secret.yaml
   # Replace every placeholder and blank deployment-specific value.
   kubectl apply -f /secure/path/prisma-secret.yaml
   ```

4. Set `ALLOWED_ORIGINS` to the exact HTTPS origin. For DGX Spark, populate
   `DGX_SPARK_BASE_URL`, `DGX_SPARK_API_KEY`, and `DGX_SPARK_MODEL` in that
   secret. Follow `docs/deployment-dgx-spark.md` for the vLLM side.

5. Adjust storage sizes/classes and resource requests for the target cluster.
   The default NetworkPolicy allows Prisma ingress only from pods in its own
   namespace; it does not allow an ingress controller.

## Load private brain data

Brain Markdown and vector indexes are deliberately excluded from the container
image so a registry push cannot leak the private corpus. After the base PVC
exists, use the restricted, temporary loader pod to copy the consolidated local
`brains/` directory into that PVC:

```bash
kubectl apply -k deploy/kubernetes
kubectl apply -f deploy/kubernetes/brain-loader.yaml
kubectl -n prisma wait --for=condition=Ready pod/prisma-brain-loader --timeout=2m
kubectl -n prisma cp brains/. prisma-brain-loader:/app/data/brains
kubectl delete -f deploy/kubernetes/brain-loader.yaml
kubectl -n prisma rollout restart deployment/prisma
```

Delete the loader immediately after the transfer. Repeat the copy after local
brain changes; the operation is additive and does not delete PVC content.

## Render and deploy

```bash
kubectl kustomize deploy/kubernetes > /tmp/prisma-rendered.yaml
kubectl apply -k deploy/kubernetes
kubectl -n prisma rollout status deployment/prisma --timeout=10m
kubectl -n prisma get pods,svc,pvc,networkpolicy
kubectl -n prisma port-forward service/prisma 7000:80
curl -fsS http://127.0.0.1:7000/api/ready
```

Do not route this no-login build through an Ingress, LoadBalancer, or NodePort.
`ingress.example.yaml` is intentionally non-deployable. Use the shown
loopback port-forward (or the Prisma Desktop SSH tunnel) until authentication
is restored and tested in a separate network-deployment overlay.

## Scaling and state

The default secret uses SQLite and the deployment deliberately uses
`replicas: 1` with `strategy: Recreate`, preventing two pods from writing the
same SQLite file. For multi-user production, move `DATABASE_URL` to managed
PostgreSQL first. Keep a single in-process scheduler/poller leader until those
workers are externalized; adding web replicas without that coordination can
duplicate scheduled work.

Back up both `prisma-data` and `chroma-data`. The brain seed init container
refreshes manifests, copies missing shipped definitions, and never overwrites
operator knowledge notes. Private brain data is loaded separately as described
above.
