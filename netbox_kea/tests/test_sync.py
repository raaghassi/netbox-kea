"""ORM/logic tests for the kea-sync daemon plumbing.

These exercise the Django-ORM-dependent code the framework-free
tests_engine/ suite can't reach: build_registry's per-Server role
assignment, OrmAdapter's lease-reflection writes, and the signal
suppression that keeps the daemon from reacting to its own reflections.

Run inside a NetBox install (postgres required — NetBox has no sqlite
path): `python manage.py test netbox_kea` from the netbox/ directory.
"""

from unittest.mock import patch

from django.test import TestCase
from ipam.models import IPAddress

from keasync.kea.app import DHCP4App
from keasync.kea.cb import DHCP4CB
from netbox_kea.management.commands.kea_sync_daemon import (
    _server_password,
    build_registry,
)
from netbox_kea.models import Server, SyncEvent
from netbox_kea.sync_adapter import OrmAdapter
from netbox_kea.sync_signals import suppress_sync

URL = "http://kea-control.example:8000/"
DSN = "host=pg dbname=kea user=kea-leases sslmode=require"


class BuildRegistryTests(TestCase):
    """build_registry maps Server rows to write backends and pollers."""

    def _poller_names(self, pollers):
        return {p.name for p in pollers}

    def test_cb_server_gets_backend_and_poller(self):
        Server.objects.create(
            name="svcs", mode="cb", dhcp4_url=URL, cb_dsn=DSN, poll_interval=60
        )
        backends, default_tag, pollers = build_registry()
        self.assertIsInstance(backends["svcs"], DHCP4CB)
        self.assertEqual(self._poller_names(pollers), {"svcs"})
        self.assertEqual(pollers[0].interval, 60)
        self.assertEqual(default_tag, "svcs")

    def test_agent_server_gets_backend_and_poller(self):
        Server.objects.create(
            name="site-a", mode="agent", dhcp4_url=URL, poll_interval=30
        )
        backends, _, pollers = build_registry()
        self.assertIsInstance(backends["site-a"], DHCP4App)
        self.assertEqual(self._poller_names(pollers), {"site-a"})
        self.assertEqual(pollers[0].interval, 30)

    def test_observe_server_is_poller_only(self):
        Server.objects.create(
            name="narwhal", mode="observe", dhcp4_url=URL, poll_interval=60
        )
        backends, _, pollers = build_registry()
        self.assertEqual(backends, {})
        self.assertEqual(self._poller_names(pollers), {"narwhal"})

    def test_poll_interval_zero_disables_poller(self):
        # cb Server stays a write backend but reflects no leases
        Server.objects.create(
            name="svcs", mode="cb", dhcp4_url=URL, cb_dsn=DSN, poll_interval=0
        )
        backends, _, pollers = build_registry()
        self.assertIn("svcs", backends)
        self.assertEqual(pollers, [])

    def test_name_is_lowercased(self):
        Server.objects.create(name="SVCS-Kea", mode="cb", dhcp4_url=URL, cb_dsn=DSN)
        backends, default_tag, pollers = build_registry()
        self.assertIn("svcs-kea", backends)
        self.assertEqual(default_tag, "svcs-kea")
        self.assertEqual(self._poller_names(pollers), {"svcs-kea"})

    def test_multiple_sync_servers_without_default_is_fatal(self):
        Server.objects.create(name="a", mode="cb", dhcp4_url=URL, cb_dsn=DSN)
        Server.objects.create(name="b", mode="cb", dhcp4_url=URL, cb_dsn=DSN)
        with self.assertRaises(SystemExit):
            build_registry()


class ServerPasswordTests(TestCase):
    """_server_password: explicit password wins, else env when a username
    is set, else no auth."""

    def test_explicit_password_wins(self):
        srv = Server(username="u", password="stored")
        with patch.dict("os.environ", {"KEA_CTRL_PASSWORD": "env"}):
            self.assertEqual(_server_password(srv), "stored")

    def test_env_fallback_when_username_no_password(self):
        srv = Server(username="u", password="")
        with patch.dict("os.environ", {"KEA_CTRL_PASSWORD": "env"}):
            self.assertEqual(_server_password(srv), "env")

    def test_no_username_means_no_auth(self):
        srv = Server(username="", password="")
        with patch.dict("os.environ", {"KEA_CTRL_PASSWORD": "env"}):
            self.assertIsNone(_server_password(srv))


