"""
Prometheus collecters for Proxmox VE cluster.
"""
# pylint: disable=too-few-public-methods

import itertools
from datetime import datetime

from prometheus_client.core import GaugeMetricFamily


def parse_cpuset(value):
    """
    Parse a Proxmox cpuset string (e.g. "0-3,8,10-11") into a set of logical
    CPU ids. Handles single values, ranges, mixed forms and whitespace.
    Malformed tokens are ignored, empty or missing values yield an empty set.
    """
    cpus = set()
    if not value:
        return cpus
    for token in str(value).split(','):
        token = token.strip()
        if not token:
            continue
        if '-' in token:
            start, _, end = token.partition('-')
            try:
                first, last = int(start.strip()), int(end.strip())
            except ValueError:
                continue
            if first <= last:
                cpus.update(range(first, last + 1))
        else:
            try:
                cpus.add(int(token))
            except ValueError:
                continue
    return cpus


class NodeConfigCollector:
    """
    Collects Proxmox VE VM information directly from config, i.e. boot, name, onboot, etc.
    For manual test: "pvesh get /nodes/<node>/<type>/<vmid>/config"

    # HELP pve_onboot_status Proxmox vm config onboot value
    # TYPE pve_onboot_status gauge
    pve_onboot_status{id="qemu/113",node="XXXX",type="qemu"} 1.0
    """

    def __init__(self, pve):
        self._pve = pve

    def collect(self):  # pylint: disable=missing-docstring
        metrics = {
            'onboot': GaugeMetricFamily(
                'pve_onboot_status',
                'Proxmox vm config onboot value',
                labels=['id', 'node', 'type']),
        }

        # Walk every online cluster node. Alloy typically scrapes one API URL
        # (any member); "local" alone would miss foreign/NR pins on peers and
        # leave dedicated-node CPU maps looking empty.
        nodes = []
        for entry in self._pve.cluster.status.get():
            if entry.get('type') != 'node':
                continue
            if 'online' in entry and not entry['online']:
                continue
            name = entry.get('name')
            if name:
                nodes.append(name)

        # (node, cpu, origin) -> count of guests pinning that cpu.
        pin_counts = {}
        # (node, origin) -> count of qemu guests with NO affinity config.
        # Explicit zeros so consumers can tell "all guests pinned" apart from
        # "collector disabled / old exporter" (metric absent).
        unpinned_counts = {}

        for node in nodes:
            unpinned_counts[(node, 'nr')] = 0
            unpinned_counts[(node, 'foreign')] = 0

            # Scrape qemu config
            vmtype = 'qemu'
            for vmdata in self._pve.nodes(node).qemu.get():
                config = self._pve.nodes(node).qemu(
                    vmdata['vmid']).config.get()
                for key, metric_value in config.items():
                    label_values = [f"{vmtype}/{vmdata['vmid']}", node, vmtype]
                    if key in metrics:
                        metrics[key].add_metric(label_values, metric_value)

                # Pinned CPUs (qemu only): count for all guests with an affinity
                # config, running or stopped - a stopped dedicated VM still owns
                # its cores. Guests without affinity float across every host CPU
                # and are counted separately: on a dedicated-cores node even one
                # such guest breaks isolation (NodeRings preflight errors on it).
                # Templates never run; skip them for both pinned and unpinned.
                if vmdata.get('template') == 1:
                    continue
                name = vmdata.get('name') or ''
                origin = 'nr' if name.startswith('NR-') else 'foreign'
                affinity_cpus = parse_cpuset(config.get('affinity'))
                if affinity_cpus:
                    for cpu in affinity_cpus:
                        key = (node, cpu, origin)
                        pin_counts[key] = pin_counts.get(key, 0) + 1
                else:
                    unpinned_counts[(node, origin)] += 1

            # Scrape LXC config
            vmtype = 'lxc'
            for vmdata in self._pve.nodes(node).lxc.get():
                config = self._pve.nodes(node).lxc(
                    vmdata['vmid']).config.get().items()
                for key, metric_value in config:
                    label_values = [f"{vmtype}/{vmdata['vmid']}", node, vmtype]
                    if key in metrics:
                        metrics[key].add_metric(label_values, metric_value)

        return itertools.chain(
            metrics.values(),
            self._pinned_cpu_metrics(pin_counts),
            self._unpinned_guest_metrics(unpinned_counts))

    @staticmethod
    def _unpinned_guest_metrics(unpinned_counts):
        """
        Build pve_node_guests_unpinned from a (node, origin) -> guest count map.
        Both origins are always emitted per visited node (explicit zeros).
        """
        unpinned = GaugeMetricFamily(
            'pve_node_guests_unpinned',
            'Number of qemu guests of this origin without a CPU affinity config',
            labels=['node', 'origin'])
        for (node, origin), count in sorted(unpinned_counts.items()):
            unpinned.add_metric([node, origin], count)
        return [unpinned]

    @staticmethod
    def _pinned_cpu_metrics(pin_counts):
        """
        Build pve_node_cpu_pinned{,_total} metric families from a
        (node, cpu, origin) -> guest count mapping.
        """
        cpu_pinned = GaugeMetricFamily(
            'pve_node_cpu_pinned',
            'Number of guests whose CPU affinity includes this host logical CPU',
            labels=['node', 'cpu', 'origin'])
        cpu_pinned_total = GaugeMetricFamily(
            'pve_node_cpu_pinned_total',
            'Number of distinct host logical CPUs pinned by guests of this origin',
            labels=['node', 'origin'])

        totals = {}
        for (node, cpu, origin), count in sorted(pin_counts.items()):
            cpu_pinned.add_metric([node, str(cpu), origin], count)
            totals[(node, origin)] = totals.get((node, origin), 0) + 1
        for (node, origin), count in sorted(totals.items()):
            cpu_pinned_total.add_metric([node, origin], count)

        return [cpu_pinned, cpu_pinned_total]

