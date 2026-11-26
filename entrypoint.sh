#!/bin/sh
# Assembles a KUBECONFIG for app-v2.py's `kubectl --context <name>` calls before starting it.
#
# - "downstream" context: built from this pod's own projected ServiceAccount token, so the
#   app gets read access to the cluster it runs in with no mounted kubeconfig at all.
# - "upstream" context: copied as-is from a mounted kubeconfig Secret (see deploy/k8s), because
#   the upstream cluster's API server is remote from this pod. Its context must already be named
#   to match $CONTROL_CONTEXT / $LOKI_CONTEXT (default "upstream").
set -eu

KUBE_DIR=/tmp/kube
mkdir -p "$KUBE_DIR"
DOWNSTREAM_CONTEXT_NAME="${KUBE_CONTEXT:-downstream}"
UPSTREAM_SECRET_FILE="${UPSTREAM_KUBECONFIG:-/secrets/upstream/kubeconfig}"
KUBECONFIG_PARTS=""

SA_DIR=/var/run/secrets/kubernetes.io/serviceaccount
if [ -n "${KUBERNETES_SERVICE_HOST:-}" ] && [ -f "$SA_DIR/token" ]; then
    DOWNSTREAM_KUBECONFIG="$KUBE_DIR/downstream.yaml"
    APISERVER="https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT:-443}"
    kubectl config set-cluster "$DOWNSTREAM_CONTEXT_NAME-cluster" \
        --server="$APISERVER" --certificate-authority="$SA_DIR/ca.crt" --embed-certs=true \
        --kubeconfig="$DOWNSTREAM_KUBECONFIG" >/dev/null
    kubectl config set-credentials "$DOWNSTREAM_CONTEXT_NAME-sa" \
        --token="$(cat "$SA_DIR/token")" --kubeconfig="$DOWNSTREAM_KUBECONFIG" >/dev/null
    kubectl config set-context "$DOWNSTREAM_CONTEXT_NAME" \
        --cluster="$DOWNSTREAM_CONTEXT_NAME-cluster" --user="$DOWNSTREAM_CONTEXT_NAME-sa" \
        --kubeconfig="$DOWNSTREAM_KUBECONFIG" >/dev/null
    kubectl config use-context "$DOWNSTREAM_CONTEXT_NAME" --kubeconfig="$DOWNSTREAM_KUBECONFIG" >/dev/null
    KUBECONFIG_PARTS="$DOWNSTREAM_KUBECONFIG"
else
    echo "entrypoint: no in-cluster ServiceAccount found, skipping downstream kubeconfig" >&2
fi

if [ -f "$UPSTREAM_SECRET_FILE" ]; then
    UPSTREAM_KUBECONFIG_COPY="$KUBE_DIR/upstream.yaml"
    cp "$UPSTREAM_SECRET_FILE" "$UPSTREAM_KUBECONFIG_COPY"
    chmod 600 "$UPSTREAM_KUBECONFIG_COPY"
    KUBECONFIG_PARTS="${KUBECONFIG_PARTS:+$KUBECONFIG_PARTS:}$UPSTREAM_KUBECONFIG_COPY"
else
    echo "entrypoint: no upstream kubeconfig Secret mounted at $UPSTREAM_SECRET_FILE, skipping" >&2
fi

if [ -n "$KUBECONFIG_PARTS" ]; then
    export KUBECONFIG="$KUBECONFIG_PARTS"
fi

exec python3 app-v2.py "$@"
