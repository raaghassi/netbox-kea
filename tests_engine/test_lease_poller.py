import unittest
from unittest.mock import Mock

from keasync.lease_poller import LeasePoller, PAGE_LIMIT


def _lease(ip, state=0, hostname='', mac='11:22:33:44:55:66'):
    return {'ip-address': ip, 'state': state, 'hostname': hostname,
            'hw-address': mac, 'subnet-id': 1, 'valid-lft': 4000}


def _nb_ip(address, description='', dns_name=''):
    obj = Mock()
    obj.address = address
    obj.description = description
    obj.dns_name = dns_name
    return obj


class TestLeasePoller(unittest.TestCase):

    def setUp(self):
        self.nb = Mock()
        self.p = LeasePoller('narwhal', 'http://kea:8000/', self.nb)
        self.p.api = Mock()

    def test_01_fetch_pages_chain_from_last_address(self):
        full = [_lease(f'10.0.{i // 256}.{i % 256}')
                for i in range(PAGE_LIMIT)]
        partial = [_lease('10.0.4.1')]
        self.p.api.get_leases_page.side_effect = [full, partial]
        leases = self.p.fetch_leases()
        self.assertEqual(len(leases), PAGE_LIMIT + 1)
        calls = self.p.api.get_leases_page.call_args_list
        self.assertEqual(calls[0].args, ('start', PAGE_LIMIT))
        self.assertEqual(
            calls[1].args, (full[-1]['ip-address'], PAGE_LIMIT))

    def test_02_reflects_active_leases_only(self):
        # state 0 = held by a client; 1 declined, 2 expired-reclaimed,
        # 3 released must not be reflected
        self.p.api.get_leases_page.side_effect = [[
            _lease('10.0.0.5', hostname='pc.lan.'),
            _lease('10.0.0.6', state=1),
            _lease('10.0.0.7', state=2),
            _lease('10.0.0.8', state=3)]]
        self.nb.dhcp_ips.return_value = []
        self.p.sync_once()
        self.nb.upsert_dhcp_ip.assert_called_once_with(
            '10.0.0.5/32', dns_name='pc.lan',
            description='kea lease [narwhal] (mac 11:22:33:44:55:66)')

    def test_03_removes_stale_own_records_only(self):
        self.p.api.get_leases_page.side_effect = [[]]
        stale = _nb_ip('10.0.0.9/32', 'kea lease [narwhal]')
        push_path = _nb_ip('10.0.0.8/32', 'kea lease (mac aa:bb)')
        other_src = _nb_ip('10.0.0.7/32', 'kea lease [svcs]')
        self.nb.dhcp_ips.return_value = [stale, push_path, other_src]
        self.p.sync_once()
        stale.delete.assert_called_once_with()
        push_path.delete.assert_not_called()
        other_src.delete.assert_not_called()
        self.nb.upsert_dhcp_ip.assert_not_called()

    def test_04_unchanged_lease_writes_nothing(self):
        self.p.api.get_leases_page.side_effect = [[
            _lease('10.0.0.5', hostname='pc.lan')]]
        current = _nb_ip(
            '10.0.0.5/32',
            'kea lease [narwhal] (mac 11:22:33:44:55:66)', 'pc.lan')
        self.nb.dhcp_ips.return_value = [current]
        self.p.sync_once()
        self.nb.upsert_dhcp_ip.assert_not_called()
        current.delete.assert_not_called()

    def test_05_changed_hostname_reupserts(self):
        self.p.api.get_leases_page.side_effect = [[
            _lease('10.0.0.5', hostname='newname.lan')]]
        current = _nb_ip(
            '10.0.0.5/32',
            'kea lease [narwhal] (mac 11:22:33:44:55:66)', 'pc.lan')
        self.nb.dhcp_ips.return_value = [current]
        self.p.sync_once()
        self.nb.upsert_dhcp_ip.assert_called_once()
        current.delete.assert_not_called()
