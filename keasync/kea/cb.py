"""Kea Config Backend writer.

Writes subnets / pools / options directly to Kea's PostgreSQL config backend
(the same DB Kea uses for leases) following Kea's own audit protocol:

    createAuditRevisionDHCP4(now(), 'all', msg, true)   -- opens an audit revision
    INSERT/UPDATE dhcp4_subnet / dhcp4_pool / dhcp4_options / dhcp4_subnet_server
    -- the table AINS/AUPD/ADEL triggers create dhcp4_audit entries automatically

so Kea's `config-fetch` picks the changes up on its timer (kea pulls; no push to
a control agent, self-healing across restarts).

This reuses DHCP4App's in-memory subnet/pool manipulation verbatim; only
pull()/commit()/push() touch the database. Host reservations live in the Kea
*host* backend (hosts table), not the config backend: when api_url is set
they are read back by SQL and written through the instance's host_cmds API
(reservation-add/-del, operation-target database), and stale leases are
evicted via lease_cmds. Requires the Kea instance to load
libdhcp_host_cmds.so and configure a hosts-database on the same DB.
"""

import json
import logging
from copy import deepcopy
from ipaddress import ip_address

import psycopg  # psycopg 3 (present in the NetBox runtime image)
from psycopg.types.json import Json

from .api import DHCP4API
from .app import (DHCP4App, SUBNETS, POOLS, RESAS, USR_CTX, PREFIX, IP_RANGE,
                  IP_ADDR)
from .exceptions import KeaError

# DHCPv4 option name -> code. Extend alongside the syncer's subnet_prefix_map.
OPTION_CODES = {
    'routers': 3,
    'time-servers': 4,
    'domain-name-servers': 6,
    'ntp-servers': 42,
    'domain-search': 119,
}
CODE_NAMES = {code: name for name, code in OPTION_CODES.items()}

# Kea subnet scalar key -> dhcp4_subnet column (the subset the syncer emits).
SCALAR_COLS = {
    'valid-lifetime': 'valid_lifetime',
    'next-server': 'next_server',
    'boot-file-name': 'boot_file_name',
    'ddns-qualifying-suffix': 'ddns_qualifying_suffix',
    'ddns-send-updates': 'ddns_send_updates',
    'ddns-override-no-update': 'ddns_override_no_update',
    'ddns-override-client-update': 'ddns_override_client_update',
}
COL_KEYS = {col: key for key, col in SCALAR_COLS.items()}

# Config associated with the "all" server applies to every Kea instance; the
# audit revision is opened against the same tag. Permanently correct here:
# by design each Kea instance owns its own single-tenant CB database.
SERVER_TAG = 'all'

# host_identifier_type seed row for hw-address (dhcpdb_create.pgsql:
# INSERT INTO host_identifier_type VALUES (0, 'hw-address'))
HW_ADDRESS_TYPE = 0


