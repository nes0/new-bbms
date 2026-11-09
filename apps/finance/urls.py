from django.urls import path

from . import views

app_name = "finance"

urlpatterns = [
    path("finance/", views.overview, name="overview"),
    path("finance/income/", views.income, name="income"),
    path("finance/machines/", views.machines, name="machines"),
    path("finance/received/", views.received, name="received"),
    path("finance/payouts/", views.payouts_page, name="payouts"),
    path("finance/payouts/<int:payout_id>/", views.payout, name="payout"),
    path("finance/payments/", views.payments, name="payments"),
    path("finance/payments/<int:payment_id>/", views.payment, name="payment"),
    path("finance/payments/<int:payment_id>/put-right/", views.put_right, name="put-right"),
    path("finance/charges/", views.charges, name="charges"),
    path("finance/statements/", views.statements_page, name="statements"),
    path("finance/statements/<int:statement_id>/", views.statement, name="statement"),
    path("finance/bank/", views.bank_lines, name="bank-lines"),
    path("finance/bank/<int:line_id>/", views.bank_line, name="bank-line"),
    path("finance/matches/", views.matches, name="matches"),
    path("stats/", views.stats, name="stats"),
]
