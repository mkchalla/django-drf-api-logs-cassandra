from django.db import models


class APILog(models.Model):
    """Placeholder so the Cassandra-backed dashboard gets an admin entry and a
    "view" permission. It has no SQL table: the admin reads Cassandra directly.
    """

    class Meta:
        managed = False
        db_table = "apilogs_cassandra_placeholder"
        verbose_name = "API log"
        verbose_name_plural = "API logs"
        default_permissions = ("view",)