class DHCP4CB(DHCP4App):

    def __init__(self, dsn, api_url=None, api_username=None,
                 api_password=None):
        # libpq DSN, e.g. "host=kea-leases-rw.kea.svc dbname=kea user=kea-leases".
        # Password/TLS come from PGPASSWORD / PGSSLMODE / PGSSLROOTCERT env.
        # api_url: the instance's HTTP control socket. When set, host
        # reservations are managed through the host_cmds API (hosts
        # database) and stale leases are evicted via lease_cmds; without
        # it, reservation sync stays a logged no-op as before.
        self.dsn = dsn
        self.api = DHCP4API(
            api_url, username=api_username,
            password=api_password) if api_url else None
        # Lease evictions recorded by the in-memory reservation logic;
        # flushed by push() so check-only runs never mutate the live server.
        self._pending_lease_dels = set()
        self.conf = None
        self.commit_conf = None
        self._has_commit = False
        self.auto_commit = True
        self.ip_uniqueness = True

    def _connect(self):
        return psycopg.connect(self.dsn)

    def pull(self):
        """Load the current CB subnets into the in-memory conf (so the inherited
        set_/update_/del_ logic and push()'s reconcile round-trip correctly)."""

        logging.info('pull running config from Kea config backend (DB)')
        cols = list(SCALAR_COLS.values())
        subnets = {}
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT subnet_id, subnet_prefix, {} '
                    'FROM dhcp4_subnet'.format(', '.join(cols)))
                for row in cur.fetchall():
                    sid, prefix = row[0], row[1]
                    s = {PREFIX: sid, 'subnet': prefix, POOLS: [], RESAS: []}
                    for col, val in zip(cols, row[2:]):
                        if val is None:
                            continue
                        if col == 'next_server':
                            val = str(val).split('/')[0]
                        s[COL_KEYS[col]] = val
                    subnets[sid] = s

                cur.execute(
                    'SELECT subnet_id, host(start_address), host(end_address), '
                    'user_context FROM dhcp4_pool')
                for sid, start, end, uctx in cur.fetchall():
                    s = subnets.get(sid)
                    if s is None:
                        continue
                    irid = uctx.get(IP_RANGE) if isinstance(uctx, dict) else None
                    pool = {'pool': '{}-{}'.format(start, end)}
                    pool.setdefault(USR_CTX, {})[IP_RANGE] = irid
                    s[POOLS].append(pool)

                cur.execute(
                    'SELECT dhcp4_subnet_id, code, formatted_value FROM '
                    'dhcp4_options WHERE scope_id=1 AND dhcp4_subnet_id IS NOT NULL')
                for sid, code, val in cur.fetchall():
                    name = CODE_NAMES.get(code)
                    s = subnets.get(sid)
                    if s is not None and name:
                        s.setdefault('option-data', []).append(
                            {'name': name, 'data': val})

                # Host reservations live in the host backend (hosts table),
                # written via the host_cmds API. Read them back by SQL: the
                # reservation-get-page response spec doesn't document
                # user-context, and the netbox id it carries is our
                # reconcile identity.
                if self.api:
                    for sid, resa in self._fetch_reservations(cur):
                        s = subnets.get(sid)
                        if s is not None:
                            s[RESAS].append(resa)

        self.conf = {SUBNETS: list(subnets.values())}
        self.commit_conf = deepcopy(self.conf)
        # A pull resynchronizes from the DB: any staged-but-unpushed commit
        # is now stale, so discard it (pull acts as rollback) — including
        # lease evictions staged by the discarded reservation changes.
        self._has_commit = False
        self._pending_lease_dels.clear()

    @staticmethod
    def _fetch_reservations(cur):
        """Yield (subnet_id, reservation_item) rows from the hosts table.

        ipv4_address is BIGINT holding the address as an integer (same
        representation as lease4.address); dhcp_identifier is raw BYTEA
        (hw-address = 6 MAC bytes, host_identifier_type 0); user_context
        is TEXT holding JSON."""

        cur.execute(
            'SELECT dhcp4_subnet_id, ipv4_address, hostname, user_context, '
            'dhcp_identifier, dhcp_identifier_type FROM hosts '
            'WHERE dhcp4_subnet_id IS NOT NULL')
        for sid, ipv4, hostname, uctx, ident, ident_type in cur.fetchall():
            resa = {}
            if ipv4:
                resa['ip-address'] = str(ip_address(ipv4))
            if hostname:
                resa['hostname'] = hostname
            if ident_type == HW_ADDRESS_TYPE and ident:
                resa['hw-address'] = ':'.join(
                    f'{b:02x}' for b in bytes(ident))
            try:
                ctx = json.loads(uctx) if uctx else {}
            except ValueError:
                ctx = {}
            if not isinstance(ctx, dict):
                ctx = {}
            ctx.setdefault(IP_ADDR, None)
            resa[USR_CTX] = ctx
            yield sid, resa

    def _evict_lease(self, ip):
        # Defer to push(): the connector only pushes outside check mode,
        # so staging a reservation change must not touch the live server.
        logging.debug(f'staging lease eviction for {ip} until push')
        self._pending_lease_dels.add(ip)

    def set_reservation(self, prefix_id, ipaddr_id, resa_item):
        if self.api is None:
            logging.warning(
                'CB backend: no control URL configured, reservation for IP '
                'id {} skipped'.format(ipaddr_id))
            return
        if resa_item.get('hw-address'):
            # Normalize: the hosts-table read-back is lowercase hex, so a
            # differently-cased NetBox MAC would otherwise churn del+add on
            # every push and false-trigger the MAC-change lease eviction.
            resa_item = {**resa_item,
                         'hw-address': resa_item['hw-address'].strip().lower()}
        # Inherited in-memory logic: duplicate checks + stale-lease
        # eviction via lease_cmds; push() applies via host_cmds.
        super().set_reservation(prefix_id, ipaddr_id, resa_item)

    def del_resa(self, ipaddr_id):
        if self.api is None:
            logging.debug(
                'CB backend: no control URL, del reservation {} '
                'no-op'.format(ipaddr_id))
            return
        super().del_resa(ipaddr_id)

    def commit(self):
        """Record the working conf. No control-agent validation — Kea validates
        on config-fetch; a bad row would surface in the kea-dhcp4 logs."""

        self.commit_conf = deepcopy(self.conf)
        self._has_commit = True
        return True

    def push(self):
        """Reconcile the CB to match the committed conf, in one audit revision."""

        if not self._has_commit:
            logging.debug('no commit to push')
            return

        desired = self.commit_conf[SUBNETS]
        desired_ids = {s[PREFIX] for s in desired}
        logging.info(
            'push {} subnet(s) to Kea config backend (DB)'.format(len(desired)))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT createAuditRevisionDHCP4(now(), %s, %s, true)',
                    (SERVER_TAG, 'netbox-kea-dhcp sync'))
                cur.execute(
                    'SELECT id FROM dhcp4_server WHERE tag=%s', (SERVER_TAG,))
                server_id = cur.fetchone()[0]

                cur.execute('SELECT subnet_id FROM dhcp4_subnet')
                for (sid,) in cur.fetchall():
                    if sid not in desired_ids:
                        self._delete_subnet(cur, sid)
                for s in desired:
                    self._upsert_subnet(cur, s, server_id)
        self._has_commit = None
        if self.api:
            self._reconcile_reservations(desired)
            # Flush lease evictions staged by MAC changes / deletions —
            # the only point live lease mutations are allowed to happen.
            for ip in sorted(self._pending_lease_dels):
                try:
                    self.api.del_lease4(ip)
                except KeaError as e:
                    logging.error(f'lease4-del {ip} failed: {e}')
            self._pending_lease_dels.clear()

    def _reconcile_reservations(self, desired):
        """Diff desired in-memory reservations against the hosts table and
        apply through the host_cmds API (database target).

        Identity is the netbox_ip_address_id carried in user-context. Rows
        without it are foreign (manual or other tooling) and are never
        touched; a desired reservation whose IP collides with a foreign row
        is skipped with a warning. Changes are del-then-add:
        reservation-update identifies hosts by identifier+subnet, which
        breaks when the MAC itself changed. API failures are logged and
        skipped — the next full sync converges."""

        current = {}          # (subnet_id, netbox_id) -> pulled resa item
        foreign_ips = set()   # (subnet_id, ip) rows not ours
        with self._connect() as conn:
            with conn.cursor() as cur:
                for sid, resa in self._fetch_reservations(cur):
                    nid = resa[USR_CTX].get(IP_ADDR)
                    if nid is None:
                        if resa.get('ip-address'):
                            foreign_ips.add((sid, resa['ip-address']))
                        continue
                    current[(sid, nid)] = resa

        desired_map = {}
        for s in desired:
            for r in s.get(RESAS, []):
                nid = r.get(USR_CTX, {}).get(IP_ADDR)
                if nid is not None:
                    desired_map[(s[PREFIX], nid)] = r

        def _differs(have, want):
            if (have.get('hw-address') or '').lower() != \
                    (want.get('hw-address') or '').lower():
                return True
            return any(have.get(k) != want.get(k)
                       for k in ('ip-address', 'hostname'))

        # netbox ids whose desired target address is held by a foreign row:
        # their adds will be skipped, so their old rows must be kept too —
        # deleting first would permanently drop a working reservation.
        blocked_nids = {
            nid for (sid, nid), r in desired_map.items()
            if (sid, r.get('ip-address')) in foreign_ips}

        # Drop rows that are gone or changed (del-then-add on change)
        for (sid, nid), have in current.items():
            want = desired_map.get((sid, nid))
            if want is not None and not _differs(have, want):
                continue
            if nid in blocked_nids:
                logging.warning(
                    f'reservation netbox id {nid}: keeping old row '
                    f'{have.get("ip-address")} — desired target is held by '
                    'a foreign hosts row')
                continue
            ip = have.get('ip-address')
            if ip:
                try:
                    self.api.del_reservation(sid, ip)
                except KeaError as e:
                    logging.error(f'reservation-del {ip} failed: {e}')

        # Add new or changed reservations
        for (sid, nid), want in desired_map.items():
            have = current.get((sid, nid))
            if have is not None and not _differs(have, want):
                continue
            ip = want.get('ip-address')
            if (sid, ip) in foreign_ips:
                logging.warning(
                    f'reservation {ip} (subnet {sid}): a foreign hosts row '
                    'holds this address, skipping (not ours to replace)')
                continue
            payload = dict(want)
            payload['subnet-id'] = sid
            try:
                self.api.add_reservation(payload)
            except KeaError as e:
                logging.error(f'reservation-add {ip} failed: {e}')

    def _delete_subnet(self, cur, sid):
        # Delete children explicitly (don't rely on FK cascade); the dhcp4_subnet
        # ADEL trigger records the audit entry config-fetch needs.
        cur.execute('DELETE FROM dhcp4_options WHERE dhcp4_subnet_id=%s', (sid,))
        cur.execute('DELETE FROM dhcp4_pool WHERE subnet_id=%s', (sid,))
        cur.execute('DELETE FROM dhcp4_subnet_server WHERE subnet_id=%s', (sid,))
        cur.execute('DELETE FROM dhcp4_subnet WHERE subnet_id=%s', (sid,))

    def _upsert_subnet(self, cur, s, server_id):
        sid = s[PREFIX]
        cols = list(SCALAR_COLS.values())
        vals = [s.get(key) for key in SCALAR_COLS]
        placeholders = ', '.join(['%s'] * (2 + len(cols)))
        updates = ', '.join('{0}=EXCLUDED.{0}'.format(c) for c in cols)
        cur.execute(
            'INSERT INTO dhcp4_subnet (subnet_id, subnet_prefix, {}, '
            'modification_ts) VALUES ({}, now()) '
            'ON CONFLICT (subnet_id) DO UPDATE SET '
            'subnet_prefix=EXCLUDED.subnet_prefix, {}, modification_ts=now()'
            .format(', '.join(cols), placeholders, updates),
            [sid, s['subnet'], *vals])

        cur.execute('DELETE FROM dhcp4_subnet_server WHERE subnet_id=%s', (sid,))
        cur.execute(
            'INSERT INTO dhcp4_subnet_server (subnet_id, server_id, '
            'modification_ts) VALUES (%s, %s, now())', (sid, server_id))

        cur.execute('DELETE FROM dhcp4_pool WHERE subnet_id=%s', (sid,))
        for p in s.get(POOLS, []):
            start, _, end = p['pool'].partition('-')
            # user_context carries the netbox_ip_range_id provenance that
            # pull() reads back for reconcile matching; omitting it here
            # erased it on every push.
            uctx = p.get(USR_CTX)
            cur.execute(
                'INSERT INTO dhcp4_pool (start_address, end_address, subnet_id, '
                'user_context, modification_ts) VALUES (%s, %s, %s, %s, now())',
                (start.strip(), end.strip(), sid,
                 Json(uctx) if uctx else None))

        cur.execute(
            'DELETE FROM dhcp4_options WHERE dhcp4_subnet_id=%s AND scope_id=1',
            (sid,))
        for od in s.get('option-data', []):
            code = OPTION_CODES.get(od.get('name'))
            if code is None:
                logging.warning(
                    "CB: unknown option '{}' skipped".format(od.get('name')))
                continue
            # client_classes '[ ]' matches what Kea's own CB writer stores
            # unconditionally; the schema-v30 upgrade makes the column
            # NOT NULL, so omitting it breaks on the first db-upgrade
            # past Kea 3.0.
            cur.execute(
                'INSERT INTO dhcp4_options (code, formatted_value, space, '
                'persistent, dhcp4_subnet_id, scope_id, client_classes, '
                'modification_ts, cancelled) '
                'VALUES (%s, %s, %s, false, %s, 1, %s, now(), false)',
                (code, od.get('data'), 'dhcp4', sid, '[ ]'))

