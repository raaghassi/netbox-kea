"""
Poll Kea instances' lease databases and reflect ACTIVE leases into NetBox
as status=dhcp IP addresses. Observation only — this module never writes
to Kea.

Built for instances whose configuration is owned elsewhere (e.g. an
OPNsense-managed firewall): the poll needs only a reachable control
channel with libdhcp_lease_cmds.so loaded, no config ownership. It also
provides lease BACKFILL for syncer-managed instances, whose event-driven
run_script reflection misses leases that predate the hook.

Reads use lease4-get-page (the ARM warns lease4-get-all can hang the
server on large databases). Only state-0 (default) leases are reflected:
declined (1), expired-reclaimed (2), and released (3) addresses are not
held by a client.

Each source tags its records' description with '[<name>]' and reconciles
only its own tagged population, so multiple sources and the event-driven
/lease/ push endpoint coexist on the shared status=dhcp surface (their
address spaces are disjoint by construction — each Kea serves different
subnets).
"""
import logging
import time
from ipaddress import ip_interface

from .kea.api import DHCP4API

PAGE_LIMIT = 1024


class LeasePoller:

    def __init__(self, name, url, nb, username=None, password=None,
                 interval=60):
        self.name = name
        self.api = DHCP4API(url, username=username, password=password)
        # Dedicated NetboxApp: requests sessions aren't shared across
        # threads, and pollers run beside the webhook listener.
        self.nb = nb
        self.interval = interval

    def fetch_leases(self):
        """All leases, paged. Next page starts from the last address of
        the previous one; the final page returns fewer than PAGE_LIMIT."""

        leases, from_ = [], 'start'
        while True:
            page = self.api.get_leases_page(from_, PAGE_LIMIT)
            leases += page
            if len(page) < PAGE_LIMIT:
                return leases
            from_ = page[-1]['ip-address']

    def sync_once(self):
        """One reconcile pass: upsert active leases, delete this source's
        tagged records whose lease is gone. Writes only when changed to
        keep NetBox changelog noise down."""

        active = {}
        for lease in self.fetch_leases():
            ip = lease.get('ip-address')
            if ip and lease.get('state') == 0:
                active[ip] = lease

        marker = f'[{self.name}]'
        current = {}
        for obj in self.nb.dhcp_ips():
            if marker in (obj.description or ''):
                current[str(ip_interface(obj.address).ip)] = obj

        for ip, lease in active.items():
            host = (lease.get('hostname') or '').strip().rstrip('.') or None
            mac = (lease.get('hw-address') or '').strip()
            desc = f'kea lease {marker}' + (f' (mac {mac})' if mac else '')
            existing = current.get(ip)
            if existing is not None and \
                    (existing.dns_name or '') == (host or '') and \
                    (existing.description or '') == desc:
                continue
            logging.info(f'lease-poll {self.name}: upsert {ip}')
            self.nb.upsert_dhcp_ip(
                f'{ip}/32', dns_name=host, description=desc)

        for ip, obj in current.items():
            if ip not in active:
                logging.info(f'lease-poll {self.name}: remove {ip}')
                obj.delete()

        logging.debug(
            f'lease-poll {self.name}: {len(active)} active lease(s)')

    def run_forever(self):
        """Poll loop for a daemon thread. Failures are logged and retried
        next interval — observation must never take the syncer down."""

        while True:
            try:
                self.sync_once()
            except Exception as e:
                logging.error(f'lease-poll {self.name} failed: {e}')
            time.sleep(self.interval)
