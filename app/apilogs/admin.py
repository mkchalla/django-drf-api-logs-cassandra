"""Django admin dashboard for API logs stored in Cassandra.

The admin normally lists a Django model's queryset. Here the data lives in
Cassandra, so APILogAdmin swaps the changelist and detail pages for views that
query Cassandra through cassandra_store and render admin-styled templates.
"""
import uuid
from collections import defaultdict

from django.conf import settings
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.template.response import TemplateResponse
from django.urls import path
from django.utils import timezone

from . import cassandra_store as store
from .filters import (
    DEFAULT_PERIOD,
    PERIODS,
    STATUS_CLASSES,
    chart_buckets,
    days_for_period,
    parse_status,
    status_class,
    status_matches,
)
from .models import APILog

PAGE_SIZE = 50


def _query(request, **changes):
    params = request.GET.copy()
    params.pop("before", None)  # any filter change restarts paging
    for key, value in changes.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = value
    encoded = params.urlencode()
    return f"?{encoded}" if encoded else "?"


@admin.register(APILog)
class APILogAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        info = self.opts.app_label, self.opts.model_name
        wrap = self.admin_site.admin_view
        return [
            path("", wrap(self.dashboard_view), name="%s_%s_changelist" % info),
            path("<uuid:log_id>/", wrap(self.detail_view), name="%s_%s_detail" % info),
        ]

    def _base_context(self, request, title):
        return {
            **self.admin_site.each_context(request),
            "opts": self.opts,
            "title": title,
            "has_view_permission": True,
        }

    # --- dashboard ------------------------------------------------------------

    def dashboard_view(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied

        period = request.GET.get("period", DEFAULT_PERIOD)
        if period not in dict(PERIODS):
            period = DEFAULT_PERIOD
        status_param = request.GET.get("status", "")
        status_filter = parse_status(status_param)

        before = None
        if request.GET.get("before"):
            try:
                before = uuid.UUID(request.GET["before"])
                if before.version != 1:
                    before = None
            except ValueError:
                before = None

        today = timezone.localdate()
        retention_days = (settings.CASSANDRA.get("LOG_TTL_SECONDS") or 0) // 86400
        days = days_for_period(period, today, retention_days)
        context = self._base_context(request, "API logs")
        context.update(
            period_label=dict(PERIODS)[period],
            time_zone=timezone.get_current_timezone_name(),
            range_start=days[0],
            range_end=days[-1],
            periods=[
                {"label": label, "url": _query(request, period=key), "selected": key == period}
                for key, label in PERIODS
            ],
        )

        try:
            counts = store.day_counts(days)
        except store.CassandraUnavailable as exc:
            context["error"] = str(exc)
            return TemplateResponse(request, "admin/apilogs/dashboard.html", context)

        # Status choices, with counts for the whole period (ignoring the status filter).
        by_code = defaultdict(int)
        by_class = defaultdict(int)
        for day_data in counts.values():
            for code, c in day_data.items():
                by_code[code] += c["requests"]
                by_class[status_class(code)] += c["requests"]
        status_choices = [
            {"label": "All", "url": _query(request, status=None),
             "selected": status_filter is None, "count": sum(by_code.values())},
        ]
        for cls in STATUS_CLASSES:
            status_choices.append({
                "label": cls, "url": _query(request, status=cls), "cls": cls,
                "selected": status_filter == cls, "count": by_class.get(cls, 0),
            })
        code_choices = [
            {"label": str(code), "url": _query(request, status=code), "cls": status_class(code),
             "selected": status_filter == code, "count": n}
            for code, n in sorted(by_code.items())
        ]

        # Summary + chart for the rows matching the status filter.
        total = errors = server_errors = exec_us = 0
        chart = []
        for bucket in chart_buckets(days):
            segments = defaultdict(int)
            for day in bucket:
                for code, c in counts.get(day, {}).items():
                    if not status_matches(code, status_filter):
                        continue
                    n = c["requests"]
                    total += n
                    exec_us += c["exec_time_us"]
                    if code >= 400:
                        errors += n
                    if code >= 500:
                        server_errors += n
                    segments[status_class(code)] += n
            chart.append({
                "day": bucket[0], "end": bucket[-1], "days": len(bucket),
                "total": sum(segments.values()), "segments": segments,
            })
        chart_weekly = len(chart) < len(days)
        chart_max = max((d["total"] for d in chart), default=0) or 1
        for d in chart:
            d["height"] = round(d["total"] * 100 / chart_max, 1)
            d["segments"] = [
                {"cls": cls, "count": d["segments"][cls],
                 "pct": round(d["segments"][cls] * 100 / d["total"], 2) if d["total"] else 0}
                for cls in STATUS_CLASSES if d["segments"].get(cls)
            ]
        breakdown_max = max((c["count"] for c in code_choices), default=0) or 1
        breakdown = [
            {**c, "pct": round(c["count"] * 100 / breakdown_max, 1)}
            for c in code_choices if status_matches(int(c["label"]), status_filter)
        ]

        # Log rows: only read partitions the counters say contain matching rows.
        plan = {
            day: [code for code in counts.get(day, {}) if status_matches(code, status_filter)]
            for day in days
        }
        rows, next_cursor = store.list_logs(
            plan, by_status=status_filter is not None, before=before, limit=PAGE_SIZE
        )
        for row in rows:
            row["status_class"] = status_class(row["status_code"])
            row["execution_ms"] = round((row.get("execution_time") or 0) * 1000, 1)

        context.update(
            status_choices=status_choices,
            code_choices=code_choices,
            status_filter=status_filter,
            summary={
                "total": total,
                "errors": errors,
                "server_errors": server_errors,
                "error_rate": round(errors * 100 / total, 1) if total else 0,
                "avg_ms": round(exec_us / total / 1000, 1) if total else 0,
            },
            chart=chart,
            chart_max=chart_max,
            chart_weekly=chart_weekly,
            breakdown=breakdown,
            rows=rows,
            paged=before is not None,
            first_page_url=_query(request),
            next_page_url=_query(request, before=str(next_cursor)) if next_cursor else None,
            current_query=request.GET.urlencode(),
        )
        return TemplateResponse(request, "admin/apilogs/dashboard.html", context)

    # --- detail ---------------------------------------------------------------

    def detail_view(self, request, log_id):
        if not self.has_view_permission(request):
            raise PermissionDenied
        if log_id.version != 1:
            raise Http404("Not an API log id.")
        context = self._base_context(request, "API log")
        try:
            log = store.get_log(log_id)
        except store.CassandraUnavailable as exc:
            context["error"] = str(exc)
            return TemplateResponse(request, "admin/apilogs/detail.html", context)
        if log is None:
            raise Http404("API log not found (it may have expired).")
        log["status_class"] = status_class(log["status_code"])
        log["execution_ms"] = round((log.get("execution_time") or 0) * 1000, 1)
        # "back" carries the dashboard's query string so filters survive the round trip.
        back = request.GET.get("back", "")
        context.update(log=log, back_url=f"../?{back}" if back else "../")
        return TemplateResponse(request, "admin/apilogs/detail.html", context)
