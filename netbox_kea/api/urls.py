from django.urls import path
from netbox.api.routers import NetBoxRouter

from . import views

app_name = "netbox_kea"

router = NetBoxRouter()
router.register("servers", views.ServerViewSet)

urlpatterns = [
    path("lease-events/", views.LeaseEventView.as_view(), name="lease_events"),
    *router.urls,
]
