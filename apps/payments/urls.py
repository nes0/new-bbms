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
    path("pay/paypal/", views.pay_by_paypal, name="pay-paypal"),
    path("pay/paypal/done/", views.paypal_done, name="paypal-done"),
    path("pay/paypal/cancelled/", views.paypal_cancelled, name="paypal-cancelled"),
    # PayPal's notifications (IPN, spec 04 §4.4): the same address as the legacy system.
    path("paypal-ipn", views.paypal_notification, name="paypal-ipn"),
    # Stand in for PayPal's pages when developing without a PayPal account (DEBUG only).
    path("dev/paypal/", views.fake_paypal, name="fake-paypal"),
    path("dev/paypal/tools/", views.fake_paypal_tools, name="fake-paypal-tools"),
    # Stripe calls this (spec 04 §4.3).
    path("stripe/webhook", views.stripe_webhook, name="stripe-webhook"),
    # Stands in for Stripe's Checkout page when developing without a key (DEBUG only).
    path("dev/stripe/<str:session_id>/", views.fake_stripe, name="fake-stripe"),
]
