import time
import unittest
from datetime import date, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from . import cassandra_store as store
from .filters import chart_buckets, days_for_period, parse_status, status_matches


class PeriodTests(SimpleTestCase):
    def test_periods(self):
        today = date(2026, 11, 13)
        self.assertEqual(days_for_period("today", today), [today])
        self.assertEqual(days_for_period("yesterday", today), [date(2026, 11, 12), today])
        self.assertEqual(len(days_for_period("7d", today)), 7)
        month = days_for_period("month", today)
        self.assertEqual((month[0], month[-1]), (date(2026, 10, 13), today))

    def test_long_periods(self):
        today = date(2026, 11, 13)
        self.assertEqual(days_for_period("3m", today)[0], date(2026, 8, 13))
        self.assertEqual(days_for_period("3m", date(2026, 2, 10))[0], date(2025, 11, 10))
        self.assertEqual(days_for_period("3m", date(2026, 5, 31))[0], date(2026, 2, 28))
        # "All" spans the retention window, or a 1-year cap when logs never expire.
        self.assertEqual(len(days_for_period("all", today, retention_days=180)), 180)
        self.assertEqual(len(days_for_period("all", today, retention_days=0)), 365)

    def test_chart_buckets(self):
        today = date(2026, 11, 13)
        month = days_for_period("month", today)
        self.assertEqual(chart_buckets(month), [[d] for d in month])  # daily
        all_days = days_for_period("all", today, retention_days=180)
        weeks = chart_buckets(all_days)
        self.assertEqual(len(weeks), 26)
        self.assertEqual(weeks[-1], all_days[-7:])  # newest bar is a full week ending today
        self.assertEqual(len(weeks[0]), 180 - 25 * 7)  # only the oldest may be partial
        self.assertEqual([d for w in weeks for d in w], all_days)

    def test_month_clamps_short_months(self):
        self.assertEqual(days_for_period("month", date(2026, 3, 31))[0], date(2026, 2, 28))
        self.assertEqual(days_for_period("month", date(2026, 1, 15))[0], date(2025, 12, 15))

    def test_status_parsing(self):
        self.assertIsNone(parse_status(""))
        self.assertIsNone(parse_status("abc"))
        self.assertEqual(parse_status("4XX"), "4xx")
        self.assertEqual(parse_status("404"), 404)
        self.assertTrue(status_matches(404, "4xx"))
        self.assertFalse(status_matches(500, "4xx"))
        self.assertTrue(status_matches(500, 500))


def _cassandra_available():
    try:
        store.reset()
        store.get_session()
        return True
    except store.CassandraUnavailable:
        return False
    finally:
        store.reset()


TEST_CASSANDRA = {**settings.CASSANDRA, "KEYSPACE": "api_logs_test", "LOG_TTL_SECONDS": 3600}


@override_settings(CASSANDRA=TEST_CASSANDRA)
class CassandraIntegrationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with override_settings(CASSANDRA=TEST_CASSANDRA):
            if not _cassandra_available():
                raise unittest.SkipTest("Cassandra is not reachable")

    def setUp(self):
        store.reset()
        session = store.get_session()
        for table in ("api_logs_by_day", "api_logs_by_status_day", "api_log_counts_by_day"):
            session.execute(f"TRUNCATE {TEST_CASSANDRA['KEYSPACE']}.{table}")
        user = get_user_model().objects.create_superuser("admin", "admin@example.com", "pw")
        self.client.force_login(user)

    def tearDown(self):
        store.reset()

    def _wait_for(self, total, days):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            got = sum(c["requests"] for d in store.day_counts(days).values() for c in d.values())
            if got >= total:
                return
            time.sleep(0.1)
        self.fail(f"expected {total} logged requests")

    def test_api_calls_are_logged_and_filterable(self):
        self.client.get("/api/hello/")
        self.client.get("/api/items/999/")
        self.client.post("/api/login/", {"password": "wrong"}, content_type="application/json")
        today = timezone.localdate()
        self._wait_for(3, [today])

        counts = store.day_counts([today])[today]
        self.assertEqual({code: c["requests"] for code, c in counts.items()}, {200: 1, 404: 1, 401: 1})

        page = self.client.get("/admin/apilogs/apilog/?period=today")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["summary"]["total"], 3)
        self.assertEqual(len(page.context["rows"]), 3)

        page = self.client.get("/admin/apilogs/apilog/?period=today&status=4xx")
        self.assertEqual({r["status_code"] for r in page.context["rows"]}, {401, 404})
        page = self.client.get("/admin/apilogs/apilog/?period=today&status=404")
        self.assertEqual([r["status_code"] for r in page.context["rows"]], [404])

        # The login password is masked by drf-api-logger before it is stored.
        login = next(r for r in store.list_logs({today: [401]}, by_status=True)[0])
        detail = store.get_log(login["id"])
        self.assertNotIn("wrong", detail["body"])
        self.assertEqual(self.client.get(f"/admin/apilogs/apilog/{login['id']}/").status_code, 200)

    def test_date_filters_and_paging(self):
        now = timezone.now()
        futures = []
        for age_days, n in ((0, 3), (1, 2), (5, 2), (20, 4), (40, 1)):
            for i in range(n):
                _, f = store.save_log({
                    "api": "/api/x/", "method": "GET", "status_code": 200 if i else 500,
                    "execution_time": 0.01, "added_on": now - timedelta(days=age_days, seconds=i),
                })
                futures += f
        for f in futures:
            f.result()

        totals = {}
        for period in ("today", "yesterday", "7d", "month", "3m", "all"):
            totals[period] = self.client.get(f"/admin/apilogs/apilog/?period={period}").context["summary"]["total"]
        self.assertEqual(totals, {"today": 3, "yesterday": 5, "7d": 7, "month": 11, "3m": 12, "all": 12})
        page = self.client.get("/admin/apilogs/apilog/?period=all")
        self.assertTrue(page.context["chart_weekly"])
        self.assertEqual(sum(b["total"] for b in page.context["chart"]), 12)

        today = timezone.localdate()
        plan = {d: [200, 500] for d in days_for_period("month", today)}
        seen, cursor = [], None
        while True:
            rows, cursor = store.list_logs(plan, by_status=False, before=cursor, limit=4)
            seen += rows
            if cursor is None:
                break
        self.assertEqual(len(seen), 11)
        self.assertEqual(len({r["id"] for r in seen}), 11)
        stamps = [r["added_on"] for r in seen]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
