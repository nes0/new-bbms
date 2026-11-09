from django.urls import path

from . import views

app_name = "equipment"

urlpatterns = [
    path("equipment/", views.index, name="index"),
    path("equipment/retired/", views.retired, name="retired"),
    path("equipment/<slug:slug>/", views.detail, name="detail"),
    path("equipment/<slug:slug>/status/", views.report, name="report"),
    path("equipment/<slug:slug>/service/", views.service, name="service"),
    path("equipment/<slug:slug>/sessions/<int:session_id>/reclassify/", views.reclassify, name="reclassify"),
]
