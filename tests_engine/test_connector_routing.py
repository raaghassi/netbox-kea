import unittest
from unittest.mock import Mock

from pynetbox.models.ipam import Prefixes

from keasync.connector import Connector
from keasync.kea.exceptions import KeaCmdError

api = Mock(base_url='http://netbox')


def _prefix(id_, prefix, tag=...):
    """Build a pynetbox Prefixes record; tag=... omits the custom field."""
    data = {'id': id_, 'prefix': prefix, 'display': prefix,
            'url': f'http://netbox/api/ipam/prefixes/{id_}/',
            'custom_fields': {} if tag is ... else {'kea_server': tag}}
    return Prefixes(data, api, None)


class TestConnectorRouting(unittest.TestCase):

    def setUp(self):
        self.nb = Mock()
        self.kea_svcs = Mock()
        self.kea_site = Mock()
        self.conn = Connector(
            self.nb, {'svcs': self.kea_svcs, 'site-a': self.kea_site},
            {}, {}, {'hw-address': ['assigned_object.mac_address'],
                     'hostname': ['dns_name']},
            default_tag='svcs')
        # No pools/reservations under test prefixes
        self.nb.ip_addresses.side_effect = lambda **kw: iter([])
        self.nb.ip_ranges.side_effect = lambda **kw: iter([])

    def test_01_untagged_prefix_routes_to_default(self):
        self.nb.prefix.return_value = _prefix(100, '10.0.0.0/24')
        self.conn.sync_prefix(100)
        self.kea_svcs.update_subnet.assert_called_once_with(
            100, {'subnet': '10.0.0.0/24'})
        self.kea_site.update_subnet.assert_not_called()

    def test_02_null_custom_field_routes_to_default(self):
        self.nb.prefix.return_value = _prefix(100, '10.0.0.0/24', tag=None)
        self.conn.sync_prefix(100)
        self.kea_svcs.update_subnet.assert_called_once_with(
            100, {'subnet': '10.0.0.0/24'})
        self.kea_site.update_subnet.assert_not_called()

    def test_03_tagged_prefix_routes_to_its_backend(self):
        self.nb.prefix.return_value = _prefix(101, '10.1.0.0/24', 'site-a')
        self.conn.sync_prefix(101)
        self.kea_site.update_subnet.assert_called_once_with(
            101, {'subnet': '10.1.0.0/24'})
        self.kea_svcs.update_subnet.assert_not_called()

    def test_04_tag_is_case_insensitive(self):
        self.nb.prefix.return_value = _prefix(101, '10.1.0.0/24', ' Site-A ')
        self.conn.sync_prefix(101)
        self.kea_site.update_subnet.assert_called_once()

    def test_05_unknown_tag_skips_prefix(self):
        self.nb.prefix.return_value = _prefix(102, '10.2.0.0/24', 'ctrl')
        self.conn.sync_prefix(102)
        self.kea_svcs.update_subnet.assert_not_called()
        self.kea_svcs.set_subnet.assert_not_called()
        self.kea_site.update_subnet.assert_not_called()
        self.kea_site.set_subnet.assert_not_called()

    def test_06_deletions_broadcast_to_all_backends(self):
        self.nb.prefix.return_value = None
        self.nb.ip_range.return_value = None
        self.nb.ip_address.side_effect = None
        self.nb.ip_address.return_value = None
        self.conn.sync_prefix(9)
        self.conn.sync_iprange(8)
        self.conn.sync_ipaddress(7)
        for kea in (self.kea_svcs, self.kea_site):
            kea.del_subnet.assert_called_once_with(9)
            kea.del_pool.assert_called_once_with(8)
            kea.del_resa.assert_called_once_with(7)

    def test_07_sync_all_routes_and_pushes_all(self):
        self.nb.all_prefixes.return_value = iter([
            _prefix(100, '10.0.0.0/24'),
            _prefix(101, '10.1.0.0/24', 'site-a')])
        self.conn.sync_all()
        for kea in (self.kea_svcs, self.kea_site):
            kea.pull.assert_called_once_with()
            kea.del_all_subnets.assert_called_once_with()
            kea.push.assert_called_once_with()
        self.kea_svcs.set_subnet.assert_called_once_with(
            100, {'subnet': '10.0.0.0/24'})
        self.kea_site.set_subnet.assert_called_once_with(
            101, {'subnet': '10.1.0.0/24'})

    def test_08_reload_pulls_all_backends(self):
        self.conn.reload_dhcp_config()
        self.kea_svcs.pull.assert_called_once_with()
        self.kea_site.pull.assert_called_once_with()

    def test_09_sync_all_unknown_tag_aborts_push(self):
        # An unknown-tag prefix during full sync means pushing would drop
        # it from live DHCP: no backend may be pushed, staged wipe discarded
        self.nb.all_prefixes.return_value = iter([
            _prefix(100, '10.0.0.0/24'),
            _prefix(102, '10.2.0.0/24', 'ctrl')])
        self.conn.sync_all()
        for kea in (self.kea_svcs, self.kea_site):
            kea.push.assert_not_called()
            self.assertEqual(kea.pull.call_count, 2)  # initial + rollback

    def test_10_sync_all_zero_prefix_backend_still_syncs_empty(self):
        # A backend legitimately owning no prefixes converges to empty:
        # declarative semantics, provided no tags are unknown
        self.nb.all_prefixes.return_value = iter([
            _prefix(100, '10.0.0.0/24')])
        self.conn.sync_all()
        self.kea_site.del_all_subnets.assert_called_once_with()
        self.kea_site.set_subnet.assert_not_called()
        self.kea_site.push.assert_called_once_with()
        self.kea_svcs.push.assert_called_once_with()

    def test_11_sync_all_backend_with_all_failures_not_pushed(self):
        # Per-backend all-failed guard: the healthy backend still pushes,
        # the failing one is rolled back instead of being wiped
        self.kea_site.set_subnet.side_effect = KeaCmdError('boom')
        self.nb.all_prefixes.return_value = iter([
            _prefix(100, '10.0.0.0/24'),
            _prefix(101, '10.1.0.0/24', 'site-a')])
        self.conn.sync_all()
        self.kea_svcs.push.assert_called_once_with()
        self.kea_site.push.assert_not_called()
        self.assertEqual(self.kea_site.pull.call_count, 2)  # + rollback


class TestConnectorLegacyMode(unittest.TestCase):
    """A single (non-dict) backend keeps pre-registry behavior: the custom
    field is ignored and everything syncs to the sole backend."""

    def setUp(self):
        self.nb = Mock()
        self.kea = Mock()
        self.conn = Connector(self.nb, self.kea, {}, {}, {
            'hw-address': ['assigned_object.mac_address'],
            'hostname': ['dns_name']})
        self.nb.ip_addresses.side_effect = lambda **kw: iter([])
        self.nb.ip_ranges.side_effect = lambda **kw: iter([])

    def test_01_tagged_prefix_still_routes_to_sole_backend(self):
        self.nb.prefix.return_value = _prefix(101, '10.1.0.0/24', 'site-a')
        self.conn.sync_prefix(101)
        self.kea.update_subnet.assert_called_once_with(
            101, {'subnet': '10.1.0.0/24'})

    def test_02_deletion_hits_sole_backend(self):
        self.nb.prefix.return_value = None
        self.conn.sync_prefix(9)
        self.kea.del_subnet.assert_called_once_with(9)
