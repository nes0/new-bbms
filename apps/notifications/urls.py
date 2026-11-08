from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("notifications/", views.notifications, name="list"),
    path("notifications/read-all", views.mark_all_read, name="read-all"),
    path("notifications/<int:notification_id>/", views.open_notification, name="open"),
    path("notifications/<int:notification_id>/<str:state>", views.mark, name="mark"),
]
