"""Bulk-load backdated fake API logs so every dashboard filter has data to show.

    python manage.py seed_api_logs                       # 1,000,000 logs over 180 days
    python manage.py seed_api_logs --count 5000 --days 35

Traffic is shaped to look real: volume grows over the period, weekends are
quieter, and requests peak in the afternoon. Rows are written with the TTL
they'd have left had they been logged live, so they age out on schedule; rows
already past the TTL are skipped.

Speed: several worker processes, each with its own Cassandra session and a
bounded number of in-flight async writes. Counters are summed in memory and
written once per (day, status code) instead of once per row.
"""
import math
import multiprocessing as mp
import os
import queue
import random
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

ENDPOINTS = [
    # (method, path, [(status, weight)], traffic weight)
    ("GET", "/api/hello/", [(200, 97), (500, 2), (503, 1)], 25),
    ("GET", "/api/items/", [(200, 97), (500, 2), (504, 1)], 20),
    ("POST", "/api/items/", [(201, 82), (400, 15), (409, 3)], 8),
    ("GET", "/api/items/{id}/", [(200, 72), (404, 27), (500, 1)], 18),
    ("PUT", "/api/items/{id}/", [(200, 80), (400, 10), (404, 10)], 4),
    ("DELETE", "/api/items/{id}/", [(204, 75), (404, 24), (403, 1)], 3),
    ("POST", "/api/login/", [(200, 62), (401, 33), (429, 5)], 10),
    ("GET", "/api/slow/?ms=300", [(200, 88), (503, 7), (504, 5)], 5),
    ("GET", "/api/old-endpoint/", [(301, 100)], 2),
    ("GET", "/api/reports/export/", [(200, 85), (302, 10), (500, 5)], 5),
]
METHODS_WITH_BODY = {"POST", "PUT"}
USER_AGENTS = ["python-requests/2.32", "okhttp/4.12", "Mozilla/5.0", "curl/8.7.1", "axios/1.7"]
# Relative traffic per local hour: quiet at night, peak mid-afternoon.
HOUR_WEIGHTS = [max(0.08, 0.55 + 0.45 * math.sin((h - 8) / 24 * 2 * math.pi)) for h in range(24)]


def _day_weights(days, today):
    """Weight per day (oldest first): ~2.5x growth across the period, quieter weekends."""
    weights = []
    for i in range(days):
        day = today - timedelta(days=days - 1 - i)
        growth = 1 + 1.5 * (i / max(days - 1, 1))
        weekend = 0.55 if day.weekday() >= 5 else 1.0
        weights.append(growth * weekend * random.uniform(0.9, 1.1))
    return weights


def _make_log(rng, day, now, tz):
    hour = rng.choices(range(24), HOUR_WEIGHTS)[0]
    added_on = timezone.make_aware(
        datetime(day.year, day.month, day.day, hour, rng.randrange(60), rng.randrange(60),
                 rng.randrange(1_000_000)),
        tz,
    )
    if added_on > now:  # today's later hours haven't happened yet
        added_on = now - timedelta(seconds=rng.uniform(0, 3600))
    method, path, outcomes, _ = rng.choices(ENDPOINTS, [e[3] for e in ENDPOINTS])[0]
    status = rng.choices([c for c, _ in outcomes], [w for _, w in outcomes])[0]
    path = path.replace("{id}", str(rng.randint(1, 5000)))
    seconds = rng.lognormvariate(-3.6, 0.7)
    if "slow" in path or "export" in path:
        seconds += rng.uniform(0.25, 1.2)
    if status >= 500:
        seconds += rng.uniform(0, 2.0)
    body = None
    if method in METHODS_WITH_BODY:
        body = ({"username": f"user{rng.randint(1, 900)}", "password": "***FILTERED***"}
                if "login" in path else {"name": f"item-{rng.randint(1, 99999)}"})
    if status >= 400:
        response = {"detail": {400: "Invalid input.", 401: "Invalid credentials.", 403: "Forbidden.",
                               404: "Not found.", 409: "Conflict.", 429: "Too many requests."}
                    .get(status, "Internal server error.")}
    elif status == 204:
        response = None
    else:
        response = {"ok": True}
    return {
        "api": path,
        "method": method,
        "status_code": status,
        "execution_time": round(seconds, 6),
        "client_ip_address": f"10.{rng.randint(0, 3)}.{rng.randint(0, 255)}.{rng.randint(2, 254)}",
        "headers": {"HOST": "localhost:8001", "USER_AGENT": rng.choice(USER_AGENTS),
                    "ACCEPT": "application/json"},
        "body": body,
        "response": response,
        "added_on": added_on,
    }


