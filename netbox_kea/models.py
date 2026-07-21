import os
from typing import Literal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from netbox.constants import CENSOR_TOKEN, CENSOR_TOKEN_CHANGED
from netbox.models import NetBoxModel

from .choices import ServerModeChoices
from .kea import KeaClient


class SyncEvent(models.Model):
    """Journal row feeding the kea-sync daemon (the netbox_dns_bridge
    changelog pattern): signal receivers append rows on relevant model
    changes, the daemon consumes them in id order and dispatches the
    corresponding incremental sync. Rows are deleted on consumption —
    this is a queue, not an audit trail (NetBox's changelog is that)."""

    kind = models.CharField(max_length=32)
    object_id = models.BigIntegerField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("id",)

    def __str__(self):
        return f"{self.kind} {self.object_id}"


class Server(NetBoxModel):
    name = models.CharField(unique=True, max_length=255)
    dhcp4_url = models.CharField(
        verbose_name="DHCPv4 Server URL", null=True, blank=True, max_length=255
    )
    dhcp6_url = models.CharField(
        verbose_name="DHCPv6 Server URL", null=True, blank=True, max_length=255
    )
    username = models.CharField(null=True, blank=True, max_length=255)
    password = models.CharField(null=True, blank=True, max_length=255)
    ssl_verify = models.BooleanField(
        default=True,
        verbose_name="SSL Verification",
        help_text="Enable SSL certificate verification. Disable with caution!",
    )
    client_cert_path = models.CharField(
        max_length=4096,
        null=True,
        blank=True,
        verbose_name="Client Certificate",
        help_text="Optional client certificate.",
    )
    client_key_path = models.CharField(
        max_length=4096,
        null=True,
        blank=True,
        verbose_name="Private Key",
        help_text="Optional client key.",
    )
    ca_file_path = models.CharField(
        max_length=4096,
        null=True,
        blank=True,
        verbose_name="CA File Path",
        help_text="The specific CA certificate file to use for SSL verification.",
    )
    # kea-sync daemon fields. The default is observe: an unconfigured or
    # freshly-migrated Server must never become a sync target implicitly —
    # sync targets are converged to what NetBox defines.
    mode = models.CharField(
        max_length=16,
        choices=ServerModeChoices,
        default=ServerModeChoices.MODE_OBSERVE,
        help_text=(
            "How the kea-sync daemon treats this instance: cb/agent are "
            "sync targets (converged to NetBox-defined config); observe "
            "is lease reflection only and is never written to."
        ),
    )
    cb_dsn = models.CharField(
        max_length=1024,
        null=True,
        blank=True,
        verbose_name="Config backend DSN",
        help_text=(
            "libpq DSN of this instance's PostgreSQL config backend "
            "(cb mode). Password-free by design: the daemon supplies "
            "credentials via PG* environment variables."
        ),
    )
    poll_interval = models.PositiveIntegerField(
        default=60,
        help_text=(
            "Lease-reflection poll interval in seconds. Applies to every "
            "mode — cb/agent Servers reflect leases by polling too, in "
            "addition to their config sync. 0 disables lease reflection "
            "for this Server (sync-only)."
        ),
    )

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("plugins:netbox_kea:server", args=[self.pk])

    def get_client(self, version: Literal[4, 6]) -> KeaClient | None:
        url = self.dhcp4_url if version == 4 else self.dhcp6_url
        if url is None:
            return None

        return KeaClient(
            url=url,
            username=self.username,
            password=self.password,
            verify=self.ca_file_path or self.ssl_verify,
            client_cert=self.client_cert_path or None,
            client_key=self.client_key_path or None,
            timeout=settings.PLUGINS_CONFIG["netbox_kea"]["kea_timeout"],
        )

    def clean(self) -> None:
        super().clean()

        if self.dhcp4_url is None and self.dhcp6_url is None:
            raise ValidationError(
                {"dhcp6_url": "At one DHCPv4 URL or DHCPv6 URL needs to be provided."}
            )

        if self.mode == ServerModeChoices.MODE_CB and not self.cb_dsn:
            raise ValidationError(
                {"cb_dsn": "cb mode requires the config backend DSN."}
            )
        if self.cb_dsn:
            # enforce the documented password-free invariant: cb_dsn is
            # API-readable and changelog-visible, unlike the password field
            from psycopg import conninfo

            try:
                parsed = conninfo.conninfo_to_dict(self.cb_dsn)
            except Exception as e:
                raise ValidationError({"cb_dsn": f"Invalid libpq DSN: {e}"}) from e
            if "password" in parsed:
                raise ValidationError(
                    {
                        "cb_dsn": (
                            "cb_dsn must not contain a password (it is "
                            "stored and exposed in cleartext) — the "
                            "daemon supplies credentials via PG* "
                            "environment variables."
                        )
                    }
                )
        if (
            self.mode
            in (
                ServerModeChoices.MODE_CB,
                ServerModeChoices.MODE_AGENT,
            )
            and not self.dhcp4_url
        ):
            raise ValidationError(
                {
                    "dhcp4_url": (
                        "cb/agent modes require the DHCPv4 control URL "
                        "(the kea-sync daemon's command channel)."
                    )
                }
            )

        if (self.client_cert_path and not self.client_key_path) or (
            not self.client_cert_path and self.client_key_path
        ):
            raise ValidationError(
                {
                    "client_cert_path": "Client certificate and client private key must be used together."
                }
            )

        if self.client_cert_path and not os.path.isfile(self.client_cert_path):
            raise ValidationError(
                {"client_cert_path": "Client certificate doesn't exist."}
            )
        if self.client_key_path and not os.path.isfile(self.client_key_path):
            raise ValidationError(
                {"client_key_path": "Client private key doesn't exist."}
            )

        if self.ca_file_path and not self.ssl_verify:
            raise ValidationError(
                {
                    "ca_file_path": "Cannot specify a CA file when SSL verification is disabled."
                }
            )

        if client6 := self.get_client(6):
            try:
                client6.command("version-get")
            except Exception as e:
                raise ValidationError(
                    {"dhcp6_url": f"Unable to get DHCPv6 version: {repr(e)}"}
                ) from e
        if client4 := self.get_client(4):
            try:
                client4.command("version-get")
            except Exception as e:
                raise ValidationError(
                    {"dhcp4_url": f"Unable to get DHCPv4 version: {repr(e)}"}
                ) from e

    def to_objectchange(self, action: str) -> None:
        objectchange = super().to_objectchange(action)

        prechange_data = objectchange.prechange_data or {}
        if prechange_data.get("password"):
            prechange_data["password"] = CENSOR_TOKEN

        if (post_data := objectchange.postchange_data) and (
            post_password := post_data.get("password")
        ):
            post_data["password"] = (
                CENSOR_TOKEN_CHANGED
                if post_password != prechange_data.get("password")
                else CENSOR_TOKEN
            )

        return objectchange
