import unittest
from ipaddress import IPv4Address
from unittest.mock import Mock

from keasync.kea.api import DHCP4API
from keasync.kea.cb import DHCP4CB
from keasync.kea.exceptions import KeaCmdError, KeaError

MAC = bytes.fromhex('112233445566')


def _ip(dotted):
    return int(IPv4Address(dotted))


class _FakeCursor:
    """Sequences one fetchall/fetchone result set per execute() call."""

    def __init__(self, results):
        self._results = results
        self.executed = []
        self._i = -1

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._i += 1

    def fetchall(self):
        return self._results[self._i]

    def fetchone(self):
        return self._results[self._i][0]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestDHCP4CBReservations(unittest.TestCase):

    def setUp(self):
        self.kea = DHCP4CB('dbname=kea', api_url='http://kea:8000/')
        self.kea.api = Mock()

    def test_01_no_api_reservation_is_noop(self):
        kea = DHCP4CB('dbname=kea')
        # conf is None: would raise if the inherited in-memory logic ran
        kea.set_reservation(100, 200, {
            'ip-address': '10.0.0.5', 'hw-address': '11:22:33:44:55:66'})
        kea.del_resa(200)

    def test_02_set_reservation_stages_in_memory(self):
        self.kea.conf = {'subnet4': []}
        self.kea.auto_commit = False
        self.kea.ip_uniqueness = True
        self.kea.set_subnet(100, {'subnet': '10.0.0.0/24'})
        self.kea.set_reservation(100, 200, {
            'ip-address': '10.0.0.5', 'hw-address': '11:22:33:44:55:66',
            'hostname': 'pc.lan'})
        resas = self.kea.conf['subnet4'][0]['reservations']
        self.assertEqual(len(resas), 1)
        self.assertEqual(
            resas[0]['user-context']['netbox_ip_address_id'], 200)

    def test_03_pull_loads_reservations_from_hosts_table(self):
        cursor = _FakeCursor([
            [(100, '10.0.0.0/24', None, None, None, None, None, None, None)],
            [],   # pools
            [],   # options
            [(100, _ip('10.0.0.5'), 'pc.lan',
              '{"netbox_ip_address_id": 200}', MAC, 0)],
        ])
        self.kea._connect = lambda: _FakeConn(cursor)
        self.kea.pull()
        resas = self.kea.conf['subnet4'][0]['reservations']
        self.assertEqual(resas, [{
            'ip-address': '10.0.0.5', 'hostname': 'pc.lan',
            'hw-address': '11:22:33:44:55:66',
            'user-context': {'netbox_ip_address_id': 200}}])

    def test_04_reconcile_add_del_change_foreign(self):
        hosts_rows = [
            # changed: netbox id 200 now wants 10.0.0.5
            (100, _ip('10.0.0.4'), 'pc.lan',
             '{"netbox_ip_address_id": 200}', MAC, 0),
            # stale: netbox id 201 no longer desired
            (100, _ip('10.0.0.9'), None,
             '{"netbox_ip_address_id": 201}', MAC, 0),
            # foreign row: no netbox marker
            (100, _ip('10.0.0.7'), 'alien', None, MAC, 0),
        ]
        cursor = _FakeCursor([hosts_rows])
        self.kea._connect = lambda: _FakeConn(cursor)
        desired = [{
            'id': 100, 'subnet': '10.0.0.0/24', 'pools': [],
            'reservations': [
                {'ip-address': '10.0.0.5', 'hostname': 'pc.lan',
                 'hw-address': '11:22:33:44:55:66',
                 'user-context': {'netbox_ip_address_id': 200}},
                # collides with the foreign row: must be skipped
                {'ip-address': '10.0.0.7', 'hostname': 'clash.lan',
                 'hw-address': '66:55:44:33:22:11',
                 'user-context': {'netbox_ip_address_id': 202}},
            ]}]
        self.kea._reconcile_reservations(desired)
        deleted = {c.args for c in self.kea.api.del_reservation.call_args_list}
        self.assertEqual(deleted, {(100, '10.0.0.4'), (100, '10.0.0.9')})
        self.kea.api.add_reservation.assert_called_once()
        payload = self.kea.api.add_reservation.call_args.args[0]
        self.assertEqual(payload['subnet-id'], 100)
        self.assertEqual(payload['ip-address'], '10.0.0.5')
        self.assertEqual(
            payload['user-context'], {'netbox_ip_address_id': 200})

    def test_05_reconcile_noop_when_converged(self):
        hosts_rows = [(100, _ip('10.0.0.5'), 'pc.lan',
                       '{"netbox_ip_address_id": 200}', MAC, 0)]
        cursor = _FakeCursor([hosts_rows])
        self.kea._connect = lambda: _FakeConn(cursor)
        desired = [{
            'id': 100, 'subnet': '10.0.0.0/24', 'pools': [],
            'reservations': [
                {'ip-address': '10.0.0.5', 'hostname': 'pc.lan',
                 'hw-address': '11:22:33:44:55:66',
                 'user-context': {'netbox_ip_address_id': 200}}]}]
        self.kea._reconcile_reservations(desired)
        self.kea.api.del_reservation.assert_not_called()
        self.kea.api.add_reservation.assert_not_called()