class OrmAdapterReflectionTests(TestCase):
    """OrmAdapter's lease-reflection writes create/maintain status=dhcp IPs
    WITHOUT re-entering the sync pipeline (suppress_sync)."""

    def setUp(self):
        self.nb = OrmAdapter()
        SyncEvent.objects.all().delete()

    def test_upsert_creates_status_dhcp_ip(self):
        self.nb.upsert_dhcp_ip(
            "10.0.0.5/32", dns_name="pc.lan", description="kea lease [svcs]"
        )
        ip = IPAddress.objects.get(address="10.0.0.5/32")
        self.assertEqual(ip.status, "dhcp")
        self.assertEqual(ip.dns_name, "pc.lan")
        self.assertEqual(ip.description, "kea lease [svcs]")

    def test_upsert_updates_in_place(self):
        self.nb.upsert_dhcp_ip("10.0.0.5/32", dns_name="old")
        self.nb.upsert_dhcp_ip("10.0.0.5/32", dns_name="new")
        qs = IPAddress.objects.filter(address="10.0.0.5/32", status="dhcp")
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().dns_name, "new")

    def test_upsert_none_clears(self):
        self.nb.upsert_dhcp_ip("10.0.0.5/32", dns_name="pc.lan")
        self.nb.upsert_dhcp_ip("10.0.0.5/32", dns_name=None)
        self.assertEqual(IPAddress.objects.get(address="10.0.0.5/32").dns_name, "")

    def test_reflection_write_does_not_enqueue_syncevent(self):
        # THE churn fix: a reflected lease must not drive sync_ipaddress
        self.nb.upsert_dhcp_ip("10.0.0.5/32", dns_name="pc.lan")
        self.assertEqual(SyncEvent.objects.count(), 0)

    def test_reflection_delete_does_not_enqueue_syncevent(self):
        self.nb.upsert_dhcp_ip("10.0.0.5/32")
        SyncEvent.objects.all().delete()
        self.nb.delete_dhcp_ip("10.0.0.5/32")
        self.assertEqual(SyncEvent.objects.count(), 0)
        self.assertFalse(IPAddress.objects.filter(address="10.0.0.5/32").exists())

    def test_poller_stale_delete_via_view_is_suppressed(self):
        self.nb.upsert_dhcp_ip("10.0.0.9/32")
        SyncEvent.objects.all().delete()
        view = next(iter(self.nb.dhcp_ips()))
        view.delete()
        self.assertEqual(SyncEvent.objects.count(), 0)


class SyncSignalTests(TestCase):
    """The signal handler enqueues SyncEvents for real IPAM changes but not
    inside suppress_sync()."""

    def setUp(self):
        SyncEvent.objects.all().delete()

    def test_normal_ipaddress_save_enqueues(self):
        IPAddress.objects.create(address="10.0.0.20/32", status="active")
        self.assertTrue(SyncEvent.objects.filter(kind="ipaddress").exists())

    def test_server_save_enqueues_server_event(self):
        Server.objects.create(name="s", mode="cb", dhcp4_url=URL, cb_dsn=DSN)
        self.assertTrue(SyncEvent.objects.filter(kind="server").exists())

    def test_suppress_sync_blocks_enqueue(self):
        with suppress_sync():
            IPAddress.objects.create(address="10.0.0.21/32", status="active")
        self.assertFalse(SyncEvent.objects.exists())

    def test_suppress_sync_restores_after_context(self):
        with suppress_sync():
            IPAddress.objects.create(address="10.0.0.22/32", status="active")
        IPAddress.objects.create(address="10.0.0.23/32", status="active")
        # exactly the one write outside the context enqueued
        self.assertEqual(SyncEvent.objects.filter(kind="ipaddress").count(), 1)
