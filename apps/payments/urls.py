from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("credit/", views.credit, name="credit"),
    path("pay/", views.pay, name="pay"),
    path("pay/card/", views.pay_by_card, name="pay-card"),
    path("pay/card/done/", views.card_done, name="card-done"),
    path("pay/card/cancelled/", views.card_cancelled, name="card-cancelled"),
    path("pay/credit/", views.pay_with_credit, name="pay-credit"),
    path("pay/direct-debit/", views.pay_with_direct_debit, name="pay-direct-debit"),
    # Stripe calls this (spec 04 §4.3).
    path("stripe/webhook", views.stripe_webhook, name="stripe-webhook"),
    # Stands in for Stripe's Checkout page when developing without a key (DEBUG only).
    path("dev/stripe/<str:session_id>/", views.fake_stripe, name="fake-stripe"),
]