class TestDHCP4CBStagedMutations(unittest.TestCase):
    """Check-mode safety: staging reservation changes must not touch the
    live server; evictions and API writes happen only at push()."""

    def setUp(self):
        self.kea = DHCP4CB('dbname=kea', api_url='http://kea:8000/')
        self.kea.api = Mock()
        self.kea.ip_uniqueness = True
        self.kea.auto_commit = False

    def _conf_with_resa(self, mac='aa:bb:cc:44:55:66'):
        self.kea.conf = {'subnet4': [{
            'id': 100, 'subnet': '10.0.0.0/24', 'pools': [],
            'reservations': [{
                'ip-address': '10.0.0.5', 'hw-address': mac,
                'user-context': {'netbox_ip_address_id': 200}}]}]}

    def test_01_mac_change_eviction_deferred(self):
        self._conf_with_resa()
        self.kea.set_reservation(100, 200, {
            'ip-address': '10.0.0.5', 'hw-address': '11:22:33:44:55:66',
            'hostname': 'pc.lan'})
        self.kea.api.del_lease4.assert_not_called()
        self.assertEqual(self.kea._pending_lease_dels, {'10.0.0.5'})

    def test_02_uppercase_mac_is_normalized_not_evicted(self):
        self._conf_with_resa()
        self.kea.set_reservation(100, 200, {
            'ip-address': '10.0.0.5', 'hw-address': 'AA:BB:CC:44:55:66',
            'hostname': 'pc.lan'})
        # same MAC, different case: no eviction, stored lowercase
        self.assertEqual(self.kea._pending_lease_dels, set())
        resa = self.kea.conf['subnet4'][0]['reservations'][0]
        self.assertEqual(resa['hw-address'], 'aa:bb:cc:44:55:66')

    def test_03_pull_discards_staged_evictions(self):
        self.kea._pending_lease_dels.add('10.0.0.5')
        cursor = _FakeCursor([
            [(100, '10.0.0.0/24', None, None, None, None, None, None,
              None)], [], [], []])
        self.kea._connect = lambda: _FakeConn(cursor)
        self.kea.pull()
        self.assertEqual(self.kea._pending_lease_dels, set())

    def test_04_push_reconciles_and_flushes_evictions(self):
        # execute order in push(): audit revision, server-id fetchone,
        # subnet_id fetchall, subnet upsert x4 statements, hosts SELECT
        cursor = _FakeCursor([[], [(1,)], [], [], [], [], [], [], []])
        self.kea._connect = lambda: _FakeConn(cursor)
        self.kea.commit_conf = {'subnet4': [{
            'id': 100, 'subnet': '10.0.0.0/24', 'pools': [],
            'reservations': [{
                'ip-address': '10.0.0.5', 'hw-address': '11:22:33:44:55:66',
                'hostname': 'pc.lan',
                'user-context': {'netbox_ip_address_id': 200}}]}]}
        self.kea._has_commit = True
        self.kea._pending_lease_dels.add('10.0.0.4')
        # _pulled_cb_key None (no pull) -> validation runs (fail-safe)
        self.kea.api.config_backend_pull.return_value = True
        self.kea.api.subnet4_list.return_value = [
            {'id': 100, 'subnet': '10.0.0.0/24'}]
        self.kea.push()
        self.kea.api.add_reservation.assert_called_once()
        self.kea.api.del_lease4.assert_called_once_with('10.0.0.4')
        self.kea.api.config_backend_pull.assert_called_once_with()
        self.assertEqual(self.kea._pending_lease_dels, set())

    def test_06_unchanged_cb_push_skips_forced_pull(self):
        # when the CB content matches the last pull, push must NOT force a
        # config-backend-pull (reservation-only / no-op sync)
        subnet = {'id': 100, 'subnet': '10.0.0.0/24', 'pools': [],
                  'reservations': []}
        cursor = _FakeCursor([[], [(1,)], [], [], [], [], [], [], []])
        self.kea._connect = lambda: _FakeConn(cursor)
        self.kea.commit_conf = {'subnet4': [subnet]}
        self.kea._pulled_cb_key = DHCP4CB._cb_content_key([subnet])
        self.kea._has_commit = True
        self.kea.push()
        self.kea.api.config_backend_pull.assert_not_called()

    def test_05_push_without_api_skips_reconcile(self):
        kea = DHCP4CB('dbname=kea')
        cursor = _FakeCursor([[], [(1,)], [], [], [], [], [], []])
        kea._connect = lambda: _FakeConn(cursor)
        kea.commit_conf = {'subnet4': [{
            'id': 100, 'subnet': '10.0.0.0/24', 'pools': []}]}
        kea._has_commit = True
        kea.push()
        # subnet SQL only: no hosts SELECT after the 8 push statements
        self.assertEqual(len(cursor.executed), 8)


