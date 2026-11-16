FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SQLITE_PATH=/data/db.sqlite3

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /data && DJANGO_DEBUG=false python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["./entrypoint.sh"]
