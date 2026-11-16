#!/bin/sh
set -e
python manage.py migrate --noinput
python manage.py init_cassandra --wait 180
if [ -n "$DJANGO_SUPERUSER_USERNAME" ]; then
  python manage.py createsuperuser --noinput 2>/dev/null || true
fi
# No --preload: each worker must open its own Cassandra connection after fork.
exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 4 --access-logfile -
