from django.urls import path

from . import views

app_name = "storage"

urlpatterns = [
    path("lockers/", views.lockers, name="lockers"),
]
