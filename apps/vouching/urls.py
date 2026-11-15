from django.urls import path

from . import views

app_name = "vouching"

urlpatterns = [
    path("vouching/", views.index, name="index"),
    path("vouching/<int:member_id>/", views.choose, name="choose"),
]
