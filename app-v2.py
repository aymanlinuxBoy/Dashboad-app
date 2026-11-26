#!/usr/bin/env python3
"""Local-only Run:ai workload viewer. Python 3.9+, no dependencies."""
import argparse
import getpass
import json
import math
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.parse import urlencode, parse_qs, quote
import re
from urllib.error import HTTPError, URLError

BASE = 'https://runai-poc-integrant.duckdns.org'

class Runai:
    def __init__(self, client_id, secret, base_url=BASE):
        self.client_id, self.secret, self.base = client_id, secret, base_url
        self.token, self.until = None, 0
        self._ids, self._cache = {}, None

    def authenticate(self):
        body = urlencode({'grant_type': 'client_credentials', 'client_id': self.client_id,
                          'client_secret': self.secret}).encode()
        req = Request(self.base + '/api/v2/token', data=body,
                      headers={'Content-Type': 'application/x-www-form-urlencoded'})
        with urlopen(req, timeout=20) as r:
            data = json.load(r)
        self.token = data['access_token']
        self.until = time.monotonic() + max(1, float(data.get('expires_in', 300)) - 30)

    def _get(self, path):
        for attempt in range(2):
            if not self.token or time.monotonic() >= self.until:
                self.authenticate()
            req = Request(self.base + path,
                          headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json'})
            try:
                with urlopen(req, timeout=20) as r:
                    return json.load(r)
            except HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self.token = None
                    continue
                raise

    def _all_workloads(self, limit=100, max_pages=20):
        """Offset paging. Stops on a short page, an empty page, or a page with no new IDs
        (the last guards against an API that ignores offset and repeats results)."""
        seen, out, offset = set(), [], 0
        for _ in range(max_pages):
            data = self._get('/api/v1/workloads?' + urlencode({'limit': limit, 'offset': offset}))
            rows = data if isinstance(data, list) else data.get('workloads', data.get('items', data.get('data')))
            if not isinstance(rows, list):
                raise ValueError('Unexpected workload response shape')
            new = [w for w in rows if isinstance(w, dict) and w.get('id') not in seen]
            if not new:
                break
            for w in new:
                seen.add(w.get('id'))
                out.append(w)
            if len(rows) < limit:
                break
            offset += len(rows)
        return out

    def workloads(self, projects):
        rows = self._all_workloads()
        return [{'id': str(w.get('id') or 'Unknown'), 'name': str(w.get('name') or 'Unknown'),
                 'projectName': str(w.get('projectName') or 'Unknown'), 'phase': str(w.get('phase') or 'Unknown'),
                 'createdAt': w.get('createdAt'),
                 'pendingSeconds': w.get('totalPendingTimeSeconds'),
                 'runningSeconds': w.get('totalRunningTimeSeconds'),
                 'gpuRequested': (w.get('workloadRequestedResources') or {}).get('gpu', {}).get('request')}
                for w in rows if isinstance(w, dict) and (projects is None or w.get('projectName') in projects)]

    WL_TYPES = ('WORKLOADS_COUNT', 'GPU_ALLOCATION', 'IDLE_ALLOCATED_GPUS')
    NODE_TYPES = ('TOTAL_GPUS', 'FREE_GPUS', 'ALLOCATED_GPUS', 'TOTAL_GPU_NODES', 'READY_GPU_NODES',
                  'TOTAL_CPU_CORES', 'USED_CPU_CORES', 'TOTAL_GPU_MEMORY_BYTES', 'USED_GPU_MEMORY_BYTES')

    @staticmethod
    def _num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    def _scalar(self, body):
        try:
            return self._num(body['values'][0]['value'])
        except (KeyError, IndexError, TypeError):
            return None

    def _series(self, body):
        out = {}
        for m in (body or {}).get('measurements', []):
            out[m.get('type')] = [{'t': v.get('timestamp'), 'v': self._num(v.get('value'))}
                                  for v in m.get('values', [])]
        return out

    def _paged(self, path, key, limit=100, max_pages=20):
        seen, out, offset = set(), [], 0
        for _ in range(max_pages):
            data = self._get(path + '?' + urlencode({'limit': limit, 'offset': offset}))
            rows = data if isinstance(data, list) else data.get(key, [])
            new = [r for r in rows if isinstance(r, dict) and r.get('id') not in seen]
            if not new:
                break
            for r in new:
                seen.add(r.get('id'))
                out.append(r)
            if len(rows) < limit:
                break
            offset += len(rows)
        return out

    def _cluster(self):
        if 'clusterObj' not in self._ids:
            clusters = self._get('/api/v1/clusters')
            self._ids['clusterObj'] = clusters[0] if clusters else {}
        return self._ids['clusterObj']

    def _cluster_uuid(self):
        return self._cluster().get('uuid')

    def _gpu_inventory(self):
        uuid = self._cluster_uuid()
        groups, rows = {}, []
        for n in self._paged('/api/v1/nodes', 'nodes'):
            if n.get('clusterUuid') != uuid:
                continue
            g = n.get('gpuInfo') or {}
            gtype = g.get('gpuType') or 'No GPU'
            gcount = int(g.get('gpuCount') or 0)
            mem = g.get('gpuMemoryBytes')
            mem_gib = round(mem / 2**30, 1) if mem else None
            rows.append({'name': n.get('name'), 'status': n.get('status'), 'nodePool': n.get('nodePool'),
                         'gpuType': gtype, 'gpuCount': gcount, 'gpuMemoryGiB': mem_gib})
            grp = groups.setdefault(gtype, {'gpuType': gtype, 'nodes': 0, 'readyNodes': 0, 'gpus': 0,
                                            'gpuMemoryGiB': mem_gib})
            grp['nodes'] += 1
            grp['gpus'] += gcount
            grp['readyNodes'] += n.get('status') == 'Ready'
        return {'byType': sorted(groups.values(), key=lambda g: (-g['gpus'], g['gpuType'])),
                'nodes': sorted(rows, key=lambda r: (r['gpuType'], r['name'] or ''))}

    def _project_id(self, name):
        key = 'project:' + name
        if key not in self._ids:
            found = next((str(p.get('id')) for p in self._paged('/api/v1/org-unit/projects', 'projects')
                          if p.get('name') == name), None)
            self._ids[key] = found
        return self._ids[key]

    def metrics(self, projects):
        if self._cache and time.monotonic() - self._cache[0] < 25:
            return self._cache[1]
        now = time.time()
        window = {'start': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now - 3600)),
                  'end': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)), 'numberOfSamples': 12}
        sections, errors = {}, {}

        def run(name, fn):
            try:
                sections[name] = fn()
            except HTTPError as e:
                errors[name] = 'Run:ai returned HTTP %s' % e.code
            except (URLError, TimeoutError):
                errors[name] = 'Cannot reach Run:ai'
            except Exception:
                errors[name] = 'Unexpected response shape'

        def workloads():
            out = {t: self._scalar(self._get('/api/v1/workloads/telemetry?' + urlencode({'telemetryType': t})))
                   for t in self.WL_TYPES}
            pend = self._get('/api/v1/workloads/telemetry?' + urlencode({'telemetryType': 'PENDING_TIME_DISTRIBUTION'}))
            out['pendingBuckets'] = [{'bucket': v['groups'][0]['value'], 'count': self._num(v['value'])}
                                     for v in pend.get('values', []) if v.get('groups')]
            return out

        def nodes():
            return {t: self._scalar(self._get('/api/v1/nodes/telemetry?' + urlencode({'telemetryType': t})))
                    for t in self.NODE_TYPES}

        def cluster():
            uuid = self._cluster_uuid()
            q = dict(window, metricType='GPU_UTILIZATION')
            return self._series(self._get('/api/v1/clusters/%s/metrics?%s' % (uuid, urlencode(q))))

        def projects_quota():
            out = {}
            names = sorted(projects) if projects is not None else sorted(
                p.get('name') for p in self._paged('/api/v1/org-unit/projects', 'projects') if p.get('name'))
            for name in names:
                pid = self._project_id(name)
                if not pid:
                    out[name] = {'error': 'project not found'}
                    continue
                q = dict(window, metricType=['GPU_QUOTA', 'GPU_ALLOCATION'])
                out[name] = self._series(self._get('/api/v1/org-unit/projects/%s/metrics?%s'
                                                   % (pid, urlencode(q, doseq=True))))
            return out

        def cluster_info():
            c = self._cluster()
            return {'name': c.get('name'), 'version': c.get('version')}

        run('cluster', cluster_info)
        run('gpuNodes', self._gpu_inventory)
        run('workloads', workloads)
        run('nodes', nodes)
        run('clusterUtilization', cluster)
        run('projectQuota', projects_quota)
        result = {'updatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)), 'window': window,
                  'sections': sections, 'errors': errors}
        self._cache = (time.monotonic(), result)
        return result

