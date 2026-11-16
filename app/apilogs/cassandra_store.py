"""Cassandra storage for drf-api-logger records.

Tables are designed around the dashboard's queries (Cassandra has no ad-hoc
WHERE/GROUP BY, so each query gets a table shaped for it):

  api_logs_by_day         PRIMARY KEY ((day), id)
      Full record, newest first. Serves the unfiltered list and the detail page.

  api_logs_by_status_day  PRIMARY KEY ((day, status_code), id)
      Summary columns only. Serves the list when a status filter is applied.

  api_log_counts_by_day   PRIMARY KEY ((day), status_code)   -- counters
      Request count and summed execution time per day and status code. Serves
      the dashboard totals, the per-day chart and the status filter choices
      without reading any log rows.

`day` is the local date in settings.TIME_ZONE, so every date filter ("today",
"since yesterday", "last 7 days", "last month") is a set of whole partitions.
`id` is a time-based UUID (timeuuid), so clustering by it orders rows by time
and gives a stable cursor for paging.
"""
import json
import logging
import threading
import time
import uuid
from datetime import timezone as dt_timezone

from cassandra.auth import PlainTextAuthProvider
from cassandra.cluster import EXEC_PROFILE_DEFAULT, Cluster, ExecutionProfile
from cassandra.policies import DCAwareRoundRobinPolicy, TokenAwarePolicy
from cassandra.query import UNSET_VALUE, BatchStatement, BatchType, dict_factory
from cassandra.util import datetime_from_uuid1, uuid_from_time
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class CassandraUnavailable(Exception):
    pass


