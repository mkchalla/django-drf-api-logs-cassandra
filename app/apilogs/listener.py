"""Receives drf-api-logger's API_LOGGER_SIGNAL and stores each record in Cassandra."""
import logging

from . import cassandra_store

logger = logging.getLogger(__name__)


def store_api_log(**data):
    # The signal fires inside the request/response cycle. save_log only queues
    # async writes, so the request isn't held up waiting for Cassandra, and any
    # failure is logged rather than breaking the API response.
    try:
        cassandra_store.save_log(data)
    except cassandra_store.CassandraUnavailable as exc:
        logger.warning("API log dropped: %s", exc)
    except Exception:
        logger.exception("API log dropped: unexpected error writing to Cassandra")
