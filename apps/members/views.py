"""Joining and proof of identity (spec 03 §3.1). The account page itself is in account_views.py."""

import logging

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Count, Min
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_http_methods, require_POST

from . import email_confirmation, id_documents, induction
from .forms import IdCheckForm, IdDocumentsForm, RegistrationForm
from .models import IdDocument, Member

logger = logging.getLogger(__name__)
review_permission = permission_required("members.review_id_documents", raise_exception=True)
UPLOADS_OFF = "Uploading isn't switched on yet. Please try again later, or ask a trustee."


@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("members:account")
    form = RegistrationForm(request.POST) if request.method == "POST" else RegistrationForm()
    if request.method == "POST" and form.is_valid():
        member = form.save()
        email_confirmation.send_confirmation(member)
        login(request, member, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(request, "Welcome! Your account is set up. Here's what to do next.")
        return redirect("members:account")
    return render(request, "members/register.html", {"form": form})


# --- Proof of identity: the member's side ---


@csrf_exempt  # CSRF is checked below, once the upload handlers are set (they must be set before the body is read)
@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def id_documents_page(request):
    if request.method == "POST" and not id_documents.encryption_ready():
        messages.error(request, UPLOADS_OFF)
        return redirect("members:id-documents")
    if request.method == "POST":
        if int(request.META.get("CONTENT_LENGTH") or 0) > id_documents.MAX_REQUEST_BYTES:
            messages.error(request, "Those files are too big: the limit is 10 MB each.")
            return redirect("members:id-documents")
        request.upload_handlers = [id_documents.InMemoryUploadHandler(request)]
    return _id_documents_page(request)


@csrf_protect
def _id_documents_page(request):
    member = request.user
    form = IdDocumentsForm(request.POST, request.FILES) if request.method == "POST" else IdDocumentsForm()
    if request.method == "POST" and form.is_valid():
        form.save(member)
        messages.success(request, "Thank you. A trustee will check your documents, then delete them.")
        return redirect("members:id-documents")
    context = {
        "form": form,
        "documents": member.id_documents.defer("data").order_by("kind"),
        "id_status": id_documents.status(member),
        "id_check": member.id_checks.first(),
        "max_age_days": id_documents.max_age_days(),
        "uploads_off": UPLOADS_OFF if not id_documents.encryption_ready() else "",
    }
    return render(request, "members/id_documents.html", context)


@login_required
@require_POST
def id_document_remove(request, kind):
    """A member can take back an upload before it's checked."""
    request.user.id_documents.filter(kind=kind).delete()
    messages.success(request, "Deleted.")
    return redirect("members:id-documents")


# --- Proof of identity: the trustees' side ---


@login_required
@review_permission
@never_cache
def id_checks(request):
    waiting = (
        Member.objects.filter(id_documents__isnull=False)
        .annotate(documents=Count("id_documents"), uploaded_at=Min("id_documents__uploaded_at"))
        .order_by("uploaded_at")
    )
    return render(request, "members/id_checks.html", {"waiting": waiting, "max_age_days": id_documents.max_age_days()})


@login_required
@review_permission
@never_cache
@require_http_methods(["GET", "POST"])
def id_check(request, member_id):
    member = get_object_or_404(Member, pk=member_id)
    documents = member.id_documents.defer("data").order_by("kind")
    form = IdCheckForm(request.POST) if request.method == "POST" else IdCheckForm()
    if request.method == "POST" and documents and form.is_valid():
        check = id_documents.record_check(member, request.user, form.cleaned_data["result"], form.cleaned_data["note"])
        logger.info("ID check for member %s by %s: %s; documents deleted", member.pk, request.user.pk, check.result)
        messages.success(request, f"Recorded for {member.full_name}. Their documents have been deleted.")
        return redirect("members:id-checks")
    context = {
        "subject": member,
        "address": member.addresses.first(),
        "documents": documents,
        "form": form,
        "history": member.id_checks.select_related("checked_by"),
    }
    return render(request, "members/id_check.html", context)


@login_required
@review_permission
@never_cache
def id_document_file(request, document_id):
    document = get_object_or_404(IdDocument, pk=document_id)
    try:
        data = id_documents.decrypt(document.data)
    except id_documents.CannotDecrypt:
        logger.error(
            "ID document %s (member %s) can't be decrypted with ID_DOCUMENT_KEYS", document.pk, document.member_id
        )
        message = (
            "This document can't be opened: the key it was encrypted with is no longer set. "
            "Record it as not accepted and ask the member to send it again."
        )
        return HttpResponse(message, status=410, content_type="text/plain; charset=utf-8")
    logger.info("ID document %s (member %s) viewed by %s", document.pk, document.member_id, request.user.pk)
    response = HttpResponse(data, content_type=document.content_type)
    disposition = "inline" if document.content_type in id_documents.SHOWN_INLINE else "attachment"
    response["Content-Disposition"] = f'{disposition}; filename="{document.filename}"'
    response["Cache-Control"] = "no-store, private"
    response["Referrer-Policy"] = "no-referrer"
    response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    return response


# --- The general induction: confirming it (05 §5.9; the member's side is in account_views.py) ---

confirm_permission = permission_required("members.confirm_general_inductions", raise_exception=True)


@login_required
@confirm_permission
def general_inductions(request):
    return render(request, "members/general_inductions.html", {"waiting": induction.waiting()})


@login_required
@confirm_permission
@require_POST
def confirm_induction(request, member_id):
    member = get_object_or_404(Member, pk=member_id)
    try:
        member = induction.confirm(member, request.user)
    except induction.InductionError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"Confirmed {member.full_name}'s general induction.")
    return redirect("members:general-inductions")
