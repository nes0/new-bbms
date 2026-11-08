from django.urls import path

from . import views

app_name = "moodle"

urlpatterns = [
    path("account/training/", views.training, name="training"),
    path("account/training/<int:course_id>/check", views.check_now, name="check-now"),
]
