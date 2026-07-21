"""Signal receivers feeding the kea-sync daemon's SyncEvent queue.

The netbox_dns_bridge pattern: receivers do NOTHING but append a journal
row — never Kea I/O, never blocking work — so UI saves stay fast and the
daemon (a separate process) applies changes with the engine's full
reconcile semantics. Event kinds mirror the webhook model names the
standalone syncer dispatched on (Connector.sync_<kind>).

Registered from NetBoxKeaConfig.ready().
"""

import logging
import threading
from contextlib import contextmanager

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Server, SyncEvent

logger = logging.getLogger("netbox_kea.sync_signals")

# The daemon's OWN lease-reflection writes (OrmAdapter.upsert_dhcp_ip /
# delete_dhcp_ip on status=dhcp IPs) must NOT re-enter the sync pipeline:
# a reflected lease has no MAC, so sync_ipaddress would del_resa it,
# flipping _has_commit and forcing a full CB rewrite per lease change.
# suppress_sync() (used by OrmAdapter around those writes) makes the
# signal handler skip enqueueing for the current thread.
_suppress = threading.local()


@contextmanager
def suppress_sync():
    prev = getattr(_suppress, "active", False)
    _suppress.active = True
    try:
        yield
    finally:
        _suppress.active = prev


# model label -> event kind (Connector.sync_<kind> / daemon dispatch)
_KINDS = {
    "ipam.prefix": "prefix",
    "ipam.iprange": "iprange",
    "ipam.ipaddress": "ipaddress",
    "dcim.interface": "interface",
    "dcim.device": "device",
    "virtualization.vminterface": "vminterface",
    "virtualization.virtualmachine": "virtualmachine",
    "netbox_dns.zone": "zone",
}


def _enqueue(kind, object_id):
    SyncEvent.objects.create(kind=kind, object_id=object_id)


def _handler(sender, instance, **kwargs):
    if getattr(_suppress, "active", False):
        return
    label = sender._meta.label_lower
    kind = _KINDS.get(label)
    if kind:
        _enqueue(kind, instance.pk)


@receiver(post_save, sender=Server)
@receiver(post_delete, sender=Server)
def _server_changed(sender, instance, **kwargs):
    # Registry definition changed: the daemon exits on consuming this and
    # re-initializes with the new Server set (restart-to-apply semantics).
    _enqueue("server", instance.pk)


def register():
    """Connect receivers. Called from PluginConfig.ready(); imports of
    other apps' models happen here, after the app registry is ready."""

    from dcim.models import Device, Interface
    from ipam.models import IPAddress, IPRange, Prefix
    from virtualization.models import VirtualMachine, VMInterface

    senders = [
        Prefix,
        IPRange,
        IPAddress,
        Interface,
        Device,
        VMInterface,
        VirtualMachine,
    ]
    try:
        from netbox_dns.models import Zone

        senders.append(Zone)
    except ImportError:
        logger.debug("netbox_dns not installed; zone events disabled")

    for sender in senders:
        post_save.connect(_handler, sender=sender, weak=False)
        post_delete.connect(_handler, sender=sender, weak=False)