_UNITS = [('Ki', 1024), ('Mi', 1024**2), ('Gi', 1024**3), ('Ti', 1024**4),
          ('k', 1e3), ('M', 1e6), ('G', 1e9), ('T', 1e12), ('m', 1e-3), ('n', 1e-9)]
DEFAULT_KUBECTL = shutil.which('kubectl') or os.path.expanduser('~/.rd/bin/kubectl')


def qty(s):
    """Parse a Kubernetes quantity ('2101m', '6581Mi', '512M', '4') into a float."""
    if s in (None, ''):
        return 0.0
    s = str(s)
    for suffix, mult in _UNITS:
        if s.endswith(suffix):
            return float(s[:-len(suffix)]) * mult
    return float(s)


K8S_NAME = re.compile(r'[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?')


DCGM_LINE = re.compile(r'^(DCGM_FI_DEV_[A-Z_]+)\{([^}]*)\}\s+(\S+)', re.M)
DCGM_LABEL = re.compile(r'(\w+)="([^"]*)"')


class KubeError(Exception):
    pass


class Kube:
    """Read-only Kubernetes access through the user's kubectl and kubeconfig.
    Only 'get' calls are made; access is limited by the kubeconfig's RBAC."""

    def __init__(self, kubectl, context):
        self.kubectl, self.context = kubectl, context
        self._cache = None

    def _run(self, args):
        cmd = [self.kubectl, '--context', self.context, '--request-timeout=15s'] + args
        for attempt in range(2):  # one retry for transient failures
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            except (OSError, subprocess.TimeoutExpired):
                if attempt == 1:
                    raise KubeError('kubectl did not run or timed out')
                continue
            if p.returncode == 0:
                break
            if attempt == 1:
                # stderr can contain hostnames and paths, so only a category is shown to the browser
                err = p.stderr.lower()
                if 'forbidden' in err or 'unauthorized' in err:
                    raise KubeError('permission denied for this kubeconfig user')
                if any(s in err for s in ('connection refused', 'no such host', 'unable to connect',
                                          'i/o timeout', 'dial tcp')):
                    raise KubeError('cluster unreachable from this Mac')
                raise KubeError('kubectl error (exit %d)' % p.returncode)
        try:
            return json.loads(p.stdout)
        except ValueError:
            raise KubeError('kubectl returned non-JSON output')

    @staticmethod
    def _pct(used, total):
        return round(used / total * 100, 1) if total else None

    def _raw_text(self, path):
        cmd = [self.kubectl, '--context', self.context, '--request-timeout=15s', 'get', '--raw', path]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise KubeError('kubectl did not run or timed out')
        if p.returncode != 0:
            raise KubeError('kubectl error on a read-only request (exit %d)' % p.returncode)
        return p.stdout

    def gpu_devices(self):
        """Per-GPU utilization and memory, read from each NVIDIA DCGM exporter pod (read-only)."""
        if getattr(self, '_gpu_cache', None) and time.monotonic() - self._gpu_cache[0] < 25:
            return self._gpu_cache[1]
        errors, devices = {}, {}
        try:
            pods = self._run(['get', 'pods', '-n', 'gpu-operator', '-l', 'app=nvidia-dcgm-exporter',
                              '-o', 'json']).get('items', [])
        except KubeError as e:
            pods, errors['exporters'] = [], str(e)
        for pod in pods:
            if pod.get('status', {}).get('phase') != 'Running':
                continue
            name, node = pod['metadata']['name'], pod.get('spec', {}).get('nodeName')
            try:
                text = self._raw_text('/api/v1/namespaces/gpu-operator/pods/%s:9400/proxy/metrics' % name)
            except KubeError as e:
                errors[node or name] = str(e)
                continue
            for m in DCGM_LINE.finditer(text):
                metric, labels = m.group(1), dict(DCGM_LABEL.findall(m.group(2)))
                try:
                    value = float(m.group(3))
                except ValueError:
                    continue
                dev = devices.setdefault((node, labels.get('gpu')), {
                    'node': node, 'gpu': labels.get('gpu'), 'model': labels.get('modelName'),
                    'utilPct': None, 'fbUsedMiB': None, 'fbFreeMiB': None, 'pod': None, 'namespace': None})
                if metric == 'DCGM_FI_DEV_GPU_UTIL':
                    dev['utilPct'] = value
                elif metric == 'DCGM_FI_DEV_FB_USED':
                    dev['fbUsedMiB'] = value
                elif metric == 'DCGM_FI_DEV_FB_FREE':
                    dev['fbFreeMiB'] = value
                if labels.get('pod'):
                    dev['pod'] = labels['pod']
                if labels.get('namespace'):
                    dev['namespace'] = labels['namespace']
        devs = sorted(devices.values(), key=lambda d: (d['node'] or '', str(d['gpu'])))
        utils = [d['utilPct'] for d in devs if d['utilPct'] is not None]
        result = {'updatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'devices': devs,
                  'avgUtilPct': round(sum(utils) / len(utils), 1) if utils else None, 'errors': errors}
        self._gpu_cache = (time.monotonic(), result)
        return result

    PROM_QUERY = ('/api/v1/namespaces/monitoring/services/'
                  'kube-prometheus-stack-prometheus:9090/proxy/api/v1/query?query=')

    def _prom(self, expr):
        """Instant query through the Prometheus service proxy (read-only GET)."""
        body = json.loads(self._raw_text(self.PROM_QUERY + quote(expr, safe='')))
        if body.get('status') != 'success':
            raise KubeError('Prometheus query failed')
        return body['data']['result']

    PROM_RANGE = ('/api/v1/namespaces/monitoring/services/'
                  'kube-prometheus-stack-prometheus:9090/proxy/api/v1/query_range?')
    # kube-state-metrics: 1 while the node's Ready condition is true, 0 otherwise.
    READY_EXPR = 'kube_node_status_condition{condition="Ready",status="true"}'
    RETENTION_UNITS = {'ms': 0.001, 's': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800, 'y': 31536000}

    def _prom_range(self, expr, start, end, step):
        """Range query through the Prometheus service proxy (read-only GET)."""
        q = urlencode({'query': expr, 'start': '%.0f' % start, 'end': '%.0f' % end, 'step': step})
        body = json.loads(self._raw_text(self.PROM_RANGE + q))
        if body.get('status') != 'success':
            raise KubeError('Prometheus range query failed')
        return body['data']['result']

    def _prom_retention_seconds(self):
        """Retention of the monitoring Prometheus, e.g. '10d' -> 864000. None if unknown."""
        items = self._run(['get', 'prometheus', '-n', 'monitoring', '-o', 'json']).get('items', [])
        raw = (items[0].get('spec', {}) if items else {}).get('retention')
        m = re.fullmatch(r'(\d+)(ms|[smhdwy])', raw or '')
        return int(m.group(1)) * self.RETENTION_UNITS[m.group(2)] if m else None

    @staticmethod
    def _period(ts, grain):
        """UTC start and end of the calendar week (Monday) or month containing ts."""
        d = datetime.fromtimestamp(ts, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        if grain == 'month':
            s = d.replace(day=1)
            e = (s + timedelta(days=32)).replace(day=1)
        else:
            s = d - timedelta(days=d.weekday())
            e = s + timedelta(days=7)
        return s.timestamp(), e.timestamp()

    # Availability sources. Two levels:
    #   vm         up{job="node-exporter"}: the VM's own node-exporter, scraped from outside
    #              Kubernetes. 0 when the VM is down or unreachable.
    #   kubernetes kube_node_status_condition Ready: kubelet heartbeat, 0 when NotReady.
    # Missing series are no-data, never downtime.
    VM_EXPR = 'up{job="node-exporter"}'
    K8S_READY_EXPR = 'kube_node_status_condition{condition="Ready",status="true"}'
    BOOT_EXPR = 'node_boot_time_seconds{job="node-exporter"}'
    UNAME_EXPR = 'node_uname_info{job="node-exporter"}'

    def availability(self, days=35, level='vm'):
        """Per-node uptime and downtime over the window, bucketed by UTC week and month.

        Each step [t, t+step) contributes its time to uptime and downtime in proportion to the
        fraction that was up (VM level uses avg_over_time, so outages shorter than a step are
        counted). Time with no samples is no-data. Reboots come from changes in boot time."""
        if level not in ('vm', 'kubernetes'):
            raise ValueError('level must be vm or kubernetes')
        days = max(1, min(365, int(days)))
        end = time.time()
        start = end - days * 86400
        step = 900 if days <= 7 else 3600
        errors = {}
        if level == 'vm':
            expr = 'avg_over_time(%s[%ds])' % (self.VM_EXPR, step)
        else:
            expr = self.K8S_READY_EXPR
        try:
            series = self._prom_range(expr, start, end, step)
            boots = self._prom_range(self.BOOT_EXPR, start, end, step) if level == 'vm' else []
            names = {}
            if level == 'vm':
                for s in self._prom(self.UNAME_EXPR):
                    m = s.get('metric', {})
                    names[m.get('instance')] = m.get('nodename') or m.get('instance')
        except (KubeError, ValueError, KeyError, TypeError, OSError):
            return {'error': 'Prometheus query failed or Prometheus is not reachable'}
        try:
            retention = self._prom_retention_seconds()
        except (KubeError, ValueError, KeyError, TypeError, OSError):
            retention, errors['retention'] = None, 'Could not read the Prometheus retention setting'

        def key_of(metric):
            # VM level keys by instance (ip:port); Kubernetes level keys by the node label.
            if level == 'vm':
                return metric.get('instance') or 'unknown'
            return metric.get('node') or metric.get('instance') or 'unknown'

        # acc: key -> {'total': [up, down, reboots], ('week'|'month', bucket_start): [up, down, reboots]}
        acc = {}

        def slot(key, bucket):
            return acc.setdefault(key, {}).setdefault(bucket, [0.0, 0.0, 0])

        for s in series:
            key = key_of(s.get('metric', {}))
            for ts, raw in s.get('values', []):
                try:
                    t0, f = float(ts), float(raw)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(f):
                    continue
                f = min(1.0, max(0.0, f))
                a, b = max(t0, start), min(t0 + step, end)
                if b <= a:
                    continue
                t = a
                while t < b:
                    # split at the next week or month boundary so each piece lies in one bucket
                    piece = min([b] + [self._period(t, g)[1] for g in ('week', 'month')]) - t
                    for bucket in ['total'] + [(g, self._period(t, g)[0]) for g in ('week', 'month')]:
                        cell = slot(key, bucket)
                        cell[0] += f * piece
                        cell[1] += (1 - f) * piece
                    t += piece

        # reboots: a change in boot time, counted in the bucket where it was first seen
        for s in boots:
            key = key_of(s.get('metric', {}))
            prev = None
            for ts, raw in s.get('values', []):
                try:
                    t0, bt = float(ts), float(raw)
                except (TypeError, ValueError):
                    continue
                if prev is not None and abs(bt - prev) > 60 and start <= t0 <= end:
                    slot(key, 'total')[2] += 1
                    for g in ('week', 'month'):
                        slot(key, (g, self._period(t0, g)[0]))[2] += 1
                prev = bt

        def row(cell, span):
            up, down, reboots = cell
            obs = up + down
            return {'uptimeSeconds': round(up), 'downtimeSeconds': round(down),
                    'noDataSeconds': round(max(0.0, span - obs)), 'reboots': reboots,
                    'uptimePct': round(up / obs * 100, 2) if obs else None}

        nodes = []
        for key in sorted(acc):
            tally = acc[key]
            entry = row(tally.get('total', [0.0, 0.0, 0]), end - start)
            periods = {'week': [], 'month': []}
            for bucket in sorted((k for k in tally if k != 'total'), key=lambda k: (k[0], k[1])):
                grain, bs = bucket
                _, be = self._period(bs, grain)
                p = row(tally[bucket], min(be, end) - max(bs, start))
                p['start'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(bs))
                p['end'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(min(be, end)))
                p['partial'] = bs < start or be > end
                periods[grain].append(p)
            entry.update({'node': names.get(key, key) if level == 'vm' else key,
                          'instance': key if level == 'vm' else None,
                          'week': periods['week'], 'month': periods['month']})
            nodes.append(entry)

        warning = None
        if retention is not None and retention < days * 86400:
            warning = ('Requested %d days but the monitoring Prometheus keeps %d days. Older periods '
                       'have no data and are not counted as downtime.' % (days, retention // 86400))
        return {'level': level,
                'updatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(end)),
                'window': {'start': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(start)),
                           'end': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(end)),
                           'days': days, 'stepSeconds': step},
                'retentionSeconds': retention, 'warning': warning,
                'nodes': nodes, 'errors': errors}

    def storage(self):
        """Disks, volumes, pods and network from Prometheus (node-exporter, kubelet, cAdvisor),
        joined with PVC/PV objects and Longhorn replicas from the API. Read-only.
        Rates use 5-minute windows. Pod-to-disk I/O comes from cAdvisor's device label."""
        if getattr(self, '_storage_cache', None) and time.monotonic() - self._storage_cache[0] < 25:
            return self._storage_cache[1]
        errors = {}

        def query(section, expr):
            try:
                return self._prom(expr)
            except (KubeError, ValueError, KeyError, TypeError, OSError):
                errors[section] = 'Prometheus query failed or Prometheus is not reachable'
                return []

        def num(sample):
            try:
                f = float(sample[1])
            except (TypeError, ValueError, IndexError):
                return None
            return f if math.isfinite(f) else None

        def by(results, *labels):
            out = {}
            for r in results:
                key = tuple(r['metric'].get(x, '') for x in labels)
                out[key[0] if len(labels) == 1 else key] = num(r['value'])
            return out

        def mb(x):
            return None if x is None else round(x / 1e6, 2)

        def add(*values):
            present = [v for v in values if v is not None]
            return sum(present) if present else None

        def disk_of(dev):
            """/dev/sda1 -> sda, /dev/nvme0n1p2 -> nvme0n1; anything else unchanged."""
            name = dev[5:] if dev.startswith('/dev/') else dev
            for rx in (r'^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+)\d*$', r'^(nvme\d+n\d+)(p\d+)?$'):
                m = re.match(rx, name)
                if m:
                    return m.group(1)
            return name

        def kube_items(section, args, optional=False):
            try:
                return self._run(args).get('items', [])
            except KubeError as e:
                if not optional:
                    errors[section] = str(e)
                return []

        def ip_of(instance):
            return instance.rsplit(':', 1)[0]

        # node names: node-exporter reports ip:port, the API reports node names
        ip_name = {}
        for n in kube_items('nodes', ['get', 'nodes', '-o', 'json']):
            for a in n.get('status', {}).get('addresses', []):
                if a.get('type') == 'InternalIP' and ':' not in a['address']:  # node-exporter reports IPv4
                    ip_name[a['address']] = n['metadata']['name']
        name_ip = {v: k for k, v in ip_name.items()}

        # node-level disk, network and filesystem series
        dev = 'device!~"loop.*|sr[0-9]+|dm-.*"'
        nic = 'device!~"lo|veth.*|cali.*|flannel.*|cni.*|docker.*|br-.*|tunl.*|vxlan.*|kube-ipvs.*|nodelocaldns"'
        fs = 'fstype!~"tmpfs|overlay|squashfs|ramfs|nsfs"'
        disk_iops = by(query('disk', f'sum by (instance, device) (rate(node_disk_reads_completed_total{{{dev}}}[5m]) + rate(node_disk_writes_completed_total{{{dev}}}[5m]))'), 'instance', 'device')
        disk_r = by(query('disk', f'sum by (instance, device) (rate(node_disk_read_bytes_total{{{dev}}}[5m]))'), 'instance', 'device')
        disk_w = by(query('disk', f'sum by (instance, device) (rate(node_disk_written_bytes_total{{{dev}}}[5m]))'), 'instance', 'device')
        rx = by(query('network', f'sum by (instance) (rate(node_network_receive_bytes_total{{{nic}}}[5m]))'), 'instance')
        tx = by(query('network', f'sum by (instance) (rate(node_network_transmit_bytes_total{{{nic}}}[5m]))'), 'instance')
        full = by(query('filesystem', f'100 * max by (instance) (1 - node_filesystem_avail_bytes{{{fs}}} / node_filesystem_size_bytes{{{fs}}})'), 'instance')
        mounts = {}  # node ip -> [(mountpoint, device)]
        for r in query('filesystem', f'node_filesystem_size_bytes{{{fs}}}'):
            m = r['metric']
            mounts.setdefault(ip_of(m.get('instance', '')), []).append((m.get('mountpoint', ''), m.get('device', '')))

        def resolve(node, path):
            """Which disk holds a path on a node: the filesystem with the longest matching mountpoint."""
            ip = name_ip.get(node)
            best = None
            for mp, device in mounts.get(ip, []) if ip else []:
                if (path + '/').startswith(mp.rstrip('/') + '/') and (best is None or len(mp) > len(best[0])):
                    best = (mp, device)
            return {'node': node, 'path': path,
                    'disk': disk_of(best[1]) if best else None,
                    'mount': best[0] if best else None}

        # per pod, per device I/O (cAdvisor)
        pio = {}  # (instance, namespace, pod, device) -> fields
        for field, metric in [('rb', 'container_fs_reads_bytes_total'), ('wb', 'container_fs_writes_bytes_total'),
                              ('ri', 'container_fs_reads_total'), ('wi', 'container_fs_writes_total')]:
            expr = f'sum by (instance, namespace, pod, device) (rate({metric}{{pod!="",container!="",container!="POD",device=~"/dev/.*"}}[5m]))'
            for key, v in by(query('pods', expr), 'instance', 'namespace', 'pod', 'device').items():
                pio.setdefault(key, {})[field] = v

        # per pod network (cAdvisor)
        pnet = {}  # (namespace, pod) -> fields
        for field, metric in [('rx', 'container_network_receive_bytes_total'), ('tx', 'container_network_transmit_bytes_total')]:
            expr = f'sum by (namespace, pod) (rate({metric}{{pod!=""}}[5m]))'
            for key, v in by(query('pods', expr), 'namespace', 'pod').items():
                pnet.setdefault(key, {})[field] = v

        # pod -> claims, and claim -> pods
        pods_api = kube_items('pods', ['get', 'pods', '-A', '-o', 'json'])
        pod_claims, claim_pods = {}, {}
        for p in pods_api:
            ns, name = p['metadata']['namespace'], p['metadata']['name']
            claims = [v['persistentVolumeClaim']['claimName']
                      for v in p.get('spec', {}).get('volumes', []) if 'persistentVolumeClaim' in v]
            pod_claims[(ns, name)] = claims
            for c in claims:
                claim_pods.setdefault((ns, c), []).append(name)

        # claim -> where its bytes live (local-path PV hostPath, or Longhorn replicas)
        pv_host = {}
        for pv in kube_items('volumes', ['get', 'pv', '-o', 'json']):
            spec = pv.get('spec', {})
            node = None
            for term in spec.get('nodeAffinity', {}).get('required', {}).get('nodeSelectorTerms', []) or []:
                for me in term.get('matchExpressions', []):
                    if me.get('values'):
                        node = me['values'][0]
                        break
                if node:
                    break
            pv_host[pv['metadata']['name']] = {'path': (spec.get('hostPath') or {}).get('path'), 'node': node}
        claim_pv, claim_sc, claim_backing = {}, {}, {}
        for pvc in kube_items('volumes', ['get', 'pvc', '-A', '-o', 'json']):
            key = (pvc['metadata']['namespace'], pvc['metadata']['name'])
            pv = pvc.get('spec', {}).get('volumeName')
            claim_pv[pv] = key
            claim_sc[key] = pvc.get('spec', {}).get('storageClassName')
            hp = pv_host.get(pv)
            if hp and hp['path'] and hp['node']:
                claim_backing.setdefault(key, []).append((hp['node'], hp['path']))
        for r in kube_items('longhorn', ['get', 'replicas.longhorn.io', '-n', 'longhorn-system', '-o', 'json'], optional=True):
            spec = r.get('spec', {})
            key = claim_pv.get(spec.get('volumeName'))
            if key and spec.get('nodeID'):
                claim_backing.setdefault(key, []).append((spec['nodeID'], spec.get('diskPath') or ''))

        # volume usage (kubelet)
        used = by(query('volumes', 'kubelet_volume_stats_used_bytes'), 'namespace', 'persistentvolumeclaim')
        cap = by(query('volumes', 'kubelet_volume_stats_capacity_bytes'), 'namespace', 'persistentvolumeclaim')

        # physical disks: node-level rates, plus the pods and volumes that use each one
        disk_pods, disk_vols = {}, {}
        for (inst, ns, pod, device), f in pio.items():
            disk_pods.setdefault((ip_of(inst), disk_of(device)), []).append(
                (ns + '/' + pod, add(f.get('rb'), f.get('wb')) or 0, add(f.get('ri'), f.get('wi')) or 0))
        for key, backing in claim_backing.items():
            for node, path in backing:
                b = resolve(node, path)
                if b['disk']:
                    disk_vols.setdefault((node, b['disk']), set()).add(key[1])

        disk_keys = sorted(set(disk_iops) | set(disk_r) | set(disk_w))
        disks, node_totals = [], {}
        for inst, device_name in disk_keys:
            ip = ip_of(inst)
            node = ip_name.get(ip, ip)
            disk = disk_of(device_name)
            read_mb = add(disk_r.get((inst, device_name)))
            write_mb = add(disk_w.get((inst, device_name)))
            iops = disk_iops.get((inst, device_name))
            top = sorted(disk_pods.get((ip, disk), []), key=lambda t: -(t[1] or 0))[:8]
            disks.append({
                'node': node, 'disk': disk,
                'diskIops': None if iops is None else round(iops, 1),
                'diskMBps': mb(add(read_mb, write_mb)),
                'volumes': sorted(disk_vols.get((node, disk), [])),
                'pods': [{'pod': name, 'diskMBps': mb(b), 'diskIops': None if i is None else round(i, 1)} for name, b, i in top],
            })
            t = node_totals.setdefault(ip, {'iops': None, 'mbps': None})
            t['iops'] = add(t['iops'], iops)
            t['mbps'] = add(t['mbps'], read_mb, write_mb)

        rx_ip = {ip_of(k): v for k, v in rx.items()}
        tx_ip = {ip_of(k): v for k, v in tx.items()}
        full_ip = {ip_of(k): v for k, v in full.items()}
        nodes = []
        for ip in sorted(set(node_totals) | set(rx_ip) | set(tx_ip) | set(full_ip)):
            t = node_totals.get(ip, {})
            nodes.append({
                'name': ip_name.get(ip, ip), 'ip': ip,
                'diskIops': None if t.get('iops') is None else round(t['iops'], 1),
                'diskMBps': mb(t.get('mbps')),
                'netInMBps': mb(rx_ip.get(ip)),
                'netOutMBps': mb(tx_ip.get(ip)),
                'fsFullPct': None if full_ip.get(ip) is None else round(full_ip[ip], 1),
            })
        nodes.sort(key=lambda n: n['name'])
        disks.sort(key=lambda d: (d['node'], d['disk']))

        volume_keys = set(used) | set(cap) | set(claim_sc)
        volumes = []
        for key in volume_keys:
            u, c = used.get(key), cap.get(key)
            volumes.append({
                'namespace': key[0], 'claim': key[1], 'storageClass': claim_sc.get(key),
                'usedGiB': None if u is None else round(u / 2**30, 2),
                'capacityGiB': None if c is None else round(c / 2**30, 2),
                'fullPct': round(u / c * 100, 1) if u is not None and c else None,
                'pods': sorted(claim_pods.get(key, [])),
                'backing': [resolve(node, path) for node, path in claim_backing.get(key, [])],
            })
        volumes.sort(key=lambda v: (-(v['fullPct'] or -1), v['namespace'], v['claim']))

        pod_totals = {}
        for (inst, ns, pod, device), f in pio.items():
            t = pod_totals.setdefault((ns, pod), {'devices': set(), 'rb': None, 'wb': None, 'ri': None, 'wi': None})
            t['devices'].add(ip_name.get(ip_of(inst), ip_of(inst)) + ':' + disk_of(device))
            for field in ('rb', 'wb', 'ri', 'wi'):
                t[field] = add(t[field], f.get(field))
        for key in pnet:
            pod_totals.setdefault(key, {'devices': set(), 'rb': None, 'wb': None, 'ri': None, 'wi': None})
        pods = []
        for (ns, pod), t in pod_totals.items():
            n = pnet.get((ns, pod), {})
            pods.append({
                'namespace': ns, 'pod': pod,
                'volumes': pod_claims.get((ns, pod), []),
                'devices': sorted(t['devices']),
                'diskIops': None if add(t['ri'], t['wi']) is None else round(add(t['ri'], t['wi']), 1),
                'diskMBps': mb(add(t['rb'], t['wb'])),
                'netInMBps': mb(n.get('rx')), 'netOutMBps': mb(n.get('tx')),
            })
        pods.sort(key=lambda p: -((p['diskMBps'] or 0) + (p['netInMBps'] or 0) + (p['netOutMBps'] or 0)))

        totals = {
            'diskIops': round(sum(d['diskIops'] or 0 for d in disks), 1) if disks else None,
            'diskMBps': round(sum(d['diskMBps'] or 0 for d in disks), 2) if disks else None,
            'netInMBps': round(sum(n['netInMBps'] or 0 for n in nodes), 2) if nodes else None,
            'netOutMBps': round(sum(n['netOutMBps'] or 0 for n in nodes), 2) if nodes else None,
            'volUsedGiB': round(sum(v['usedGiB'] or 0 for v in volumes), 2) if volumes else None,
            'volCapGiB': round(sum(v['capacityGiB'] or 0 for v in volumes), 2) if volumes else None,
        }
        data = {
            'updatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'window': '5-minute rates',
            'cluster': totals,
            'nodes': nodes,
            'disks': disks[:60],
            'volumes': volumes[:40],
            'volumeCount': len(volumes),
            'pods': pods[:20],
            'errors': errors,
        }
        self._storage_cache = (time.monotonic(), data)
        return data

    def pod_detail(self, namespace, name):
        """One pod: status, containers, conditions and recent events. Read-only 'get' calls only."""
        if not (K8S_NAME.fullmatch(namespace or '') and K8S_NAME.fullmatch(name or '')):
            raise ValueError('invalid pod or namespace name')
        pod = self._run(['get', 'pod', name, '-n', namespace, '-o', 'json'])
        events_error = None
        try:
            ev_items = self._run(['get', 'events', '-n', namespace, '--field-selector',
                                  'involvedObject.name=' + name, '-o', 'json']).get('items', [])
        except KubeError as e:
            ev_items, events_error = [], str(e)
        spec, status = pod.get('spec', {}), pod.get('status', {})
        cstat = {c.get('name'): c for c in status.get('containerStatuses', [])}
        containers = []
        for c in spec.get('containers', []):
            st = cstat.get(c.get('name'), {})
            state = st.get('state') or {}
            state_name = next(iter(state), 'unknown')
            reason = (state.get(state_name) or {}).get('reason', '') if state else ''
            res = c.get('resources', {})
            containers.append({'name': c.get('name'), 'image': c.get('image'), 'state': state_name,
                               'reason': reason, 'ready': bool(st.get('ready')),
                               'restarts': st.get('restartCount', 0),
                               'requests': res.get('requests', {}), 'limits': res.get('limits', {})})
        events = sorted(({'type': e.get('type'), 'reason': e.get('reason'), 'message': e.get('message'),
                          'count': e.get('count', 1), 'last': e.get('lastTimestamp') or e.get('eventTime')}
                         for e in ev_items), key=lambda e: e['last'] or '', reverse=True)[:20]
        return {'name': name, 'namespace': namespace, 'node': spec.get('nodeName'),
                'phase': status.get('phase'), 'startTime': status.get('startTime'),
                'restarts': sum(c['restarts'] for c in containers), 'containers': containers,
                'conditions': [{'type': c.get('type'), 'status': c.get('status'), 'reason': c.get('reason', '')}
                               for c in status.get('conditions', [])],
                'events': events, 'eventsError': events_error,
                'updatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}

    def snapshot(self):
        if self._cache and time.monotonic() - self._cache[0] < 25:
            return self._cache[1]
        errors = {}

        def safe(name, args, default):
            try:
                return self._run(args)
            except KubeError as e:
                errors[name] = str(e)
                return default

        nodes_raw = safe('nodes', ['get', 'nodes', '-o', 'json'], {'items': []}).get('items', [])
        pods_raw = safe('pods', ['get', 'pods', '-A', '-o', 'json'], {'items': []}).get('items', [])
        node_metrics = safe('nodeMetrics', ['get', '--raw', '/apis/metrics.k8s.io/v1beta1/nodes'],
                            {'items': []}).get('items', [])
        pod_metrics = safe('podMetrics', ['get', '--raw', '/apis/metrics.k8s.io/v1beta1/pods'],
                           {'items': []}).get('items', [])

        node_use = {m['metadata']['name']: m.get('usage', {}) for m in node_metrics}
        pods_per_node = {}
        for p in pods_raw:
            node = p.get('spec', {}).get('nodeName')
            if node:
                pods_per_node[node] = pods_per_node.get(node, 0) + 1

        nodes = []
        for n in nodes_raw:
            meta, labels = n['metadata'], n['metadata'].get('labels', {})
            status = n.get('status', {})
            alloc = status.get('allocatable', {})
            use = node_use.get(meta['name'])
            cpu_used, cpu_total = qty((use or {}).get('cpu')), qty(alloc.get('cpu'))
            mem_used, mem_total = qty((use or {}).get('memory')), qty(alloc.get('memory'))
            nodes.append({
                'name': meta['name'],
                'ready': any(c.get('type') == 'Ready' and c.get('status') == 'True'
                             for c in status.get('conditions', [])),
                'roles': sorted(k.split('/', 1)[1] for k in labels if k.startswith('node-role.kubernetes.io/')),
                'cpuUsageCores': round(cpu_used, 2) if use else None,
                'cpuCores': cpu_total,
                'cpuPct': self._pct(cpu_used, cpu_total) if use else None,
                'memUsageGiB': round(mem_used / 2**30, 2) if use else None,
                'memGiB': round(mem_total / 2**30, 2),
                'memPct': self._pct(mem_used, mem_total) if use else None,
                'gpus': int(qty(alloc.get('nvidia.com/gpu'))),
                'gpuProduct': labels.get('nvidia.com/gpu.product'),
                'pods': pods_per_node.get(meta['name'], 0),
                'maxPods': int(qty(alloc.get('pods'))),
            })

        usage = {}
        for m in pod_metrics:
            key = (m['metadata'].get('namespace'), m['metadata'].get('name'))
            cpu = sum(qty(c.get('usage', {}).get('cpu')) for c in m.get('containers', []))
            mem = sum(qty(c.get('usage', {}).get('memory')) for c in m.get('containers', []))
            usage[key] = (cpu, mem)

        pods = []
        for p in pods_raw:
            labels = p['metadata'].get('labels', {})
            wid = labels.get('run.ai/workload-id')
            if not wid:
                continue
            key = (p['metadata'].get('namespace'), p['metadata'].get('name'))
            cpu_u, mem_u = usage.get(key, (None, None))
            req_cpu = req_mem = req_gpu = 0.0
            for c in p.get('spec', {}).get('containers', []):
                r = c.get('resources', {}).get('requests', {})
                req_cpu += qty(r.get('cpu'))
                req_mem += qty(r.get('memory'))
                req_gpu += qty(r.get('nvidia.com/gpu'))
            pods.append({
                'workloadId': wid,
                'name': key[1],
                'namespace': key[0],
                'node': p.get('spec', {}).get('nodeName'),
                'phase': p.get('status', {}).get('phase'),
                'restarts': sum(cs.get('restartCount', 0) for cs in p.get('status', {}).get('containerStatuses', [])),
                'cpuUsageCores': round(cpu_u, 3) if cpu_u is not None else None,
                'memUsageMiB': round(mem_u / 2**20, 1) if mem_u is not None else None,
                'cpuRequestCores': round(req_cpu, 3),
                'memRequestMiB': round(req_mem / 2**20, 1),
                'gpuRequest': int(req_gpu),
            })

        ns_raw = safe('namespaces', ['get', 'namespaces', '-o', 'json'], {'items': []}).get('items', [])
        all_pods = [{'namespace': p['metadata'].get('namespace'), 'name': p['metadata'].get('name'),
                     'phase': p.get('status', {}).get('phase'), 'node': p.get('spec', {}).get('nodeName'),
                     'restarts': sum(cs.get('restartCount', 0)
                                     for cs in p.get('status', {}).get('containerStatuses', []))}
                    for p in pods_raw]
        result = {'updatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  'context': self.context, 'nodes': nodes, 'pods': pods, 'errors': errors,
                  'namespaces': sorted(n['metadata']['name'] for n in ns_raw),
                  'allPods': all_pods}
        self._cache = (time.monotonic(), result)
        return result


LOKI_SERVICE_PATH = '/api/v1/namespaces/logging/services/loki-loki-distributed-gateway:80/proxy'
LEVEL_RE = re.compile(r'\b(ERROR|FATAL|WARN(?:ING)?|INFO|DEBUG)\b', re.I)


class Loki:
    """Read-only log queries against the Loki gateway, reached through the Kubernetes API
    service proxy (same kubectl access as the Kubernetes panel). No Loki password is needed."""

    def __init__(self, kubectl, context, cluster):
        self.kubectl, self.context, self.cluster = kubectl, context, cluster

    def query(self, ns_prefix, text='', minutes=60, limit=300, exact=False, pod=None, cluster=None):
        if not re.fullmatch(r'[a-z0-9-]{1,63}', ns_prefix or ''):
            raise ValueError('invalid namespace prefix')
        end = time.time_ns()
        start = end - minutes * 60 * 10**9
        cl = cluster or self.cluster
        logql = '{cluster="%s"} |= "%s"' % (cl, pod or ns_prefix)
        path = LOKI_SERVICE_PATH + '/loki/api/v1/query_range?' + urlencode(
            {'query': logql, 'start': start, 'end': end, 'limit': limit, 'direction': 'backward'})
        cmd = [self.kubectl, '--context', self.context, '--request-timeout=60s', 'get', '--raw', path]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=75)
            data = json.loads(p.stdout) if p.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            data = None
        if not data or data.get('status') != 'success':
            raise KubeError('log query failed or timed out')

        lines, by_pod = [], {}
        for stream in data.get('data', {}).get('result', []):
            for ts, raw in stream.get('values', []):
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                kube = obj.get('kubernetes') or {}
                ns = kube.get('namespace_name') or ''
                ok = (ns == ns_prefix) if exact else ns.startswith(ns_prefix)
                if not ok or (pod and kube.get('pod_name') != pod):
                    continue
                message = re.sub(r'\x1b\[[0-9;]*m', '', str(obj.get('message', raw)))
                if text and text.lower() not in message.lower():
                    continue
                level = None
                try:
                    level = str(json.loads(message).get('level', '')).upper() or None
                except (ValueError, AttributeError):
                    pass
                if not level:
                    m = LEVEL_RE.search(message)
                    level = m.group(1).upper() if m else 'INFO'
                level = {'WARNING': 'WARN', 'INFORMATION': 'INFO'}.get(level, level)
                pod = kube.get('pod_name') or 'unknown'
                entry = by_pod.setdefault((ns, pod), {'namespace': ns, 'pod': pod, 'lines': 0, 'errors': 0})
                entry['lines'] += 1
                is_error = level in ('ERROR', 'FATAL')
                entry['errors'] += is_error
                lines.append({'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(ts) / 1e9)),
                              'namespace': ns, 'pod': pod, 'container': kube.get('container_name'),
                              'level': level, 'message': message[:400]})
        lines.sort(key=lambda r: r['time'], reverse=True)
        return {'updatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'windowMinutes': minutes, 'namespacePrefix': ns_prefix, 'cluster': cl,
                'pods': sorted(by_pod.values(), key=lambda p: (-p['errors'], -p['lines'])),
                'lines': lines[:limit]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _view(self):
        """Cluster view from ?cluster=, default downstream. None if unknown."""
        qs = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
        name = (qs.get('cluster') or ['downstream'])[0]
        return name if name in self.server.kube_views else None

    def do_GET(self):
        # Reject other hostnames, including DNS rebinding to loopback.
        if self.headers.get('Host') not in self.server.allowed_hosts:
            self.send_error(403)
            return
        path = self.path.split('?', 1)[0]
        if path == '/':
            self.respond(200, (Path(__file__).parent / 'index.html').read_bytes(), 'text/html; charset=utf-8')
        elif path == '/api/gpu':
            try:
                self.respond_json(200, self.server.kube.gpu_devices())
            except Exception:
                self.respond_json(502, {'error': 'GPU data unavailable. Check kubectl access to the gpu-operator namespace.'})
        elif path == '/api/pod':
            qs = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
            ns = (qs.get('namespace') or [''])[0]
            name = (qs.get('name') or [''])[0]
            view = self._view()
            if view is None:
                self.respond_json(400, {'error': 'Unknown cluster.'})
                return
            if not (K8S_NAME.fullmatch(ns) and K8S_NAME.fullmatch(name)):
                self.respond_json(400, {'error': 'Pod and namespace names must be valid Kubernetes names.'})
                return
            try:
                self.respond_json(200, self.server.kube_views[view].pod_detail(ns, name))
            except KubeError as e:
                self.respond_json(502, {'error': str(e)})
            except Exception:
                self.respond_json(502, {'error': 'Pod details unavailable. Check kubectl access and that the pod exists.'})
        elif path == '/api/logs':
            qs = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
            ns_prefix = (qs.get('namespace') or ['runai'])[0]
            text = (qs.get('q') or [''])[0][:200]
            exact = (qs.get('exact') or ['0'])[0] == '1'
            try:
                limit = min(1000, max(50, int((qs.get('limit') or ['300'])[0])))
            except ValueError:
                limit = 300
            try:
                view = self._view()
                if view is None:
                    self.respond_json(400, {'error': 'Unknown cluster.'})
                    return
                pod = (qs.get('pod') or [None])[0]
                if pod is not None and not K8S_NAME.fullmatch(pod):
                    self.respond_json(400, {'error': 'Invalid pod name.'})
                    return
                self.respond_json(200, self.server.loki.query(ns_prefix, text, limit=limit, exact=exact, pod=pod,
                                                              cluster=self.server.loki_cluster[view]))
            except ValueError:
                self.respond_json(400, {'error': 'Namespace must be lowercase letters, digits and dashes.'})
            except KubeError as e:
                self.respond_json(502, {'error': str(e)})
            except Exception:
                self.respond_json(502, {'error': 'Log data unavailable. Check kubectl access to the logging stack.'})
        elif path == '/api/kubernetes':
            view = self._view()
            if view is None:
                self.respond_json(400, {'error': 'Unknown cluster.'})
                return
            try:
                self.respond_json(200, self.server.kube_views[view].snapshot())
            except Exception:
                self.respond_json(502, {'error': 'Kubernetes data unavailable. Check kubectl and the context.'})
        elif path == '/api/storage':
            view = self._view()
            if view is None:
                self.respond_json(400, {'error': 'Unknown cluster.'})
                return
            try:
                self.respond_json(200, self.server.kube_views[view].storage())
            except Exception:
                self.respond_json(502, {'error': 'Storage data unavailable. Check kubectl access to the monitoring namespace.'})
        elif path == '/api/metrics':
            try:
                self.respond_json(200, self.server.api.metrics(self.server.projects))
            except Exception:
                self.respond_json(502, {'error': 'Metrics unavailable. Check Run:ai connectivity and permissions.'})
        elif path == '/api/availability':
            qs = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
            view = self._view()
            if view is None:
                self.respond_json(400, {'error': 'Unknown cluster.'})
                return
            try:
                days = int((qs.get('days') or ['35'])[0])
            except ValueError:
                self.respond_json(400, {'error': 'days must be a whole number from 1 to 365.'})
                return
            level = (qs.get('level') or ['vm'])[0]
            if level not in ('vm', 'kubernetes'):
                self.respond_json(400, {'error': 'level must be vm or kubernetes.'})
                return
            try:
                self.respond_json(200, self.server.kube_views[view].availability(days, level))
            except Exception:
                self.respond_json(502, {'error': 'Availability data unavailable. Check kubectl access to the monitoring namespace.'})
        elif path == '/api/workloads':
            try:
                rows = self.server.api.workloads(self.server.projects)
                self.respond_json(200, {'workloads': rows, 'updatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                                       'scope': 'all' if self.server.projects is None else sorted(self.server.projects)})
            except HTTPError as e:
                self.respond_json(502, {'error': 'Run:ai returned HTTP %s. Check credentials and project permissions.' % e.code})
            except (URLError, TimeoutError):
                self.respond_json(502, {'error': 'Cannot reach Run:ai. Check network, DNS and trusted TLS certificates.'})
            except Exception:
                self.respond_json(502, {'error': 'Unexpected Run:ai response. Check the API response format.'})
        else:
            self.send_error(404)

    def respond_json(self, status, data):
        self.respond(status, json.dumps(data).encode(), 'application/json')

    def respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

def _env_list(name):
    raw = os.environ.get(name)
    return [v.strip() for v in raw.split(',') if v.strip()] if raw else None


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', 8088)))
    parser.add_argument('--bind-host', default=os.environ.get('BIND_HOST', '127.0.0.1'),
                        help="address to bind the server to (default: 127.0.0.1; use 0.0.0.0 "
                             "when running in a container/pod)")
    parser.add_argument('--allowed-host', action='append', dest='allowed_hosts',
                        default=_env_list('ALLOWED_HOSTS'),
                        help='additional "host:port" value(s) accepted in the Host header '
                             '(repeatable; env ALLOWED_HOSTS is comma-separated). The loopback '
                             'address is always accepted in addition to these.')
    parser.add_argument('--project', action='append', dest='projects',
                        default=_env_list('RUNAI_PROJECTS'),
                        help='Run:ai project name to show (repeatable; env RUNAI_PROJECTS is '
                             'comma-separated). Default: all projects')
    parser.add_argument('--runai-base-url', default=os.environ.get('RUNAI_BASE_URL', BASE),
                        help='Run:ai API base URL')
    parser.add_argument('--kube-context', default=os.environ.get('KUBE_CONTEXT', 'downstream'),
                        help='kubeconfig context for the Run:ai GPU cluster (default: downstream)')
    parser.add_argument('--kubectl', default=os.environ.get('KUBECTL', DEFAULT_KUBECTL), help='path to kubectl')
    parser.add_argument('--loki-context', default=os.environ.get('LOKI_CONTEXT', 'upstream'),
                        help='kubeconfig context whose cluster hosts the Loki gateway (default: upstream)')
    parser.add_argument('--loki-cluster', default=os.environ.get('LOKI_CLUSTER', 'downstream'),
                        help='Loki cluster label for the GPU cluster logs (default: downstream)')
    parser.add_argument('--control-context', default=os.environ.get('CONTROL_CONTEXT', 'upstream'),
                        help='kubeconfig context for the control-plane cluster (default: upstream)')
    parser.add_argument('--control-loki-cluster', default=os.environ.get('CONTROL_LOKI_CLUSTER', 'local'),
                        help='Loki cluster label for the control-plane logs (default: local)')
    args = parser.parse_args()
    projects = set(args.projects) if args.projects else None  # None = every project
    client = os.environ.get('RUNAI_CLIENT_ID')
    secret = os.environ.get('RUNAI_CLIENT_SECRET')
    if not client:
        client = input('Run:ai client ID: ').strip()
    if not client:
        raise SystemExit('A client ID is required.')
    if not secret:
        secret = getpass.getpass('Run:ai client secret (hidden): ')
    if not secret:
        raise SystemExit('A client secret is required.')
    # One thread per request, so a slow log query does not hold up the other panels.
    # daemon_threads lets Ctrl+C stop the server without waiting for requests in flight.
    ThreadingHTTPServer.daemon_threads = True
    server = ThreadingHTTPServer((args.bind_host, args.port), Handler)
    server.api = Runai(client, secret, args.runai_base_url)
    server.projects = projects
    server.kube = Kube(args.kubectl, args.kube_context)
    server.kube_views = {'downstream': server.kube, 'upstream': Kube(args.kubectl, args.control_context)}
    server.loki_cluster = {'downstream': args.loki_cluster, 'upstream': args.control_loki_cluster}
    server.loki = Loki(args.kubectl, args.loki_context, args.loki_cluster)
    server.allowed_hosts = {'127.0.0.1:%d' % args.port, 'localhost:%d' % args.port, *(args.allowed_hosts or [])}
    print('Open http://%s:%d — stop with Ctrl+C' % (args.bind_host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
