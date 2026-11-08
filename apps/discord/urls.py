from django.urls import path

from . import views

app_name = "discord"

urlpatterns = [
    path("account/discord/", views.page, name="page"),
    path("account/discord/connect/", views.connect, name="connect"),
    path("account/discord/done/", views.done, name="done"),
    path("account/discord/unlink/", views.unlink, name="unlink"),
    # Stands in for Discord's page when developing without the DISCORD_* settings (DEBUG only).
    path("dev/discord/", views.fake_discord, name="fake-discord"),
]
