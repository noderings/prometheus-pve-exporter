"""
Tests for pve_exporter.collector.node (NodeConfigCollector and cpuset parsing).
"""

import pytest

from pve_exporter.collector.node import NodeConfigCollector, parse_cpuset


class FakeEndpoint:
    """Endpoint returning a fixed value from get()."""

    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


class FakeGuestCollection:
    """Mimics pve.nodes(node).qemu / .lxc: get() lists guests, call(vmid) drills down."""

    def __init__(self, guests, configs):
        self._guests = guests
        self._configs = configs

    def get(self):
        return self._guests

    def __call__(self, vmid):
        return FakeGuest(self._configs[vmid])


class FakeGuest:
    def __init__(self, config):
        self.config = FakeEndpoint(config)


class FakeNode:
    def __init__(self, qemu_guests, qemu_configs, lxc_guests, lxc_configs):
        self.qemu = FakeGuestCollection(qemu_guests, qemu_configs)
        self.lxc = FakeGuestCollection(lxc_guests, lxc_configs)


class FakePve:
    def __init__(self, qemu_guests, qemu_configs, lxc_guests=None, lxc_configs=None,
                 remote_nodes=None):
        """
        remote_nodes: optional {name: FakeNode} for peer cluster members.
        The collector must walk all online nodes, not only local.
        """
        self.cluster = type('Cluster', (), {})()
        status = [
            {'type': 'cluster', 'name': 'test-cluster'},
            {'type': 'node', 'local': 1, 'name': 'pve01', 'online': 1},
        ]
        self._nodes = {
            'pve01': FakeNode(qemu_guests, qemu_configs,
                              lxc_guests or [], lxc_configs or {}),
        }
        for name, node in (remote_nodes or {}).items():
            status.append({'type': 'node', 'local': 0, 'name': name, 'online': 1})
            self._nodes[name] = node
        self.cluster.status = FakeEndpoint(status)

    def nodes(self, name):
        return self._nodes[name]


def collect_families(pve):
    """Run the collector and index metric families by name."""
    collector = NodeConfigCollector(pve)
    return {family.name: family for family in collector.collect()}


def samples(family):
    """Map (sorted label items) -> value for all samples of a family."""
    return {tuple(sorted(sample.labels.items())): sample.value
            for sample in family.samples}


class TestParseCpuset:
    def test_singles(self):
        assert parse_cpuset('0,4') == {0, 4}

    def test_single_value(self):
        assert parse_cpuset('7') == {7}

    def test_range(self):
        assert parse_cpuset('0-3') == {0, 1, 2, 3}

    def test_mixed(self):
        assert parse_cpuset('0-3,8,10-11') == {0, 1, 2, 3, 8, 10, 11}

    def test_whitespace(self):
        assert parse_cpuset(' 0 , 2 - 3 ') == {0, 2, 3}

    def test_empty_string(self):
        assert parse_cpuset('') == set()

    def test_none(self):
        assert parse_cpuset(None) == set()

    def test_malformed_tokens_ignored(self):
        assert parse_cpuset('a,1,2-,3--5,-2,,4-x') == {1}

    def test_reversed_range_ignored(self):
        assert parse_cpuset('5-3,7') == {7}

    def test_integer_value(self):
        assert parse_cpuset(3) == {3}


