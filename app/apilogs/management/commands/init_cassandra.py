import time

from django.core.management.base import BaseCommand, CommandError

from apilogs import cassandra_store as store


class Command(BaseCommand):
    help = "Wait for Cassandra, then create the API log keyspace and tables (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--wait", type=int, default=120, help="Seconds to keep retrying.")

    def handle(self, *args, wait, **options):
        deadline = time.monotonic() + wait
        while True:
            store.reset()  # also clears the connect back-off between attempts
            try:
                store.get_session()
                break
            except store.CassandraUnavailable as exc:
                if time.monotonic() > deadline:
                    raise CommandError(str(exc))
                self.stdout.write("Waiting for Cassandra...")
                time.sleep(3)
        self.stdout.write(self.style.SUCCESS("Cassandra schema is ready."))
