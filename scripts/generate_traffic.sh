#!/bin/sh
# Send a mix of requests so the dashboard shows 2xx, 4xx and 5xx entries.
BASE=${BASE:-http://localhost:8001}
N=${1:-20}
for i in $(seq 1 "$N"); do
  curl -s -o /dev/null "$BASE/api/hello/"
  curl -s -o /dev/null -X POST -H 'Content-Type: application/json' -d "{\"name\":\"item-$i\"}" "$BASE/api/items/"
  curl -s -o /dev/null -X POST -H 'Content-Type: application/json' -d '{}' "$BASE/api/items/"
  curl -s -o /dev/null "$BASE/api/items/$((i * 7))/"
  curl -s -o /dev/null -X POST -H 'Content-Type: application/json' -d '{"username":"a","password":"guess"}' "$BASE/api/login/"
  curl -s -o /dev/null "$BASE/api/slow/?ms=120"
  [ $((i % 5)) -eq 0 ] && curl -s -o /dev/null "$BASE/api/boom/"
done
echo "sent traffic to $BASE"
