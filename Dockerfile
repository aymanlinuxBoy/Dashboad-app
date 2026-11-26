# syntax=docker/dockerfile:1
# Pinned kubectl release matching the target cluster's Kubernetes minor version.
# Override at build time with --build-arg KUBECTL_VERSION=vX.Y.Z if the cluster differs.
ARG KUBECTL_VERSION=v1.36.3

FROM debian:bookworm-slim AS kubectl
ARG KUBECTL_VERSION
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSLo /kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
 && curl -fsSLo /kubectl.sha256 "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256" \
 && echo "$(cat /kubectl.sha256)  /kubectl" | sha256sum -c - \
 && chmod +x /kubectl

FROM python:3.12-slim
COPY --from=kubectl /kubectl /usr/local/bin/kubectl
RUN useradd --create-home --home-dir /home/appuser --uid 10001 appuser
WORKDIR /app
COPY app-v2.py index.html entrypoint.sh ./
RUN chmod +x entrypoint.sh
USER appuser
ENV HOME=/home/appuser
EXPOSE 8088
ENTRYPOINT ["./entrypoint.sh"]
