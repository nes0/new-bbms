from django.urls import path

from . import views

app_name = "expenses"

urlpatterns = [
    path("expenses/", views.index, name="index"),
    path("expenses/claim/", views.claim, name="claim"),
    path("expenses/<int:expense_id>/", views.detail, name="detail"),
    path("expenses/<int:expense_id>/receipt/", views.receipt, name="receipt"),
    path("expenses/<int:expense_id>/decide/", views.decide, name="decide"),
    path("expenses/<int:expense_id>/withdraw/", views.withdraw, name="withdraw"),
    path("expenses/<int:expense_id>/machine/", views.set_machine, name="set-machine"),
]
