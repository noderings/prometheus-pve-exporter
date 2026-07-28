"""
Tests for ClusterResourcesCollector non_nr_* aggregation.
"""

import pytest

from pve_exporter.collector.cluster import ClusterResourcesCollector


class FakeEndpoint:
    def __init__(self, value):
        self._value = value

    def get(self, **_kwargs):
        return self._value


class FakePve:
    def __init__(self, resources):
        self.cluster = type('Cluster', (), {})()
        self.cluster.resources = FakeEndpoint(resources)


def collect_families(pve):
    collector = ClusterResourcesCollector(pve)
    return {family.name: family for family in collector.collect()}


def samples(family):
    return {tuple(sorted(sample.labels.items())): sample.value
            for sample in family.samples}


class TestNonNrAggregation:
    def test_templates_excluded_from_cpu_mem_but_included_in_disk(self):
        pve = FakePve([
            {
                'type': 'qemu',
                'id': 'qemu/900',
                'name': 'debian-12-template',
                'node': 'pve01',
                'template': 1,
                'maxcpu': 4,
                'maxmem': 8 * 1024**3,
                'maxdisk': 32 * 1024**3,
                'disk': 0,
                'tags': '',
            },
            {
                'type': 'qemu',
                'id': 'qemu/101',
                'name': 'customer-a',
                'node': 'pve01',
                'template': 0,
                'maxcpu': 2,
                'maxmem': 4 * 1024**3,
                'maxdisk': 20 * 1024**3,
                'disk': 5 * 1024**3,
                'tags': '',
            },
            {
                'type': 'qemu',
                'id': 'qemu/200',
                'name': 'NR-abc',
                'node': 'pve01',
                'template': 0,
                'maxcpu': 8,
                'maxmem': 16 * 1024**3,
                'maxdisk': 100 * 1024**3,
                'disk': 10 * 1024**3,
                'tags': '',
            },
        ])
        families = collect_families(pve)

        assert samples(families['non_nr_pve_cpu_usage_limit']) == {
            (('node', 'pve01'),): 2.0,
        }
        assert samples(families['non_nr_pve_memory_size_bytes']) == {
            (('node', 'pve01'),): float(4 * 1024**3),
        }
        # Template disk is included even though its CPU/memory are not.
        assert samples(families['non_nr_pve_disk_size_bytes']) == {
            (('node', 'pve01'),): float((32 + 20) * 1024**3),
        }
        assert samples(families['non_nr_pve_disk_usage_bytes']) == {
            (('node', 'pve01'),): float(5 * 1024**3),
        }

    def test_only_templates_counts_disk_not_cpu_mem(self):
        pve = FakePve([
            {
                'type': 'qemu',
                'id': 'qemu/900',
                'name': 'tpl',
                'node': 'pve01',
                'template': 1,
                'maxcpu': 13,
                'maxmem': 13 * 1024**3,
                'maxdisk': 101 * 1024**3,
                'disk': 24 * 1024**3,
                'tags': '',
            },
        ])
        families = collect_families(pve)
        assert families['non_nr_pve_cpu_usage_limit'].samples == []
        assert families['non_nr_pve_memory_size_bytes'].samples == []
        assert samples(families['non_nr_pve_disk_size_bytes']) == {
            (('node', 'pve01'),): float(101 * 1024**3),
        }
        assert samples(families['non_nr_pve_disk_usage_bytes']) == {
            (('node', 'pve01'),): float(24 * 1024**3),
        }


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__]))
