from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

admin.site.site_header = "API Logs (Cassandra)"
admin.site.site_title = "API Logs"
admin.site.index_title = "Administration"

urlpatterns = [
    path("", RedirectView.as_view(url="/admin/apilogs/apilog/", permanent=False)),
    path("admin/", admin.site.urls),
    path("api/", include("api.urls")),
]
