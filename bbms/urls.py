"""Top-level URLs. Each app adds its own pages here as it is built."""

from django.contrib import admin
from django.urls import include, path

from apps.core import views as core_views

admin.site.site_header = "BBMS administration"
admin.site.site_title = "BBMS admin"

urlpatterns = [
    path("", core_views.home, name="home"),
    path("healthz", core_views.healthz, name="healthz"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("apps.members.urls")),
    path("admin/", admin.site.urls),
    # Device API v1 lives at /acs/... (the address today's door firmware uses).
    path("", include("apps.access.urls")),
    path("", include("apps.payments.urls")),
    path("", include("apps.storage.urls")),
    path("", include("apps.billing.urls")),
    path("", include("apps.notifications.urls")),
    path("", include("apps.moodle.urls")),
    path("o/", include("apps.moodle.oidc_urls")),
    path("", include("apps.profiles.urls")),
    path("", include("apps.discord.urls")),
    path("", include("apps.proposals.urls")),
    path("", include("apps.expenses.urls")),
    path("", include("apps.finance.urls")),
]
