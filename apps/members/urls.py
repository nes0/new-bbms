from django.urls import path

from . import views

app_name = "members"

urlpatterns = [
    path("register", views.register, name="register"),
    path("account/", views.account, name="account"),
    path("account/id-documents/", views.id_documents_page, name="id-documents"),
    path("account/id-documents/<str:kind>/delete", views.id_document_remove, name="id-document-remove"),
    # Trustees (permission members.review_id_documents)
    path("id-checks/", views.id_checks, name="id-checks"),
    path("id-checks/<int:member_id>/", views.id_check, name="id-check"),
    path("id-checks/documents/<int:document_id>", views.id_document_file, name="id-document-file"),
]
