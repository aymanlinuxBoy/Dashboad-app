# Dashboad-app

Read-only Run:ai + Kubernetes monitoring dashboard. See `AGENTS.md` for architecture and routes.

## Running locally

```
python3 app-v2.py --help
```

Running `python3 app-v2.py` prompts for a Run:ai client ID and secret, then serves the dashboard
at `http://127.0.0.1:8088`. All CLI flags have an equivalent env var (e.g. `--kube-context` /
`KUBE_CONTEXT`, `--port` / `PORT`) — see `--help` for the full list. Setting both
`RUNAI_CLIENT_ID` and `RUNAI_CLIENT_SECRET` skips the interactive prompts.

## Running in Docker

```
docker build -t runai-dashboard .
docker run --rm -p 8088:8088 \
  -e RUNAI_CLIENT_ID=... -e RUNAI_CLIENT_SECRET=... \
  runai-dashboard
```

Without a mounted kubeconfig, the Run:ai panels work normally and the Kubernetes/Loki/storage
panels report their existing `errors` sections (same as running locally without kubectl access).

## Deploying to Kubernetes

The dashboard is designed to run as a Deployment **inside the downstream (Run:ai/GPU) cluster**,
reached via `kubectl port-forward` (there's no auth layer in front of it, so it isn't meant to be
exposed on a public Ingress). It needs read access to two clusters:

- **downstream** (the cluster it runs in): granted via an in-cluster ServiceAccount, no kubeconfig
  needed.
- **upstream** (control-plane/logging cluster, hosts Prometheus + Loki): reached remotely, so it
  needs a kubeconfig delivered as a Secret.

Steps, using the manifests in `deploy/k8s/`:

1. Build and push the image: `docker build -t <registry>/runai-dashboard:latest . && docker push ...`,
   and set that image in `deploy/k8s/06-deployment.yaml`.
2. Apply the downstream-cluster manifests, in order:
   `kubectl apply -f deploy/k8s/00-namespace.yaml -f deploy/k8s/01-serviceaccount.yaml \
     -f deploy/k8s/02-clusterrole.yaml -f deploy/k8s/03-clusterrolebinding.yaml`
3. Copy `deploy/k8s/04-secret-runai.example.yaml` to `secret-runai.yaml`, fill in real Run:ai
   credentials, and `kubectl apply -f` it (this file is gitignored).
4. Hand `deploy/k8s/clusterrole-upstream.yaml` to whoever administers the upstream cluster — it's
   self-contained (namespace, ServiceAccount, RBAC) and documents how to mint a token from it.
   Build a kubeconfig from that token (context named `upstream`), copy
   `deploy/k8s/05-secret-upstream-kubeconfig.example.yaml` to `secret-upstream-kubeconfig.yaml`
   with that kubeconfig inside, and `kubectl apply -f` it in the downstream cluster.
5. Apply the rest: `kubectl apply -f deploy/k8s/06-deployment.yaml -f deploy/k8s/07-service.yaml`
6. Reach it: `kubectl -n runai-dashboard port-forward svc/runai-dashboard 8088:8088`
