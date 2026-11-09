from django.urls import path

from . import views

app_name = "group_email"

urlpatterns = [
    path("group-email/", views.compose, name="compose"),
]