class TestNodeConfigCollector:
    def test_nr_and_foreign_classification(self):
        pve = FakePve(
            qemu_guests=[
                {'vmid': 100, 'name': 'NR-abc123', 'status': 'running'},
                {'vmid': 101, 'name': 'customer-vm', 'status': 'running'},
            ],
            qemu_configs={
                100: {'affinity': '0-1'},
                101: {'affinity': '2'},
            },
        )
        families = collect_families(pve)

        pinned = samples(families['pve_node_cpu_pinned'])
        assert pinned == {
            (('cpu', '0'), ('node', 'pve01'), ('origin', 'nr')): 1,
            (('cpu', '1'), ('node', 'pve01'), ('origin', 'nr')): 1,
            (('cpu', '2'), ('node', 'pve01'), ('origin', 'foreign')): 1,
        }

        totals = samples(families['pve_node_cpu_pinned_total'])
        assert totals == {
            (('node', 'pve01'), ('origin', 'nr')): 2,
            (('node', 'pve01'), ('origin', 'foreign')): 1,
        }

    def test_missing_name_is_foreign(self):
        pve = FakePve(
            qemu_guests=[{'vmid': 100, 'status': 'running'}],
            qemu_configs={100: {'affinity': '5'}},
        )
        pinned = samples(collect_families(pve)['pve_node_cpu_pinned'])
        assert pinned == {
            (('cpu', '5'), ('node', 'pve01'), ('origin', 'foreign')): 1,
        }

    def test_stopped_guest_included(self):
        pve = FakePve(
            qemu_guests=[{'vmid': 100, 'name': 'NR-stopped', 'status': 'stopped'}],
            qemu_configs={100: {'affinity': '0-3'}},
        )
        families = collect_families(pve)
        assert len(families['pve_node_cpu_pinned'].samples) == 4
        totals = samples(families['pve_node_cpu_pinned_total'])
        assert totals == {(('node', 'pve01'), ('origin', 'nr')): 4}

    def test_double_pin_counts_guests(self):
        pve = FakePve(
            qemu_guests=[
                {'vmid': 100, 'name': 'NR-one', 'status': 'running'},
                {'vmid': 101, 'name': 'NR-two', 'status': 'running'},
            ],
            qemu_configs={
                100: {'affinity': '2-3'},
                101: {'affinity': '3'},
            },
        )
        families = collect_families(pve)
        pinned = samples(families['pve_node_cpu_pinned'])
        assert pinned == {
            (('cpu', '2'), ('node', 'pve01'), ('origin', 'nr')): 1,
            (('cpu', '3'), ('node', 'pve01'), ('origin', 'nr')): 2,
        }
        # Total counts distinct CPUs, not pin instances.
        totals = samples(families['pve_node_cpu_pinned_total'])
        assert totals == {(('node', 'pve01'), ('origin', 'nr')): 2}

    def test_no_affinity_no_series(self):
        pve = FakePve(
            qemu_guests=[{'vmid': 100, 'name': 'NR-plain', 'status': 'running'}],
            qemu_configs={100: {'onboot': 1}},
        )
        families = collect_families(pve)
        assert families['pve_node_cpu_pinned'].samples == []
        assert families['pve_node_cpu_pinned_total'].samples == []

    def test_lxc_guests_skipped(self):
        pve = FakePve(
            qemu_guests=[],
            qemu_configs={},
            lxc_guests=[{'vmid': 200, 'name': 'NR-container', 'status': 'running'}],
            lxc_configs={200: {'affinity': '0-7', 'onboot': 1}},
        )
        families = collect_families(pve)
        assert families['pve_node_cpu_pinned'].samples == []
        assert families['pve_node_cpu_pinned_total'].samples == []

    def test_unpinned_guests_counted_per_origin(self):
        pve = FakePve(
            qemu_guests=[
                {'vmid': 100, 'name': 'NR-pinned', 'status': 'running'},
                {'vmid': 101, 'name': 'NR-floating', 'status': 'running'},
                {'vmid': 102, 'name': 'customer-a', 'status': 'running'},
                {'vmid': 103, 'name': 'customer-b', 'status': 'stopped'},
                {'vmid': 104, 'name': 'customer-pinned', 'status': 'running'},
            ],
            qemu_configs={
                100: {'affinity': '0-1'},
                101: {'onboot': 1},
                102: {'onboot': 1},
                103: {},
                104: {'affinity': '4'},
            },
        )
        unpinned = samples(collect_families(pve)['pve_node_guests_unpinned'])
        assert unpinned == {
            (('node', 'pve01'), ('origin', 'nr')): 1,
            (('node', 'pve01'), ('origin', 'foreign')): 2,
        }

    def test_unpinned_guests_zero_when_all_pinned(self):
        pve = FakePve(
            qemu_guests=[{'vmid': 100, 'name': 'NR-abc', 'status': 'running'}],
            qemu_configs={100: {'affinity': '0-3'}},
        )
        unpinned = samples(collect_families(pve)['pve_node_guests_unpinned'])
        # Explicit zeros: metric presence distinguishes a healthy node from
        # a disabled config collector.
        assert unpinned == {
            (('node', 'pve01'), ('origin', 'nr')): 0,
            (('node', 'pve01'), ('origin', 'foreign')): 0,
        }

    def test_unpinned_guests_zero_with_no_guests(self):
        pve = FakePve(qemu_guests=[], qemu_configs={})
        unpinned = samples(collect_families(pve)['pve_node_guests_unpinned'])
        assert unpinned == {
            (('node', 'pve01'), ('origin', 'nr')): 0,
            (('node', 'pve01'), ('origin', 'foreign')): 0,
        }

    def test_unpinned_guests_templates_excluded(self):
        pve = FakePve(
            qemu_guests=[
                {'vmid': 900, 'name': 'debian-12-template', 'status': 'stopped', 'template': 1},
                {'vmid': 101, 'name': 'customer-a', 'status': 'running'},
            ],
            qemu_configs={900: {'onboot': 0}, 101: {}},
        )
        unpinned = samples(collect_families(pve)['pve_node_guests_unpinned'])
        assert unpinned == {
            (('node', 'pve01'), ('origin', 'nr')): 0,
            (('node', 'pve01'), ('origin', 'foreign')): 1,
        }

    def test_pinned_templates_excluded(self):
        pve = FakePve(
            qemu_guests=[
                {'vmid': 900, 'name': 'tpl-with-affinity', 'status': 'stopped', 'template': 1},
                {'vmid': 100, 'name': 'NR-abc', 'status': 'running'},
            ],
            qemu_configs={
                900: {'affinity': '0-7'},
                100: {'affinity': '2'},
            },
        )
        families = collect_families(pve)
        pinned = samples(families['pve_node_cpu_pinned'])
        assert pinned == {
            (('cpu', '2'), ('node', 'pve01'), ('origin', 'nr')): 1,
        }

    def test_onboot_still_collected(self):
        pve = FakePve(
            qemu_guests=[{'vmid': 100, 'name': 'NR-abc', 'status': 'running'}],
            qemu_configs={100: {'onboot': 1, 'affinity': '0'}},
            lxc_guests=[{'vmid': 200, 'name': 'ct', 'status': 'running'}],
            lxc_configs={200: {'onboot': 0}},
        )
        onboot = samples(collect_families(pve)['pve_onboot_status'])
        assert onboot == {
            (('id', 'qemu/100'), ('node', 'pve01'), ('type', 'qemu')): 1,
            (('id', 'lxc/200'), ('node', 'pve01'), ('type', 'lxc')): 0,
        }

    def test_collects_pins_from_all_cluster_nodes(self):
        """One Alloy scrape of any API member must still see peer-node pins."""
        pve = FakePve(
            qemu_guests=[
                {'vmid': 100, 'name': 'NR-local', 'status': 'running'},
            ],
            qemu_configs={100: {'affinity': '0-1'}},
            remote_nodes={
                'pve02': FakeNode(
                    qemu_guests=[
                        {'vmid': 200, 'name': 'customer-vm', 'status': 'running'},
                    ],
                    qemu_configs={200: {'affinity': '2-3'}},
                    lxc_guests=[],
                    lxc_configs={},
                ),
            },
        )
        families = collect_families(pve)
        pinned = samples(families['pve_node_cpu_pinned'])
        assert pinned == {
            (('cpu', '0'), ('node', 'pve01'), ('origin', 'nr')): 1,
            (('cpu', '1'), ('node', 'pve01'), ('origin', 'nr')): 1,
            (('cpu', '2'), ('node', 'pve02'), ('origin', 'foreign')): 1,
            (('cpu', '3'), ('node', 'pve02'), ('origin', 'foreign')): 1,
        }
        unpinned = samples(families['pve_node_guests_unpinned'])
        assert unpinned == {
            (('node', 'pve01'), ('origin', 'nr')): 0,
            (('node', 'pve01'), ('origin', 'foreign')): 0,
            (('node', 'pve02'), ('origin', 'nr')): 0,
            (('node', 'pve02'), ('origin', 'foreign')): 0,
        }


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__]))
