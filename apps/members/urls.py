from django.urls import path

from . import account_views, views

app_name = "members"

urlpatterns = [
    path("register", views.register, name="register"),
    # The member's own account. Changing the password uses Django's page at /accounts/password_change/.
    path("account/", account_views.account, name="account"),
    path("account/details/", account_views.edit_details, name="edit-details"),
    path("account/email/", account_views.change_email, name="change-email"),
    path("account/confirm-email/resend", account_views.resend_confirmation, name="resend-confirmation"),
    path("account/confirm-email/<str:token>/", account_views.confirm_email, name="confirm-email"),
    path("account/address/", account_views.change_address, name="change-address"),
    path("account/subscription/", account_views.change_subscription, name="change-subscription"),
    path("account/leave/", account_views.leave, name="leave"),
    path("account/induction/", account_views.general_induction, name="induction"),
    path("account/id-documents/", views.id_documents_page, name="id-documents"),
    path("account/id-documents/<str:kind>/delete", views.id_document_remove, name="id-document-remove"),
    # Trustees (permission members.review_id_documents)
    path("id-checks/", views.id_checks, name="id-checks"),
    path("id-checks/<int:member_id>/", views.id_check, name="id-check"),
    path("id-checks/documents/<int:document_id>", views.id_document_file, name="id-document-file"),
    # The comms role (permission members.confirm_general_inductions)
    path("general-inductions/", views.general_inductions, name="general-inductions"),
    path("general-inductions/<int:member_id>/confirm", views.confirm_induction, name="confirm-induction"),
]
