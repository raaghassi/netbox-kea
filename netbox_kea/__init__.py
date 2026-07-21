from netbox.plugins import PluginConfig


class NetBoxKeaConfig(PluginConfig):
    name = "netbox_kea"
    verbose_name = "Kea"
    description = "Kea integration for NetBox"
    version = "2.0.0"
    base_url = "kea"
    default_settings = {
        "kea_timeout": 30,
        # --- kea-sync daemon (manage.py kea_sync_daemon) ---
        # Sync-target routing: prefixes select their Server via this
        # custom field; unlabelled prefixes go to the default Server
        # (required only when several cb/agent-mode Servers exist).
        "sync_default_server": None,
        "sync_tag_field": "kea_server",
        "sync_check_only": False,
        # Object-selection filters — REST-API filter semantics (driven
        # through NetBox's own FilterSets by the ORM adapter).
        "sync_prefix_filter": {"cf_dhcp_enabled": True, "status": "active"},
        "sync_iprange_filter": {"status": "active"},
        "sync_ipaddress_filter": {"status": "dhcp"},
        # NetBox-attribute -> Kea-setting maps (engine defaults; the
        # custom_fields.* paths resolve against custom_field_data).
        "sync_subnet_prefix_map": {
            "option-data.routers":
                "custom_fields.dhcp_option_data_routers",
            "option-data.domain-search":
                "custom_fields.dhcp_option_data_domain_search",
            "option-data.domain-name-servers":
                "custom_fields.dhcp_option_data_domain_name_servers",
            "next-server": "custom_fields.dhcp_next_server",
            "boot-file-name": "custom_fields.dhcp_boot_file_name",
            "valid-lifetime": "custom_fields.dhcp_valid_lifetime",
            "ddns-qualifying-suffix":
                "custom_fields.dhcp_ddns_qualifying_suffix",
            "ddns-send-updates":
                "custom_fields.dhcp_ddns_send_updates",
        },
        "sync_pool_iprange_map": {},
        "sync_reservation_ipaddr_map": {
            "hw-address": ["custom_fields.dhcp_reservation_hw_address",
                           "assigned_object.mac_address"],
            "hostname": ["dns_name", "assigned_object.device.name",
                         "assigned_object.virtual_machine.name"],
        },
        # kea-dhcp-ddns (D2) zone management; credentials optional.
        "ddns_d2_url": None,
        "ddns_d2_username": None,
        "ddns_d2_password": None,
    }

    def ready(self):
        super().ready()
        from . import sync_signals
        sync_signals.register()


config = NetBoxKeaConfig