class TestDHCP4CBForeignBlocking(unittest.TestCase):

    def setUp(self):
        self.kea = DHCP4CB('dbname=kea', api_url='http://kea:8000/')
        self.kea.api = Mock()

    def test_01_move_to_foreign_held_ip_keeps_old_row(self):
        hosts_rows = [
            (100, _ip('10.0.0.4'), 'pc.lan',
             '{"netbox_ip_address_id": 200}', MAC, 0),
            (100, _ip('10.0.0.7'), 'alien', None, MAC, 0),
        ]
        cursor = _FakeCursor([hosts_rows])
        self.kea._connect = lambda: _FakeConn(cursor)
        desired = [{
            'id': 100, 'subnet': '10.0.0.0/24', 'pools': [],
            'reservations': [
                {'ip-address': '10.0.0.7', 'hostname': 'pc.lan',
                 'hw-address': '11:22:33:44:55:66',
                 'user-context': {'netbox_ip_address_id': 200}}]}]
        self.kea._reconcile_reservations(desired)
        # the old, working reservation survives; nothing added
        self.kea.api.del_reservation.assert_not_called()
        self.kea.api.add_reservation.assert_not_called()

    def test_02_mac_case_difference_is_not_a_change(self):
        hosts_rows = [(100, _ip('10.0.0.5'), 'pc.lan',
                       '{"netbox_ip_address_id": 200}', MAC, 0)]
        cursor = _FakeCursor([hosts_rows])
        self.kea._connect = lambda: _FakeConn(cursor)
        desired = [{
            'id': 100, 'subnet': '10.0.0.0/24', 'pools': [],
            'reservations': [
                {'ip-address': '10.0.0.5', 'hostname': 'pc.lan',
                 'hw-address': '11:22:33:44:55:66'.upper(),
                 'user-context': {'netbox_ip_address_id': 200}}]}]
        self.kea._reconcile_reservations(desired)
        self.kea.api.del_reservation.assert_not_called()
        self.kea.api.add_reservation.assert_not_called()


