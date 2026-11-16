"""Small demo API that produces a spread of status codes for the log dashboard."""
import itertools
import time

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

_items = {}
_ids = itertools.count(1)


@api_view(["GET"])
def hello(request):
    return Response({"message": "hello"})


@api_view(["GET", "POST"])
def items(request):
    if request.method == "POST":
        name = (request.data or {}).get("name")
        if not name:
            return Response({"name": ["This field is required."]}, status=status.HTTP_400_BAD_REQUEST)
        item = {"id": next(_ids), "name": name}
        _items[item["id"]] = item
        return Response(item, status=status.HTTP_201_CREATED)
    return Response(list(_items.values()))


@api_view(["GET", "DELETE"])
def item_detail(request, pk):
    item = _items.get(pk)
    if item is None:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "DELETE":
        del _items[pk]
        return Response(status=status.HTTP_204_NO_CONTENT)
    return Response(item)


@api_view(["POST"])
def login(request):
    # "password" is masked by DRF_API_LOGGER_EXCLUDE_KEYS before it is stored.
    if (request.data or {}).get("password") != "demo":
        return Response({"detail": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)
    return Response({"token": "not-a-real-token"})


@api_view(["GET"])
def slow(request):
    ms = min(int(request.GET.get("ms", 300)), 5000)
    time.sleep(ms / 1000)
    return Response({"slept_ms": ms})


@api_view(["GET"])
def boom(request):
    raise RuntimeError("intentional failure for testing 500 logs")
