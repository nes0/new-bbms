"""Top-level URLs. Each app adds its own pages here as it is built."""

from django.contrib import admin
from django.urls import include, path

from apps.core import views as core_views
from apps.signin import views as signin_views

admin.site.site_header = "BBMS administration"
admin.site.site_title = "BBMS admin"

urlpatterns = [
    path("", core_views.home, name="home"),
    path("healthz", core_views.healthz, name="healthz"),
    path("resources/", core_views.resources, name="resources"),
    path("resources/policy/<slug:name>/", core_views.policy, name="policy"),
    path("feedback/", core_views.feedback, name="feedback"),
    # Our login and password reset come first, so they're used instead of Django's (06 §6.11).
    path("accounts/login/", signin_views.LoginView.as_view(), name="login"),
    path("accounts/password_reset/", signin_views.PasswordResetView.as_view(), name="password_reset"),
    path("admin/login/", signin_views.admin_login),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("apps.signin.urls")),
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
    path("", include("apps.equipment.urls")),
]
