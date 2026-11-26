# Dashboard project memory

The dashboard is a local, read-only monitoring application for a Run:ai GPU cluster and its control plane. The backend entry point is `app-v2.py`. It serves its sibling `index.html`, which contains the CSS, JavaScript, and dashboard panels. Trace a feature from its browser fetch and renderer to `Handler.do_GET` and then to the provider method below.

## Running locally

Run `python3 app-v2.py --help` to inspect options offline. Running `python3 app-v2.py` prompts for a Run:ai client ID and hidden secret, then serves the dashboard at `http://127.0.0.1:8088` by default. The server uses Python 3.9+ and the standard library. Live panels depend on the configured Run:ai endpoint, local kubectl, kubeconfig contexts, and permissions for their respective data sources. The threaded server binds to loopback and checks the Host header. Run:ai credentials and tokens stay in the backend process.

## Routes and data ownership

| Route | Backend owner | Data and scope |
| --- | --- | --- |
| `/api/workloads` | `Runai.workloads` | Pages workload records by offset, normalizes IDs, phase, durations and requested GPUs, and applies the optional repeatable startup filter `--project`. |
| `/api/metrics` | `Runai.metrics` | Returns named sections for cluster identity, GPU inventory, workload and node telemetry, utilization history and project quota. The project filter selects quota projects. Cluster telemetry remains cluster-wide. Results are cached for 25 seconds. |
| `/api/gpu` | `Kube.gpu_devices` | Per-device exporter data from the downstream GPU cluster, independent of the selected cluster view. |
| `/api/kubernetes`, `/api/pod` | `Kube.snapshot`, `Kube.pod_detail` | Cluster inventory and pod drill-down. The dashboard joins pod records to Run:ai workloads by workload ID. Pod detail needs the cluster, namespace and pod name. |
| `/api/availability` | `Kube.availability` | Prometheus range queries for node availability. Returns per-node totals, UTC week and month buckets, window metadata and retention warnings. |
| `/api/storage` | `Kube.storage` | Joins node-exporter, kubelet and cAdvisor metrics with pod claims, PVs and Longhorn replica data. Rates use five-minute windows. Each cluster client caches its result for 25 seconds. |
| `/api/logs` | `Loki.query` | Queries the Loki gateway through the Kubernetes service proxy, then filters returned JSON records by namespace, optional pod and text. The server queries the past hour by default. |

## Cluster routing and response semantics

- Kubernetes, pod, availability, storage and logs accept `cluster=downstream` or `cluster=upstream`. Omitting it selects downstream. The kubeconfig contexts default to the matching names. Loki uses a separate gateway context (default upstream) and separate stream labels (downstream for GPU logs, local for control-plane logs).
- VM availability measures Prometheus reachability of node-exporter. Kubernetes availability uses node readiness. Uptime percentage divides uptime by observed time. Missing samples contribute to no-data time. Boot-time changes supply reboot counts only in VM mode. The backend defaults to 35 days, clamps integer windows to 1–365 days, and uses 15-minute steps through seven days and hourly steps thereafter.
- A successful HTTP response can contain partial data. Metrics and storage report section failures in `errors`. Availability can return `warning`, `errors`, or a top-level `error` on query failure. Preserve those distinctions in browser renderers.
- Storage cluster totals are calculated before the response truncates the disk, volume and pod lists to 60, 40 and 20 entries respectively. Summing visible rows can undercount a cluster.

## Open items from the November 25 2026 dashboard review

These observations apply to the working copy of `index.html` at review time. Recheck them against the current code when picking up a change.

- In `loadAvailability`, the success handler clears the message that `renderAvailability` just populated. A short Prometheus retention window is one case where the warning matters.
- Availability requests can complete out of order after a window or source change. An older response can replace the current selection's data.
- `renderStatus` rebuilds workload groups in the collapsed state. A 30-second auto-refresh can close the group someone is inspecting.
- Two product decisions remain open. The backend still returns weekly and monthly breakdowns while the current UI shows window totals. The UI requests both clusters on each auto-refresh, including while the availability panel is hidden.

The repository currently has no automated test suite. The CLI help path exercises argument parsing without credentials. Live end-to-end checks require access to the relevant providers.