class TestDHCP4APIReservationCommands(unittest.TestCase):

    def setUp(self):
        self.api = DHCP4API('http://kea:8000/')
        self.api.session = Mock()

    def _respond(self, result, text):
        resp = Mock()
        resp.raise_for_status = Mock()
        resp.json.return_value = [{'result': result, 'text': text}]
        self.api.session.post.return_value = resp

    def test_01_add_reservation_targets_database(self):
        self.api._request_kea = Mock()
        self.api.add_reservation({'subnet-id': 100, 'ip-address': '10.0.0.5'})
        self.api._request_kea.assert_called_once_with(
            'reservation-add', {
                'reservation': {'subnet-id': 100, 'ip-address': '10.0.0.5'},
                'operation-target': 'database'})

    def test_02_del_reservation_tolerates_not_found(self):
        self._respond(1, 'Host not deleted (not found).')
        self.api.del_reservation(100, '10.0.0.5')
        args = self.api.session.post.call_args
        self.assertEqual(args.kwargs['json']['command'], 'reservation-del')
        self.assertEqual(
            args.kwargs['json']['arguments'],
            {'subnet-id': 100, 'ip-address': '10.0.0.5',
             'operation-target': 'database'})

    def test_03_del_reservation_raises_on_real_error(self):
        self._respond(
            1, 'Unable to delete a host because there is no hosts-database '
               'configured.')
        with self.assertRaises(KeaCmdError):
            self.api.del_reservation(100, '10.0.0.5')

    def test_04_del_reservation_ok(self):
        self._respond(0, 'Host deleted.')
        self.api.del_reservation(100, '10.0.0.5')

    def test_07_get_leases_page_ok(self):
        resp = Mock()
        resp.raise_for_status = Mock()
        resp.json.return_value = [{
            'result': 0, 'text': '1 IPv4 lease(s) found.',
            'arguments': {'leases': [{'ip-address': '10.0.0.5',
                                      'state': 0}]}}]
        self.api.session.post.return_value = resp
        leases = self.api.get_leases_page('start', 1024)
        self.assertEqual(leases, [{'ip-address': '10.0.0.5', 'state': 0}])
        args = self.api.session.post.call_args
        self.assertEqual(args.kwargs['json']['command'], 'lease4-get-page')
        self.assertEqual(args.kwargs['json']['arguments'],
                         {'from': 'start', 'limit': 1024})

    def test_08_get_leases_page_empty_result3(self):
        self._respond(3, '0 IPv4 lease(s) found.')
        self.assertEqual(self.api.get_leases_page('start', 1024), [])

    def test_10_config_backend_pull_ok_returns_true(self):
        self._respond(0, 'On demand configuration update successful.')
        self.assertIs(self.api.config_backend_pull(), True)
        self.assertEqual(
            self.api.session.post.call_args.kwargs['json']['command'],
            'config-backend-pull')

    def test_11_config_backend_pull_no_cb_returns_false(self):
        self._respond(3, 'No config backend.')
        self.assertIs(self.api.config_backend_pull(), False)

    def test_12_config_backend_pull_failure_raises(self):
        self._respond(1, 'unable to fetch: bad subnet')
        with self.assertRaises(KeaCmdError):
            self.api.config_backend_pull()

    def test_15_malformed_response_raises_keaerror(self):
        # a response array not of length 1 must raise a KeaError subclass,
        # not a bare AssertionError that would escape uncaught
        resp = Mock()
        resp.raise_for_status = Mock()
        resp.json.return_value = []  # empty / unexpected
        self.api.session.post.return_value = resp
        with self.assertRaises(KeaError):
            self.api.config_backend_pull()

    def test_13_subnet4_list_ok(self):
        resp = Mock()
        resp.raise_for_status = Mock()
        resp.json.return_value = [{
            'result': 0, 'text': '1 IPv4 subnets found',
            'arguments': {'subnets': [{'id': 4, 'subnet': '172.16.24.0/23'}]}}]
        self.api.session.post.return_value = resp
        self.assertEqual(self.api.subnet4_list(),
                         [{'id': 4, 'subnet': '172.16.24.0/23'}])

    def test_14_subnet4_list_empty_result3(self):
        self._respond(3, '0 IPv4 subnets found')
        self.assertEqual(self.api.subnet4_list(), [])

    def test_09_get_leases_page_error_raises(self):
        self._respond(1, 'malformed')
        with self.assertRaises(KeaCmdError):
            self.api.get_leases_page('start', 1024)

    def test_05_basic_auth_set_on_session(self):
        api = DHCP4API('http://kea:8000/', username='syncer',
                       password='s3cret')
        self.assertEqual(api.session.auth, ('syncer', 's3cret'))

    def test_06_no_auth_by_default(self):
        api = DHCP4API('http://kea:8000/')
        self.assertIsNone(api.session.auth)


