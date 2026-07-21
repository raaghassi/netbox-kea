import logging

from netbox.api.authentication import TokenWritePermission
from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import filtersets, models
from ..sync_adapter import OrmAdapter
from .serializers import ServerSerializer

logger = logging.getLogger("netbox_kea.api")


class ServerViewSet(NetBoxModelViewSet):
    queryset = models.Server.objects.prefetch_related("tags")
    filterset_class = filtersets.ServerFilterSet
    serializer_class = ServerSerializer


class LeaseEventView(APIView):
    """Kea -> NetBox lease reflection (run_script hook target).

    Replaces the standalone syncer's bottle /lease/ endpoint; NetBox
    token auth replaces the homemade shared-secret header. Body:
    {"action": "add"|"del", "address", "hostname", "hwaddr"}.
    Touches only status=dhcp IPs (reflection population).

    TokenWritePermission: NetBox's default TokenPermissions is model-
    bound (asserts on a missing queryset — every request would 500);
    this is NetBox's own class for custom non-CRUD actions, and it
    still enforces the token's write_enabled flag."""

    permission_classes = [TokenWritePermission]

    def post(self, request):
        lease = request.data if isinstance(request.data, dict) else None
        if not lease or "address" not in lease:
            return Response(
                {"detail": 'lease body missing "address"'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        address = lease["address"]
        addr = address if "/" in address else f"{address}/32"
        action = lease.get("action")
        adapter = OrmAdapter()
        if action == "add":
            host = (lease.get("hostname") or "").strip().rstrip(".") or None
            mac = (lease.get("hwaddr") or "").strip()
            desc = "kea lease" + (f" (mac {mac})" if mac else "")
            adapter.upsert_dhcp_ip(addr, dns_name=host, description=desc)
        elif action == "del":
            adapter.delete_dhcp_ip(addr)
        else:
            return Response(
                {"detail": f"unknown lease action: {action}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"detail": "ok"}, status=status.HTTP_201_CREATED)
