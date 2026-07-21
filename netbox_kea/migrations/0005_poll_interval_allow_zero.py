from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_kea", "0004_kea_sync_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="server",
            name="poll_interval",
            field=models.PositiveIntegerField(
                default=60,
                help_text=(
                    "Lease-reflection poll interval in seconds. Applies to "
                    "every mode — cb/agent Servers reflect leases by polling "
                    "too, in addition to their config sync. 0 disables lease "
                    "reflection for this Server (sync-only)."
                ),
            ),
        ),
    ]
