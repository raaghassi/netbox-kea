"""kea-sync daemon — NetBox → Kea sync + lease observation, in-process.

The netbox_dns_bridge service pattern: a management command run as its
own long-lived process beside the NetBox web/worker pods, sharing the
codebase and ORM. Replaces the standalone netbox-kea-dhcp syncer:

- Instance registry comes from netbox_kea Server rows (mode cb/agent =
  sync targets, observe = lease pollers), credentials included — no
  TOML, no env-var credential conventions, no webhooks.
- Incremental sync consumes the SyncEvent journal appended by
  sync_signals (signals never do Kea I/O; this process does).
- A Server-row change makes the daemon exit(0): restart-to-apply — the
  Deployment restarts it and it re-initializes with the new registry.
"""

import logging
import sys
import threading
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from keasync.connector import Connector
from keasync.ddns import DdnsManager
from keasync.kea.app import DHCP4App
from keasync.kea.cb import DHCP4CB
from keasync.lease_poller import LeasePoller

from ...choices import ServerModeChoices
from ...models import Server, SyncEvent
from ...sync_adapter import OrmAdapter

logger = logging.getLogger("netbox_kea.sync_daemon")

BATCH = 100
IDLE_SLEEP = 2.0


def _cfg():
    return settings.PLUGINS_CONFIG["netbox_kea"]


def build_registry():
    """(backends, default_tag, pollers) from Server rows.

    Names are lowercased to match the cf_kea_server routing convention.
    default tag: the sync_default_server setting, else the sole sync
    server; fatal if ambiguous — silently picking one would misroute
    every untagged prefix.
    """

    cfg = _cfg()
    backends, pollers = {}, []
    for srv in Server.objects.all():
        name = srv.name.strip().lower()
        if srv.mode == ServerModeChoices.MODE_CB:
            backends[name] = DHCP4CB(
                srv.cb_dsn, api_url=srv.dhcp4_url,
                api_username=srv.username, api_password=srv.password)
        elif srv.mode == ServerModeChoices.MODE_AGENT:
            backends[name] = DHCP4App(
                srv.dhcp4_url, username=srv.username,
                password=srv.password)
        elif srv.mode == ServerModeChoices.MODE_OBSERVE:
            pollers.append(LeasePoller(
                name, srv.dhcp4_url, OrmAdapter(),
                username=srv.username, password=srv.password,
                interval=srv.poll_interval))

    default_tag = cfg.get("sync_default_server")
    if default_tag:
        default_tag = str(default_tag).strip().lower()
        if default_tag not in backends:
            logger.critical(
                "sync_default_server %r is not a cb/agent-mode Server",
                default_tag)
            sys.exit(1)
    elif len(backends) == 1:
        default_tag = next(iter(backends))
    elif len(backends) > 1:
        logger.critical(
            "multiple sync-mode Servers but no sync_default_server set")
        sys.exit(1)
    return backends, default_tag, pollers


class Command(BaseCommand):
    help = "Run the NetBox->Kea sync daemon (kea-sync)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-initial-sync", action="store_true",
            help="Skip the full sync at startup.")
        parser.add_argument(
            "--once", action="store_true",
            help="Full sync + one poll pass, then exit (no event loop).")

    def handle(self, *args, **options):
        cfg = _cfg()
        backends, default_tag, pollers = build_registry()

        conn = None
        if backends:
            adapter = OrmAdapter(
                prefix_filter=cfg.get("sync_prefix_filter"),
                iprange_filter=cfg.get("sync_iprange_filter"),
                ipaddress_filter=cfg.get("sync_ipaddress_filter"))
            conn = Connector(
                adapter, backends,
                cfg.get("sync_subnet_prefix_map"),
                cfg.get("sync_pool_iprange_map"),
                cfg.get("sync_reservation_ipaddr_map"),
                check=cfg.get("sync_check_only", False),
                default_tag=default_tag if len(backends) > 1 else None,
                tag_field=cfg.get("sync_tag_field", "kea_server"))
            logger.info("sync targets: %s (default %s)",
                        ", ".join(backends), default_tag)
        else:
            logger.info("no cb/agent-mode Servers; sync disabled")

        ddns = None
        if cfg.get("ddns_d2_url"):
            ddns = DdnsManager(
                "http://localhost/", "unused", cfg["ddns_d2_url"],
                username=cfg.get("ddns_d2_username"),
                password=cfg.get("ddns_d2_password"),
                zone_source=_orm_zone_names)

        if not options["no_initial_sync"]:
            if conn:
                logger.info("initial full sync")
                conn.sync_all()
            if ddns:
                try:
                    ddns.sync()
                except Exception as e:
                    logger.error("initial D2 sync failed: %s", e)
            for p in pollers:
                try:
                    p.sync_once()
                except Exception as e:
                    logger.error("initial lease poll (%s) failed: %s",
                                 p.name, e)

        if options["once"]:
            return

        for p in pollers:
            logger.info("lease-poll: observing %s every %ss",
                        p.name, p.interval)
            threading.Thread(target=p.run_forever, daemon=True,
                             name=f"lease-poll-{p.name}").start()

        dispatch = {}
        if conn:
            dispatch = {
                "prefix": conn.sync_prefix,
                "iprange": conn.sync_iprange,
                "ipaddress": conn.sync_ipaddress,
                "interface": conn.sync_interface,
                "device": conn.sync_device,
                "vminterface": conn.sync_vminterface,
                "virtualmachine": conn.sync_virtualmachine,
            }

        logger.info("consuming SyncEvent queue")
        while True:
            close_old_connections()
            events = list(SyncEvent.objects.order_by("id")[:BATCH])
            if not events:
                time.sleep(IDLE_SLEEP)
                continue
            if conn:
                conn.reload_dhcp_config()
            touched_sync = False
            for ev in events:
                if ev.kind == "server":
                    # registry changed: restart-to-apply. Consume the
                    # queue up to this point first.
                    SyncEvent.objects.filter(
                        id__in=[e.id for e in events]).delete()
                    logger.info("Server registry changed; exiting for "
                                "re-initialization")
                    sys.exit(0)
                elif ev.kind == "zone":
                    if ddns:
                        try:
                            ddns.sync()
                        except Exception as e:
                            logger.error("D2 sync failed: %s", e)
                elif ev.kind in dispatch:
                    try:
                        dispatch[ev.kind](ev.object_id)
                        touched_sync = True
                    except Exception as e:
                        logger.error("sync_%s(%s) failed: %s",
                                     ev.kind, ev.object_id, e)
            if conn and touched_sync:
                conn.push_to_dhcp()
            SyncEvent.objects.filter(
                id__in=[e.id for e in events]).delete()


def _orm_zone_names():
    """ddns_enabled active zones via the ORM (replaces the API query)."""

    from netbox_dns.models import Zone

    return [
        z.name for z in Zone.objects.filter(status="active")
        if (z.custom_field_data or {}).get("ddns_enabled")
    ]
