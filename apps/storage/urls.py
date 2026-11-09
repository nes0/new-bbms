from django.urls import path

from . import views

app_name = "storage"

urlpatterns = [
    path("lockers/", views.lockers, name="lockers"),
    path("lockers/<int:locker_id>/rent/", views.rent, name="rent"),
    path("lockers/rentals/<int:rental_id>/give-up/", views.give_up, name="give-up"),
]
