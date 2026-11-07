"""Proof of identity: members upload, trustees check, documents are deleted (spec 03 §3.1)."""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from . import id_documents
from .models import IdCheck, IdDocument, Member

PDF = b"%PDF-1.7\n" + b"x" * 100
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100
HEIC = b"\x00\x00\x00\x18ftypheic" + b"x" * 100
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"x" * 100


def person(email, **fields):
    return Member.objects.create_user(email, "a-good-password", given_name="Kim", family_name="Maker", **fields)


def upload(name, data):
    return SimpleUploadedFile(name, data, content_type="application/octet-stream")


class SniffingTests(TestCase):
    def test_accepted_types_are_recognised_from_their_contents(self):
        cases = [
            (PDF, "application/pdf"),
            (JPEG, "image/jpeg"),
            (PNG, "image/png"),
            (WEBP, "image/webp"),
            (HEIC, "image/heic"),
        ]
        for data, expected in cases:
            self.assertEqual(id_documents.sniff_content_type(data), expected)

    def test_anything_else_is_refused(self):
        for data in (b"", b"<html><script>", b"PK\x03\x04 a zip or docx", b"GIF89a"):
            self.assertIsNone(id_documents.sniff_content_type(data))

    def test_filenames_are_made_safe(self):
        self.assertEqual(id_documents.safe_filename('C:\\scans\\my "passport".pdf'), "my _passport_.pdf")
        self.assertEqual(id_documents.safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(id_documents.safe_filename(""), "document")


class MemberUploadTests(TestCase):
    def setUp(self):
        self.member = person("kim@example.com")
        self.client.force_login(self.member)

    def send(self, **files):
        return self.client.post("/account/id-documents/", files)

    def test_both_documents_can_be_sent_together(self):
        response = self.send(photo_id=upload("passport.jpg", JPEG), proof_of_address=upload("bill.pdf", PDF))
        self.assertRedirects(response, "/account/id-documents/")
        stored = {d.kind: d for d in IdDocument.objects.all()}
        self.assertEqual(stored["photo_id"].content_type, "image/jpeg")
        self.assertEqual(bytes(stored["proof_of_address"].data), PDF)
        self.assertEqual(id_documents.status(self.member), "waiting")

    def test_one_can_be_sent_at_a_time_and_sending_again_replaces_it(self):
        self.send(photo_id=upload("first.png", PNG))
        self.send(photo_id=upload("second.pdf", PDF))
        document = IdDocument.objects.get()
        self.assertEqual((document.filename, document.content_type), ("second.pdf", "application/pdf"))

    def test_the_type_comes_from_the_contents_not_the_name(self):
        response = self.send(photo_id=upload("passport.pdf", b"<html><script>alert(1)</script>"))
        self.assertContains(response, "Please send a PDF or a photo")
        self.assertFalse(IdDocument.objects.exists())

    def test_files_over_10_mb_are_refused(self):
        big = JPEG + b"x" * id_documents.MAX_FILE_BYTES
        response = self.send(photo_id=upload("huge.jpg", big))
        self.assertContains(response, "too big")
        self.assertFalse(IdDocument.objects.exists())

    def test_a_request_too_big_for_two_files_is_refused_before_reading_it(self):
        with mock.patch.object(id_documents, "MAX_REQUEST_BYTES", 50):
            response = self.send(photo_id=upload("passport.jpg", JPEG))
        self.assertRedirects(response, "/account/id-documents/")
        self.assertFalse(IdDocument.objects.exists())

    def test_sending_nothing_asks_for_a_file(self):
        self.assertContains(self.send(), "Choose a file to send")

    def test_uploads_stay_in_memory(self):
        request_handlers = []
        original = id_documents.InMemoryUploadHandler.__init__

        def spy(handler, *args, **kwargs):
            request_handlers.append(handler)
            original(handler, *args, **kwargs)

        with mock.patch.object(id_documents.InMemoryUploadHandler, "__init__", spy):
            self.send(photo_id=upload("passport.jpg", JPEG + b"x" * (3 * 1024 * 1024)))  # over Django's 2.5 MB
        self.assertEqual(len(request_handlers), 1)
        self.assertEqual(IdDocument.objects.get().size, len(JPEG) + 3 * 1024 * 1024)

    def test_a_member_can_delete_an_upload_before_it_is_checked(self):
        self.send(photo_id=upload("passport.jpg", JPEG))
        self.client.post("/account/id-documents/photo_id/delete")
        self.assertFalse(IdDocument.objects.exists())

    def test_members_cannot_see_the_checking_pages_or_files(self):
        self.send(photo_id=upload("passport.jpg", JPEG))
        document = IdDocument.objects.get()
        self.assertEqual(self.client.get("/id-checks/").status_code, 403)
        self.assertEqual(self.client.get(f"/id-checks/{self.member.pk}/").status_code, 403)
        self.assertEqual(self.client.get(f"/id-checks/documents/{document.pk}").status_code, 403)

    def test_the_page_needs_a_login(self):
        self.client.logout()
        self.assertEqual(self.client.get("/account/id-documents/").status_code, 302)

    def test_csrf_is_still_checked_on_upload(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.member)
        response = client.post("/account/id-documents/", {"photo_id": upload("passport.jpg", JPEG)})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(IdDocument.objects.exists())

    def test_the_account_page_shows_the_step(self):
        self.assertContains(self.client.get("/account/"), "Send your proof of identity")


class TrusteeCheckTests(TestCase):
    def setUp(self):
        self.member = person("kim@example.com")
        self.trustee = person("trustee@example.com")
        self.trustee.user_permissions.add(Permission.objects.get(codename="review_id_documents"))
        id_documents.save_document(self.member, "photo_id", upload("passport.jpg", JPEG), JPEG, "image/jpeg")
        id_documents.save_document(self.member, "proof_of_address", upload("bill.heic", HEIC), HEIC, "image/heic")
        self.client.force_login(self.trustee)

    def test_the_list_shows_members_waiting(self):
        response = self.client.get("/id-checks/")
        self.assertContains(response, "Kim Maker")
        self.assertContains(response, "2 of 2")

    def test_images_and_pdfs_open_in_the_browser_and_are_never_cached(self):
        document = IdDocument.objects.get(kind="photo_id")
        response = self.client.get(f"/id-checks/documents/{document.pk}")
        self.assertEqual(response.content, JPEG)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertTrue(response["Content-Disposition"].startswith("inline"))
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_heic_photos_download_instead(self):
        document = IdDocument.objects.get(kind="proof_of_address")
        response = self.client.get(f"/id-checks/documents/{document.pk}")
        self.assertTrue(response["Content-Disposition"].startswith("attachment"))

    def test_approving_records_the_check_and_deletes_the_documents(self):
        response = self.client.post(f"/id-checks/{self.member.pk}/", {"result": "approved", "note": "passport, bill"})
        self.assertRedirects(response, "/id-checks/")
        self.assertFalse(IdDocument.objects.exists())
        check = IdCheck.objects.get()
        self.assertEqual((check.result, check.checked_by, check.note), ("approved", self.trustee, "passport, bill"))
        self.assertEqual(check.documents_seen, "photo_id, proof_of_address")
        self.assertEqual(id_documents.status(self.member), "approved")

    def test_not_accepting_needs_a_reason_and_still_deletes_the_documents(self):
        response = self.client.post(f"/id-checks/{self.member.pk}/", {"result": "rejected", "note": ""})
        self.assertContains(response, "Say why")
        self.assertEqual(IdDocument.objects.count(), 2)
        self.client.post(f"/id-checks/{self.member.pk}/", {"result": "rejected", "note": "The bill is from 2019."})
        self.assertFalse(IdDocument.objects.exists())
        self.assertEqual(id_documents.status(self.member), "rejected")
        self.client.force_login(self.member)
        self.assertContains(self.client.get("/account/id-documents/"), "The bill is from 2019.")

    def test_sending_again_after_a_rejection_waits_for_a_new_check(self):
        id_documents.record_check(self.member, self.trustee, "rejected", "Blurry")
        id_documents.save_document(self.member, "photo_id", upload("p.jpg", JPEG), JPEG, "image/jpeg")
        self.assertEqual(id_documents.status(self.member), "waiting")

    def test_nothing_to_check_records_nothing(self):
        IdDocument.objects.all().delete()
        self.client.post(f"/id-checks/{self.member.pk}/", {"result": "approved"})
        self.assertFalse(IdCheck.objects.exists())

    def test_deleting_a_member_deletes_their_documents(self):
        self.member.delete()
        self.assertFalse(IdDocument.objects.exists())


class StaleShreddingTests(TestCase):
    def test_unchecked_uploads_are_deleted_after_the_time_limit(self):
        member = person("kim@example.com")
        old = id_documents.save_document(member, "photo_id", upload("a.jpg", JPEG), JPEG, "image/jpeg")
        IdDocument.objects.filter(pk=old.pk).update(uploaded_at=timezone.now() - timedelta(days=31))
        id_documents.save_document(member, "proof_of_address", upload("b.pdf", PDF), PDF, "application/pdf")
        out = StringIO()
        call_command("shred_stale_id_documents", stdout=out)
        self.assertEqual(list(IdDocument.objects.values_list("kind", flat=True)), ["proof_of_address"])
        self.assertIn("Deleted 1", out.getvalue())
