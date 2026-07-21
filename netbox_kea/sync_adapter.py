"""ORM adapter for the keasync engine — the in-process replacement for
the standalone syncer's pynetbox client.

Exposes the exact interface keasync's Connector and LeasePoller consume.
Queries are driven through NetBox's OWN FilterSets so the filter dicts
keep their REST-API semantics (cf_*, status, parent, contains,
interface_id, ...) — the engine's filter configuration ports unchanged.
Rows are wrapped in lightweight views normalizing the attribute shapes
the engine expects from pynetbox: prefixes/addresses as strings,
custom_fields as a dict, assigned_object with a string mac_address.
"""

import logging

from ipam.filtersets import (
    IPAddressFilterSet,
    IPRangeFilterSet,
    PrefixFilterSet,
)
from ipam.models import IPAddress, IPRange, Prefix

logger = logging.getLogger("netbox_kea.sync_adapter")


class _AssignedView:
    """Interface/VMInterface view: mac_address normalized to string."""

    def __init__(self, obj):
        self._obj = obj
        mac = getattr(obj, "mac_address", None)
        self.mac_address = str(mac) if mac else None
        self.device = getattr(obj, "device", None)
        self.virtual_machine = getattr(obj, "virtual_machine", None)


class _PrefixView:
    def __init__(self, obj):
        self.id = obj.pk
        self.prefix = str(obj.prefix)
        self.custom_fields = obj.custom_field_data or {}

    def __str__(self):
        return self.prefix


class _IPRangeView:
    def __init__(self, obj):
        self.id = obj.pk
        self.start_address = str(obj.start_address)
        self.end_address = str(obj.end_address)
        self.custom_fields = obj.custom_field_data or {}

    def __str__(self):
        return f"{self.start_address}-{self.end_address}"


class _IPAddressView:
    def __init__(self, obj):
        self.id = obj.pk
        self.address = str(obj.address)
        self.dns_name = obj.dns_name
        self.description = obj.description
        self.custom_fields = obj.custom_field_data or {}
        assigned = getattr(obj, "assigned_object", None)
        self.assigned_object = _AssignedView(assigned) if assigned else None
        self._obj = obj

    def delete(self):
        self._obj.delete()

    def __str__(self):
        return self.address


class OrmAdapter:
    """Drop-in for keasync's `nb` dependency, backed by the local ORM."""

    def __init__(self, prefix_filter=None, iprange_filter=None,
                 ipaddress_filter=None):
        self.prefix_filter = prefix_filter or {}
        self.iprange_filter = iprange_filter or {}
        self.ipaddress_filter = ipaddress_filter or {"status": "dhcp"}

    @staticmethod
    def _filtered(filterset_cls, model, data):
        fs = filterset_cls(data=data, queryset=model.objects.all())
        return fs.qs

    # --- prefixes ---

    def prefix(self, id_):
        qs = self._filtered(PrefixFilterSet, Prefix, self.prefix_filter)
        obj = qs.filter(pk=id_).first()
        return _PrefixView(obj) if obj else None

    def prefixes(self, contains):
        qs = self._filtered(
            PrefixFilterSet, Prefix,
            {**self.prefix_filter, "contains": contains})
        return (_PrefixView(o) for o in qs)

    def all_prefixes(self):
        qs = self._filtered(PrefixFilterSet, Prefix, self.prefix_filter)
        return (_PrefixView(o) for o in qs)

    # --- ip ranges ---

    def ip_range(self, id_):
        qs = self._filtered(IPRangeFilterSet, IPRange, self.iprange_filter)
        obj = qs.filter(pk=id_).first()
        return _IPRangeView(obj) if obj else None

    def ip_ranges(self, parent):
        qs = self._filtered(
            IPRangeFilterSet, IPRange,
            {**self.iprange_filter, "parent": [parent]})
        return (_IPRangeView(o) for o in qs)

    # --- ip addresses ---

    def ip_address(self, id_):
        qs = self._filtered(
            IPAddressFilterSet, IPAddress, self.ipaddress_filter)
        obj = qs.filter(pk=id_).first()
        return _IPAddressView(obj) if obj else None

    def ip_addresses(self, **filters):
        if not filters:
            raise ValueError(
                "OrmAdapter.ip_addresses() requires at least one keyword arg")
        if "parent" in filters:
            filters["parent"] = [filters["parent"]]
        qs = self._filtered(
            IPAddressFilterSet, IPAddress,
            {**self.ipaddress_filter, **filters})
        return (_IPAddressView(o) for o in qs)

    # --- lease reflection (status=dhcp population only) ---

    def upsert_dhcp_ip(self, address, dns_name=None, description=None):
        """Reflection semantics (see keasync): dns_name/description mirror
        the CURRENT lease state — None clears."""
        existing = IPAddress.objects.filter(
            address=address, status="dhcp").first()
        if existing:
            existing.dns_name = dns_name or ""
            existing.description = description or ""
            existing.save()
            return _IPAddressView(existing)
        obj = IPAddress(address=address, status="dhcp",
                        dns_name=dns_name or "",
                        description=description or "")
        obj.save()
        return _IPAddressView(obj)

    def delete_dhcp_ip(self, address):
        for ip in IPAddress.objects.filter(address=address, status="dhcp"):
            ip.delete()

    def dhcp_ips(self):
        return (_IPAddressView(o)
                for o in IPAddress.objects.filter(status="dhcp"))
