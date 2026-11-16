from django.apps import AppConfig


class ApiLogsConfig(AppConfig):
    name = "apilogs"
    verbose_name = "API logs"

    def ready(self):
        from drf_api_logger import API_LOGGER_SIGNAL

        from .listener import store_api_log

        if store_api_log not in API_LOGGER_SIGNAL.listen.targets:
            API_LOGGER_SIGNAL.listen += store_api_log
