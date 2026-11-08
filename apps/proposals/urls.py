from django.urls import path

from . import views

app_name = "proposals"

urlpatterns = [
    path("proposals/", views.index, name="index"),
    path("proposals/<int:proposal_id>/", views.detail, name="detail"),
    path("proposals/<int:proposal_id>/vote/", views.vote, name="vote"),
]