def _worker(worker_id, count, days, day_weights, concurrency, progress, errors, results, seed):
    import django
    from django.apps import apps

    if not apps.ready:  # spawned worker: fresh interpreter
        django.setup()
    from apilogs import cassandra_store as store

    store.reset()
    rng = random.Random(seed + worker_id)
    tz = timezone.get_current_timezone()
    now = timezone.now()
    today = timezone.localdate()
    all_days = [today - timedelta(days=days - 1 - i) for i in range(days)]
    ttl_seconds = int(settings.CASSANDRA.get("LOG_TTL_SECONDS") or 0)

    slots = threading.BoundedSemaphore(concurrency)
    failures = [0]

    def done(_=None):
        slots.release()

    def failed(exc):
        failures[0] += 1
        slots.release()

    counts = defaultdict(lambda: [0, 0])
    written = 0
    picked_days = rng.choices(all_days, day_weights, k=count)
    for i, day in enumerate(picked_days, 1):
        log = _make_log(rng, day, now, tz)
        ttl = 0
        if ttl_seconds:
            ttl = ttl_seconds - int((now - log["added_on"]).total_seconds())
            if ttl <= 0:
                continue  # would already have expired
        _, futures = store.save_log(log, ttl=ttl, bulk=True)
        for f in futures:
            slots.acquire()
            f.add_callbacks(done, failed)
        c = counts[(timezone.localtime(log["added_on"]).date(), log["status_code"])]
        c[0] += 1
        c[1] += int(log["execution_time"] * 1_000_000)
        written += 1
        if i % 2000 == 0:
            with progress.get_lock():
                progress.value += 2000
    with progress.get_lock():
        progress.value += count % 2000

    for _ in range(concurrency):  # wait for in-flight row writes
        slots.acquire()
    # Counters only for rows that were written.
    for (day, status), (n, us) in counts.items():
        store.add_counts(day, status, n, us).result()
    with errors.get_lock():
        errors.value += failures[0]
    store.reset()
    results.put(written)


class Command(BaseCommand):
    help = "Bulk-insert fake API logs spread over the last N days."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=1_000_000)
        parser.add_argument("--days", type=int, default=180)
        parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 2))
        parser.add_argument("--concurrency", type=int, default=256,
                            help="In-flight writes per worker.")
        parser.add_argument("--seed", type=int, default=None, help="Random seed (repeatable data).")

    def handle(self, *args, count, days, workers, concurrency, seed, **options):
        from apilogs import cassandra_store as store

        try:  # fail fast (and create the schema) before forking workers
            store.get_session()
        except store.CassandraUnavailable as exc:
            raise CommandError(str(exc))
        store.reset()

        ttl_days = (settings.CASSANDRA.get("LOG_TTL_SECONDS") or 0) // 86400
        if ttl_days and days > ttl_days:
            self.stdout.write(self.style.WARNING(
                f"API_LOG_TTL_DAYS is {ttl_days}: logs older than that are skipped."))

        seed = seed if seed is not None else random.randrange(1 << 30)
        random.seed(seed)
        day_weights = _day_weights(days, timezone.localdate())
        workers = max(1, min(workers, count))
        shares = [count // workers + (1 if i < count % workers else 0) for i in range(workers)]

        ctx = mp.get_context("spawn")
        progress = ctx.Value("q", 0)
        errors = ctx.Value("q", 0)
        self.stdout.write(f"Seeding {count:,} logs over {days} days with {workers} workers...")
        started = time.monotonic()
        results = ctx.Queue()
        procs = [
            ctx.Process(target=_worker, args=(
                i, shares[i], days, day_weights, concurrency, progress, errors, results, seed))
            for i in range(workers)
        ]
        for p in procs:
            p.start()
        written, finished = 0, 0
        while finished < workers:
            try:
                written += results.get(timeout=10)
                finished += 1
            except queue.Empty:
                if not any(p.is_alive() for p in procs):
                    raise CommandError("A seeding worker died; see the error above.")
                done = progress.value
                self.stdout.write(f"  {done:,} / {count:,} generated "
                                  f"({done / (time.monotonic() - started):,.0f}/s)")
        for p in procs:
            p.join()

        elapsed = time.monotonic() - started
        msg = f"Inserted {written:,} API logs over the last {days} days in {elapsed:,.0f}s"
        if written < count:
            msg += f" ({count - written:,} skipped: older than the TTL)"
        self.stdout.write(self.style.SUCCESS(msg + "."))
        if errors.value:
            self.stdout.write(self.style.ERROR(f"{errors.value:,} row writes failed."))
