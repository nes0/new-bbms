from django.urls import path

from . import views, webhooks

app_name = "billing"

urlpatterns = [
    path("account/payment/", views.payment, name="payment"),
    path("account/payment/direct-debit/", views.direct_debit_start, name="direct-debit-start"),
    path("account/payment/direct-debit/done/", views.direct_debit_done, name="direct-debit-done"),
    path("account/payment/credit/", views.credit_start, name="credit-start"),
    path("account/payment/paypal/", views.paypal_start, name="paypal-start"),
    path("account/payment/method/", views.change_method, name="change-method"),
    path("account/charges/<int:charge_id>/pay/", views.pay_charge, name="pay-charge"),
    path("account/rejoin/", views.rejoin, name="rejoin"),
    # GoCardless calls this (spec 04 §4.2).
    path("gocardless/webhook", webhooks.webhook, name="gocardless-webhook"),
    # Stands in for GoCardless's pages when developing without a token (DEBUG only).
    path("dev/gocardless/payments/", views.fake_gocardless_payments, name="fake-gocardless-payments"),
    path("dev/gocardless/<str:request_id>/", views.fake_gocardless, name="fake-gocardless"),
]
