from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("credit/", views.credit, name="credit"),
]