_lock = threading.Lock()
_session = None
_stmts = {}
_last_failure = 0.0


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS {ks}.api_logs_by_day (
        day date,
        id timeuuid,
        added_on timestamp,
        api text,
        method text,
        status_code int,
        execution_time double,
        client_ip_address text,
        headers text,
        body text,
        response text,
        tracing_id text,
        PRIMARY KEY ((day), id)
    ) WITH CLUSTERING ORDER BY (id DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS {ks}.api_logs_by_status_day (
        day date,
        status_code int,
        id timeuuid,
        added_on timestamp,
        api text,
        method text,
        execution_time double,
        client_ip_address text,
        PRIMARY KEY ((day, status_code), id)
    ) WITH CLUSTERING ORDER BY (id DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS {ks}.api_log_counts_by_day (
        day date,
        status_code int,
        requests counter,
        exec_time_us counter,
        PRIMARY KEY ((day), status_code)
    )
    """,
]

SUMMARY_COLUMNS = "id, added_on, api, method, status_code, execution_time, client_ip_address"


def _conf():
    return settings.CASSANDRA


def ensure_schema(session):
    conf = _conf()
    ks = conf["KEYSPACE"]
    replication = json.dumps(conf["REPLICATION"]).replace('"', "'")
    session.execute(f"CREATE KEYSPACE IF NOT EXISTS {ks} WITH replication = {replication}")
    for cql in SCHEMA:
        session.execute(cql.format(ks=ks))


def _prepare(session):
    p = session.prepare
    ks = _conf()["KEYSPACE"]
    return {
        "insert_log": p(
            f"INSERT INTO {ks}.api_logs_by_day (day, id, added_on, api, method, status_code, "
            f"execution_time, client_ip_address, headers, body, response, tracing_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) USING TTL ?"
        ),
        "insert_status": p(
            f"INSERT INTO {ks}.api_logs_by_status_day (day, status_code, id, added_on, api, method, "
            f"execution_time, client_ip_address) VALUES (?, ?, ?, ?, ?, ?, ?, ?) USING TTL ?"
        ),
        "incr_counts": p(
            f"UPDATE {ks}.api_log_counts_by_day SET requests = requests + ?, "
            f"exec_time_us = exec_time_us + ? WHERE day = ? AND status_code = ?"
        ),
        "counts_for_day": p(
            f"SELECT status_code, requests, exec_time_us FROM {ks}.api_log_counts_by_day WHERE day = ?"
        ),
        "page_day": p(f"SELECT {SUMMARY_COLUMNS} FROM {ks}.api_logs_by_day WHERE day = ? LIMIT ?"),
        "page_day_before": p(
            f"SELECT {SUMMARY_COLUMNS} FROM {ks}.api_logs_by_day WHERE day = ? AND id < ? LIMIT ?"
        ),
        "page_status": p(
            f"SELECT {SUMMARY_COLUMNS} FROM {ks}.api_logs_by_status_day "
            f"WHERE day = ? AND status_code = ? LIMIT ?"
        ),
        "page_status_before": p(
            f"SELECT {SUMMARY_COLUMNS} FROM {ks}.api_logs_by_status_day "
            f"WHERE day = ? AND status_code = ? AND id < ? LIMIT ?"
        ),
        "get_log": p(f"SELECT * FROM {ks}.api_logs_by_day WHERE day = ? AND id = ?"),
    }


def get_session():
    """Return a shared session, connecting lazily on first use in each process.

    Lazy connection matters under pre-forking servers (gunicorn): the driver's
    I/O threads don't survive fork(), so each worker must connect on its own.
    """
    global _session, _stmts, _last_failure
    if _session is not None:
        return _session
    with _lock:
        if _session is not None:
            return _session
        conf = _conf()
        if time.monotonic() - _last_failure < conf.get("RETRY_AFTER_SECONDS", 30) and _last_failure:
            raise CassandraUnavailable("Cassandra was unreachable recently; retrying shortly.")
        auth = (
            PlainTextAuthProvider(conf["USER"], conf["PASSWORD"]) if conf.get("USER") else None
        )
        profile = ExecutionProfile(
            load_balancing_policy=TokenAwarePolicy(
                DCAwareRoundRobinPolicy(local_dc=conf["LOCAL_DC"])
            ),
            row_factory=dict_factory,
            request_timeout=10,
        )
        cluster = Cluster(
            conf["HOSTS"],
            port=conf["PORT"],
            auth_provider=auth,
            execution_profiles={EXEC_PROFILE_DEFAULT: profile},
            protocol_version=5,
            connect_timeout=5,
        )
        try:
            session = cluster.connect()
            ensure_schema(session)
            _stmts = _prepare(session)
        except Exception as exc:
            _last_failure = time.monotonic()
            cluster.shutdown()
            raise CassandraUnavailable(f"Cannot connect to Cassandra at {conf['HOSTS']}: {exc}") from exc
        _last_failure = 0.0
        _session = session
        return _session


def reset():
    """Drop the shared session (used by tests and after settings changes)."""
    global _session, _stmts, _last_failure
    with _lock:
        if _session is not None:
            _session.cluster.shutdown()
        _session, _stmts, _last_failure = None, {}, 0.0


# --- helpers -----------------------------------------------------------------

def _as_text(value):
    if value in (None, "", {}, []):
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _unset_if_none(*values):
    """Binding None writes a tombstone per null cell; UNSET_VALUE writes nothing.
    Tombstones on every request would slow reads of each day's partition."""
    return tuple(UNSET_VALUE if v is None else v for v in values)


def _aware(dt):
    """Cassandra returns naive UTC datetimes; make them aware."""
    if dt is None:
        return None
    if timezone.is_naive(dt):
        return dt.replace(tzinfo=dt_timezone.utc)
    return dt


def day_of(log_id):
    """Local date (partition key) a timeuuid belongs to."""
    return timezone.localtime(_aware(datetime_from_uuid1(log_id))).date()


def _id_sort_key(row):
    return (row["id"].time, row["id"].bytes)


def _row(row):
    row = dict(row)
    row["added_on"] = _aware(row.get("added_on"))
    return row


# --- writes ------------------------------------------------------------------

def save_log(data, ttl=None, bulk=False):
    """Write one drf-api-logger record. Returns (log_id, futures); nothing blocks.

    ttl:  override the configured TTL in seconds (seeding uses it to age
          backdated rows out on schedule).
    bulk: for bulk loads. Skips the logged batch and the counter update; the
          caller must add the counts itself with add_counts(), which is far
          cheaper than one counter write per row.
    """
    session = get_session()
    added_on = data.get("added_on") or timezone.now()
    if timezone.is_naive(added_on):
        added_on = timezone.make_aware(added_on)
    log_id = uuid_from_time(added_on)
    day = timezone.localtime(added_on).date()
    status_code = int(data.get("status_code") or 0)
    execution_time = float(data.get("execution_time") or 0.0)
    api = data.get("api") or ""
    method = data.get("method") or ""
    client_ip = data.get("client_ip_address")
    if ttl is None:
        ttl = int(_conf().get("LOG_TTL_SECONDS") or 0)
    tracing_id = data.get("tracing_id")

    full_row = (
        _stmts["insert_log"],
        _unset_if_none(
            day, log_id, added_on, api, method, status_code, execution_time, client_ip,
            _as_text(data.get("headers")), _as_text(data.get("body")),
            _as_text(data.get("response")), str(tracing_id) if tracing_id else None, ttl,
        ),
    )
    status_row = (
        _stmts["insert_status"],
        _unset_if_none(day, status_code, log_id, added_on, api, method, execution_time, client_ip, ttl),
    )
    if bulk:
        futures = [session.execute_async(*full_row), session.execute_async(*status_row)]
    else:
        # Logged batch: both copies of the record land, or neither does.
        batch = BatchStatement(batch_type=BatchType.LOGGED)
        batch.add(*full_row)
        batch.add(*status_row)
        futures = [
            session.execute_async(batch),
            # Counters can't share a batch with regular writes.
            session.execute_async(
                _stmts["incr_counts"], (1, int(execution_time * 1_000_000), day, status_code)
            ),
        ]
        for f in futures:
            f.add_errback(lambda exc: logger.error("Failed to write API log to Cassandra: %s", exc))
    return log_id, futures


def add_counts(day, status_code, requests, exec_time_us):
    """Add pre-aggregated totals to the counter table (used by bulk loads)."""
    return get_session().execute_async(
        _stmts["incr_counts"], (int(requests), int(exec_time_us), day, int(status_code))
    )


# --- reads -------------------------------------------------------------------

def day_counts(days):
    """{day: {status_code: {"requests": n, "exec_time_us": us}}} for the given dates."""
    session = get_session()
    futures = {d: session.execute_async(_stmts["counts_for_day"], (d,)) for d in days}
    result = {}
    for d, f in futures.items():
        result[d] = {
            r["status_code"]: {"requests": r["requests"] or 0, "exec_time_us": r["exec_time_us"] or 0}
            for r in f.result()
        }
    return result


def list_logs(plan, by_status, before=None, limit=50):
    """Newest-first page of log summaries.

    plan:      {day: [status codes to read for that day]}; days with an empty
               list are skipped (the counters say they hold nothing relevant).
    by_status: read from the per-status table (a status filter is active) or
               from the per-day table (no filter).
    before:    timeuuid cursor; return rows strictly older than it.

    Returns (rows, next_cursor). next_cursor is None on the last page.
    """
    session = get_session()
    cursor_day = day_of(before) if before else None
    rows = []
    for day in sorted(plan, reverse=True):
        codes = plan[day]
        if not codes or (cursor_day and day > cursor_day):
            continue
        need = limit + 1 - len(rows)
        if need <= 0:
            break
        use_cursor = cursor_day is not None and day == cursor_day
        if by_status:
            futures = [
                session.execute_async(
                    _stmts["page_status_before" if use_cursor else "page_status"],
                    (day, code, before, need) if use_cursor else (day, code, need),
                )
                for code in codes
            ]
        else:
            futures = [
                session.execute_async(
                    _stmts["page_day_before" if use_cursor else "page_day"],
                    (day, before, need) if use_cursor else (day, need),
                )
            ]
        day_rows = [r for f in futures for r in f.result()]
        day_rows.sort(key=_id_sort_key, reverse=True)
        rows.extend(_row(r) for r in day_rows[:need])
    if len(rows) > limit:
        return rows[:limit], rows[limit - 1]["id"]
    return rows, None


def get_log(log_id):
    if not isinstance(log_id, uuid.UUID):
        log_id = uuid.UUID(str(log_id))
    session = get_session()
    found = session.execute(_stmts["get_log"], (day_of(log_id), log_id)).one()
    return _row(found) if found else None
