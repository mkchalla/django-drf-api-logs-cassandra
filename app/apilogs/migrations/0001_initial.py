from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="APILog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ],
            options={
                "verbose_name": "API log",
                "verbose_name_plural": "API logs",
                "db_table": "apilogs_cassandra_placeholder",
                "managed": False,
                "default_permissions": ("view",),
            },
        ),
    ]
