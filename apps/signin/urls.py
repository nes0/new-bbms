from django.urls import path

from . import views

app_name = "signin"

urlpatterns = [
    path("accounts/two-factor/", views.two_factor_step, name="two-factor"),
    path("account/two-factor/", views.manage, name="manage"),
    path("account/two-factor/recovery-codes/", views.new_codes, name="new-codes"),
    path("account/two-factor/off/", views.turn_off, name="turn-off"),
]
