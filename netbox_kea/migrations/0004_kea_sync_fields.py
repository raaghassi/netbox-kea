import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("netbox_kea", "0003_remove_server_dhcp4_remove_server_dhcp6_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="server",
            name="mode",
            field=models.CharField(default="observe", max_length=16),
        ),
        migrations.AddField(
            model_name="server",
            name="cb_dsn",
            field=models.CharField(blank=True, max_length=1024, null=True),
        ),
        migrations.AddField(
            model_name="server",
            name="poll_interval",
            field=models.PositiveIntegerField(
                default=60,
                validators=[django.core.validators.MinValueValidator(1)],
            ),
        ),
        migrations.CreateModel(
            name="SyncEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("kind", models.CharField(max_length=32)),
                ("object_id", models.BigIntegerField()),
                ("created", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ("id",),
            },
        ),
    ]
