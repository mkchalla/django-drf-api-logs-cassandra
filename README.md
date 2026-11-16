# DRF API logs in Cassandra, with a Django admin dashboard

A Django REST Framework project where [drf-api-logger](https://github.com/vishalanandl177/DRF-API-Logger)
captures every API call and stores it in **Cassandra**. The Django admin shows the logs as a
dashboard you can filter **by status code** and **by date** (Today, Since yesterday, Last 7 days,
Last month).

## Quick start (Docker)

```bash
docker compose up -d --build
./scripts/generate_traffic.sh 20                                  # some live API calls
docker compose exec web python manage.py seed_api_logs             # optional: 1M backdated logs over 180 days
```

Open http://localhost:8001/admin/ and sign in with the dev account from `docker-compose.yml`
(`admin` / `admin` — change it). The dashboard is at **API logs**.

## Run locally (Cassandra in Docker, Django on your machine)

```bash
docker compose up -d cassandra
cd app
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py init_cassandra      # waits for Cassandra, creates keyspace + tables
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver 8002
.venv/bin/python manage.py test apilogs         # integration tests use keyspace api_logs_test
```

## Demo data

`seed_api_logs` bulk-loads fake logs (default **1,000,000 over 180 days**; a few minutes on a laptop):

```bash
python manage.py seed_api_logs                          # 1M over 180 days
python manage.py seed_api_logs --count 5000 --days 35   # small set
python manage.py seed_api_logs --seed 42                # repeatable data
```

* Traffic looks real: volume grows ~2.5x across the period, weekends are quieter, requests peak in
  the afternoon, and each endpoint has its own mix of 2xx/3xx/4xx/5xx and response times.
* Several worker processes (`--workers`) each keep up to `--concurrency` writes in flight. Rows skip
  the logged batch, and counters are summed in memory and written once per day and status code.
* Each row gets the TTL it would have left if it had been logged live, so seeded data ages out on
  schedule. Rows already older than `API_LOG_TTL_DAYS` are skipped.
* Running it again adds more rows; it doesn't replace what's there.

## How it works

```
request ─► APILoggerMiddleware (drf-api-logger)
              │  DRF_API_LOGGER_DATABASE = False, DRF_API_LOGGER_SIGNAL = True
              ▼
           API_LOGGER_SIGNAL ─► apilogs/listener.py ─► cassandra_store.save_log()  (execute_async)
                                                                    │
Django admin ◄── apilogs/admin.py (custom changelist + detail) ◄────┘ reads Cassandra
```

* **No SQL table for logs.** SQLite only holds Django's own users/sessions. `apilogs.APILog` is an
  unmanaged placeholder model that gives the dashboard an admin entry and a `view` permission.
* **Writes don't block requests.** The signal handler queues async writes; if Cassandra is down the
  log is dropped with a warning (and reconnects are throttled to once every 30 s) — the API keeps working.
* Sensitive keys (`password`, `token`, …) are masked by drf-api-logger before storage
  (`DRF_API_LOGGER_EXCLUDE_KEYS`). Only JSON responses are logged (a drf-api-logger rule), which is
  why `api/exceptions.py` turns unhandled errors into JSON 500s.

### Cassandra data model

Each dashboard query has a table shaped for it:

| Table | Primary key | Used for |
|---|---|---|
| `api_logs_by_day` | `((day), id)` | unfiltered list, detail page (full record) |
| `api_logs_by_status_day` | `((day, status_code), id)` | list when a status filter is active |
| `api_log_counts_by_day` (counters) | `((day), status_code)` | totals, chart, filter counts, avg latency |

* `day` is the local date in `DJANGO_TIME_ZONE`, so every date filter is a set of whole partitions.
  Choose the time zone once; changing it later shifts which partition "today" maps to.
* `id` is a timeuuid, clustered `DESC`: rows come back newest first and the id doubles as the paging
  cursor (`?before=<id>`).
* The counters tell the list query which `(day, status_code)` partitions hold data, so empty days are skipped.
* Log rows expire after `API_LOG_TTL_DAYS` (default 180; `0` keeps them forever). Counters don't expire.

### Filters

| Filter | Range (inclusive, local dates) |
|---|---|
| Today | today |
| Since yesterday | yesterday – today |
| Last 7 days (default) | today − 6 – today |
| Last month | same day last month – today |
| Last 3 months | same day 3 months ago – today |
| All | the retention window: last `API_LOG_TTL_DAYS` days (365 if logs never expire) |

Ranges longer than 62 days are charted per week (the newest bar ends today) instead of per day.
Wide ranges stay cheap: the totals and chart read one small counter partition per day (180 days ≈
20 ms), and the log table stops after 50 rows, skipping days the counters show are empty.

Status filters: a class (`2xx`, `3xx`, `4xx`, `5xx`) or any exact code seen in the period, each
with its request count. Filters combine, e.g. `?period=today&status=5xx`.

## Configuration

| Env var | Default |
|---|---|
| `CASSANDRA_HOSTS` | `127.0.0.1` (comma-separated) |
| `CASSANDRA_PORT` | `9042` |
| `CASSANDRA_KEYSPACE` | `api_logs` |
| `CASSANDRA_LOCAL_DC` | `datacenter1` |
| `CASSANDRA_REPLICATION_FACTOR` | `1` (SimpleStrategy; use NetworkTopologyStrategy in production) |
| `CASSANDRA_USER` / `CASSANDRA_PASSWORD` | unset |
| `API_LOG_TTL_DAYS` | `180` |
| `DJANGO_TIME_ZONE` | `UTC` |

## Notes for production

* One partition per day holds every request of that day. Beyond a few million requests/day, add a
  bucket (e.g. hour) to the partition key of the two log tables.
* Gunicorn runs without `--preload`, so each worker opens its own Cassandra connection after fork.