class NodeReplicationCollector:
    """
    Collects Proxmox VE Replication information directly from status, i.e. replication duration,
    last_sync, last_try, next_sync, fail_count.
    For manual test: "pvesh get /nodes/<node>/replication/<id>/status"
    """

    def __init__(self, pve):
        self._pve = pve

    def collect(self): # pylint: disable=missing-docstring

        info_metrics = {
            'info': GaugeMetricFamily(
            'pve_replication_info',
            'Proxmox vm replication info',
            labels=['id', 'type', 'source', 'target', 'guest'])
        }

        metrics = {
            'duration': GaugeMetricFamily(
                'pve_replication_duration_seconds',
                'Proxmox vm replication duration',
                labels=['id']),
            'last_sync': GaugeMetricFamily(
                'pve_replication_last_sync_timestamp_seconds',
                'Proxmox vm replication last_sync',
                labels=['id']),
            'last_try': GaugeMetricFamily(
                'pve_replication_last_try_timestamp_seconds',
                'Proxmox vm replication last_try',
                labels=['id']),
            'next_sync': GaugeMetricFamily(
                'pve_replication_next_sync_timestamp_seconds',
                'Proxmox vm replication next_sync',
                labels=['id']),
            'fail_count': GaugeMetricFamily(
                'pve_replication_failed_syncs',
                'Proxmox vm replication fail_count',
                labels=['id']),
        }

        node = None
        for entry in self._pve.cluster.status.get():
            if entry['type'] == 'node' and entry['local']:
                node = entry['name']
                break

        for jobdata in self._pve.nodes(node).replication.get():
            # Add info metric
            label_values = [
                str(jobdata['id']),
                str(jobdata['type']),
                f"node/{jobdata['source']}",
                f"node/{jobdata['target']}",
                f"{jobdata['vmtype']}/{jobdata['guest']}",
            ]
            info_metrics['info'].add_metric(label_values, 1)

            # Add metrics
            label_values = [str(jobdata['id'])]
            status = self._pve.nodes(node).replication(jobdata['id']).status.get()
            for key, metric_value in status.items():
                if key in metrics:
                    metrics[key].add_metric(label_values, metric_value)

        return itertools.chain(metrics.values(), info_metrics.values())

class SubscriptionCollector:
    """
    Collects Proxmox VE subscription information (node, subscription level, status, next due date).
    """

    def __init__(self, pve):
        self._pve = pve

    def collect(self):  # pylint: disable=missing-docstring
        info_metric = GaugeMetricFamily(
            "pve_subscription_info",
            "Proxmox VE subscription info (1 if present)",
            labels=["id", "level"],
        )

        possible_statuses = ["new", "notfound", "active", "invalid", "expired", "suspended"]
        status_metric = GaugeMetricFamily(
            "pve_subscription_status",
            "Proxmox VE subscription status (1 if matches status)",
            labels=["id", "status"],
        )

        next_due_metric = GaugeMetricFamily(
            "pve_subscription_next_due_timestamp_seconds",
            "Subscription next due date as Unix timestamp",
            labels=["id"],
        )

        node = None
        for entry in self._pve.cluster.status.get():
            if entry['type'] == 'node' and entry['local']:
                node = entry['name']
                break

        subscription = self._pve.nodes(node).subscription.get()

        level = subscription.get("level", "unknown")
        status = subscription.get("status", "unknown")

        info_metric.add_metric(
            [f"node/{node}", level],
            1,
        )

        for possible_status in possible_statuses:
            value = 1 if status == possible_status else 0
            status_metric.add_metric(
                [f"node/{node}", possible_status],
                value,
            )

        next_due_date = subscription.get("nextduedate")
        if next_due_date:
            timestamp = datetime.strptime(next_due_date, "%Y-%m-%d").timestamp()
            next_due_metric.add_metric(
                [f"node/{node}"],
                timestamp,
            )

        yield info_metric
        yield status_metric
        yield next_due_metric
