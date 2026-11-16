import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger("api.errors")


def json_exception_handler(exc, context):
    """Return JSON for unhandled exceptions too.

    drf-api-logger only logs responses with a JSON content type, so Django's
    default HTML 500 page would otherwise never reach Cassandra.
    """
    response = exception_handler(exc, context)
    if response is None:
        logger.exception("unhandled exception in %s", context["view"].__class__.__name__)
        response = Response(
            {"detail": "Internal server error."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return response
