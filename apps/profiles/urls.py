from django.urls import path

from . import views

app_name = "profiles"

urlpatterns = [
    path("members/", views.directory, name="directory"),
    path("members/<int:member_id>/", views.member_page, name="member"),
    path("members/<int:member_id>/photo/<str:size>/", views.photo, name="photo"),
    path("account/profile/", views.edit, name="edit"),
    path("account/profile/photo/delete/", views.remove_photo, name="remove-photo"),
    path("groups/", views.groups, name="groups"),
    path("groups/<str:name>/", views.group, name="group"),
    path("credits/", views.credits, name="credits"),
]