class TestDHCP4CBValidateApplied(unittest.TestCase):

    def setUp(self):
        self.kea = DHCP4CB('dbname=kea', api_url='http://kea:8000/')
        self.kea.api = Mock()
        self.kea.api.config_backend_pull.return_value = True  # applied
        self.desired = [{'id': 4, 'subnet': '172.16.24.0/23', 'pools': []}]

    def test_01_pull_ok_and_served_matches_no_error(self):
        self.kea.api.subnet4_list.return_value = [
            {'id': 4, 'subnet': '172.16.24.0/23'}]
        with self.assertNoLogs(level='ERROR'):
            self.kea._validate_applied(self.desired)
        self.kea.api.config_backend_pull.assert_called_once_with()

    def test_02_pull_failure_logs_and_skips_readback(self):
        self.kea.api.config_backend_pull.side_effect = KeaCmdError('bad row')
        with self.assertLogs(level='ERROR') as cm:
            self.kea._validate_applied(self.desired)
        self.assertIn('NOT applied', ' '.join(cm.output))
        self.kea.api.subnet4_list.assert_not_called()

    def test_02b_no_cb_backend_one_error_not_per_subnet_flood(self):
        # config-backend-pull result 3 (no CB) -> one clear error, no diff
        self.kea.api.config_backend_pull.return_value = False
        with self.assertLogs(level='ERROR') as cm:
            self.kea._validate_applied(self.desired)
        self.assertEqual(len(cm.output), 1)
        self.assertIn('no config backend', ' '.join(cm.output))
        self.kea.api.subnet4_list.assert_not_called()

    def test_05_noncanonical_cidr_not_flagged_divergent(self):
        # Kea renders canonical CIDR; a non-canonical desired string must
        # not read as divergence after normalization
        self.kea.api.config_backend_pull.return_value = True
        self.kea.api.subnet4_list.return_value = [
            {'id': 4, 'subnet': '172.16.24.0/23'}]
        desired = [{'id': 4, 'subnet': '172.16.24.5/23', 'pools': []}]
        with self.assertNoLogs(level='ERROR'):
            self.kea._validate_applied(desired)

    def test_03_missing_desired_subnet_logs_divergence(self):
        self.kea.api.subnet4_list.return_value = []  # Kea serves nothing
        with self.assertLogs(level='ERROR') as cm:
            self.kea._validate_applied(self.desired)
        self.assertIn('divergence', ' '.join(cm.output))
        self.assertIn('id=4', ' '.join(cm.output))

    def test_04_unexpected_extra_subnet_logs_divergence(self):
        self.kea.api.subnet4_list.return_value = [
            {'id': 4, 'subnet': '172.16.24.0/23'},
            {'id': 9, 'subnet': '10.9.0.0/24'}]  # stale, not desired
        with self.assertLogs(level='ERROR') as cm:
            self.kea._validate_applied(self.desired)
        self.assertIn('id=9', ' '.join(cm.output))
