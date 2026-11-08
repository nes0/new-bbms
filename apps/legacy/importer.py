"""
Copy the legacy system's data into the new models (spec 08 §8.2).

Run through `manage.py import_legacy`, always into an **empty** database: the script is meant to be run again and
again against fresh copies of production before the real cutover, so it never tries to merge into existing data.

What is imported so far (the apps that exist): members (with how they pay, what they've paid up to and who confirmed
their general induction), their addresses, photos (with --photos-from), audit log and roles, subscription charges,
the payments ledger (linked to charges, sessions and inductions, with credit checked against legacy), key fobs,
devices (keeping their API keys, so today's door and laser keep working), equipment, inductions, machine usage
sessions and the door log.
Everything else is counted and listed as "not imported yet".

The report only ever shows legacy **ids** and counts, never names or email addresses, so it's safe to share.
"""

import json
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group, Permission
from django.core.files.base import ContentFile
from django.utils import timezone
from django.utils.text import slugify

from apps.access import tags
from apps.access.models import AccessEvent, Device, KeyFob
from apps.billing.models import Charge
from apps.equipment import rules
from apps.equipment.models import Equipment, Induction, UsageSession
from apps.members.hashers import from_legacy_hash
from apps.members.models import Address, Member, MemberChange, MemberPhoto
from apps.payments.credit import credit_by_member
from apps.payments.models import Payment

BATCH = 2000

# Legacy statuses that don't exist any more (03). ❓ Provisional: confirm with the trustees.
STATUS_MAP = {
    "setting-up": Member.Status.SETTING_UP,
    "active": Member.Status.ACTIVE,
    "suspended": Member.Status.SUSPENDED,
    "leaving": Member.Status.LEAVING,
    "left": Member.Status.LEFT,
    "honorary": Member.Status.HONORARY,
    "payment-warning": Member.Status.ACTIVE,  # legacy only, and it still let members in (05 §5.2)
    "on-hold": Member.Status.SUSPENDED,  # display only in legacy, with no access
}

# The "Member Box" equipment item becomes a "box paid" flag instead (08 §8.2, decided 2026-11-07).
BOX_INDUCTION_KEY = "box"

# How members pay (02 §2.2). Old-style GoCardless subscriptions are dropped: those members must be moved to a
# Direct Debit before cutover (08), so they're imported with no method and listed.
PAYMENT_METHOD_MAP = {
    "": Member.PaymentMethod.NONE,
    "gocardless-variable": Member.PaymentMethod.DIRECT_DEBIT,
    "balance": Member.PaymentMethod.CREDIT,
    "standing-order": Member.PaymentMethod.STANDING_ORDER,
    "paypal": Member.PaymentMethod.PAYPAL,
    "cash": Member.PaymentMethod.CASH,
}

CHARGE_STATUSES = {status.value for status in Charge.Status}

SESSION_REASONS = {
    "": UsageSession.Reason.NORMAL,
    "training": UsageSession.Reason.TRAINING,
    "testing": UsageSession.Reason.TESTING,
}

NOT_IMPORTED_YET = [
    ("proposals", "governance app"),
    ("proposal_votes", "governance app"),
    ("expenses", "governance app"),
    ("notifications", "notifications app"),
]
NEVER_IMPORTED = ["storage_boxes", "detected_devices", "settings", "failed_jobs", "password_reminders", "migrations"]


# Payments (02 §2.4, 04 §4.1). Any other legacy reason is a consumable service (e.g. a printer), kept in the reference.
PAYMENT_REASONS = {
    "subscription": Payment.Reason.SUBSCRIPTION,
    "induction": Payment.Reason.INDUCTION,
    "door-key": Payment.Reason.KEY_DEPOSIT,
    "key-deposit": Payment.Reason.KEY_DEPOSIT,
    "storage-box": Payment.Reason.STORAGE_BOX,
    "balance": Payment.Reason.BALANCE,
    "equipment-fee": Payment.Reason.EQUIPMENT_FEE,
    "withdrawal": Payment.Reason.WITHDRAWAL,
    "donation": Payment.Reason.DONATION,
    "costs": Payment.Reason.COSTS,
    "transfer": Payment.Reason.TRANSFER,
}
PAYMENT_SOURCES = {source.value: source for source in Payment.Source}
# Legacy stored GoCardless's own statuses as they came. Credit counts paid, pending and withdrawn (04 §4.7).
PAYMENT_STATUSES = {
    "paid": Payment.Status.PAID,
    "confirmed": Payment.Status.PAID,
    "paid_out": Payment.Status.PAID,
    "pending": Payment.Status.PENDING,
    "pending_submission": Payment.Status.PENDING,
    "pending_customer_approval": Payment.Status.PENDING,
    "submitted": Payment.Status.PENDING,
    "withdrawn": Payment.Status.WITHDRAWN,
    "failed": Payment.Status.FAILED,
    "charged_back": Payment.Status.FAILED,
    "customer_approval_denied": Payment.Status.FAILED,
    "late_failure_settled": Payment.Status.FAILED,
    "cancelled": Payment.Status.CANCELLED,
    "refunded": Payment.Status.CANCELLED,
}


# ---- Turning legacy values into Python values -------------------------------------------------------------------


def when(value):
    """A legacy timestamp (stored in UTC) as an aware datetime, or None for empty and zero dates."""
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None  # '0000-00-00 00:00:00'
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


def day(value):
    moment = when(value)
    return moment.date() if moment else None


def text(value, limit=None):
    result = ("" if value is None else str(value)).strip()
    return result[:limit] if limit else result


def flag(value):
    return bool(value) and value not in ("0", 0)


def pence(pounds):
    """Legacy money (pounds, as a MySQL double) in pence, rounding half up (08 §8.2)."""
    return int((Decimal(str(pounds or 0)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


# ---- The report ---------------------------------------------------------------------------------------------------


class Report:
    SAMPLE_IDS = 10

    def __init__(self):
        self.read = Counter()
        self.imported = Counter()
        self.skipped = defaultdict(lambda: defaultdict(list))  # table -> reason -> [legacy ids]
        self.notes = []

    def skip(self, table, reason, legacy_id):
        self.skipped[table][reason].append(legacy_id)

    def note(self, message):
        self.notes.append(message)

    def lines(self):
        yield "Imported:"
        for table in self.read:
            skipped = sum(len(ids) for ids in self.skipped[table].values())
            yield f"  {table}: {self.read[table]} read, {self.imported[table]} imported, {skipped} skipped"
            for reason, ids in self.skipped[table].items():
                sample = ", ".join(str(i) for i in ids[: self.SAMPLE_IDS])
                more = f" and {len(ids) - self.SAMPLE_IDS} more" if len(ids) > self.SAMPLE_IDS else ""
                yield f"    - {reason}: {len(ids)} (legacy ids {sample}{more})"
        if self.notes:
            yield "Notes:"
            for message in self.notes:
                yield f"  - {message}"


# ---- The importer -------------------------------------------------------------------------------------------------


class Importer:
    """`mark_inducted`: treat every imported member who has been a member (not still setting up) as having had their
    general induction confirmed, as the trustees may decide for cutover (05 §5.2). `photos`: a photos.PhotoSource
    to copy members' photos from; without it, photos aren't copied.
    """

    def __init__(self, source, now=None, *, mark_inducted=False, photos=None):
        self.source = source
        self.mark_inducted = mark_inducted
        self.photos = photos
        self.report = Report()
        self.now = now or timezone.now()
        self.members = {}  # legacy user id -> Member
        self.groups = {}  # legacy role id -> Group
        self.fobs = {}  # legacy key fob id -> KeyFob
        self.devices = {}  # device id (e.g. "main-door") -> Device
        self.equipment = {}  # slug -> Equipment
        self.box_paid_user_ids = []  # legacy user ids who'll get the "box paid" flag
        self.inducted_by = {}  # legacy user id -> legacy id of who confirmed their general induction
        self.legacy_credit = {}  # legacy user id -> their cached credit in legacy (users.cash_balance, pence)
        self.user_hashes = {}  # legacy user id -> users.hash (names their photos)
        self.induction_payments = {}  # legacy induction id -> legacy payment id

    def run(self):
        # List every table in the report, even empty ones, in the order they're imported.
        for table in (
            "users",
            "audit_log",
            "user_address",
            "subscription_charge",
            "profile_data",
            "roles",
            "role_user",
            "key_fobs",
            "equipment",
            "acs_nodes",
            "inductions",
            "equipment_log",
            "payments",
            "access_log",
        ):
            self.report.read[table] += 0
        self.import_members()
        self.import_audit_log()
        self.import_addresses()
        self.import_charges()
        self.import_profiles()
        self.import_roles()
        self.import_fobs()
        self.import_equipment()
        self.import_devices()
        self.import_inductions()
        self.import_sessions()
        self.import_payments()
        self.import_access_log()
        self.list_not_imported()
        return self.report

    # -- Members ----------------------------------------------------------------------------------------------------

    def import_members(self):
        table, report = "users", self.report
        unusable_password = make_password(None)
        seen_emails = {}
        statuses = Counter()
        dropped_methods = defaultdict(list)
        batch = []
        for row in self.source.rows(table):
            report.read[table] += 1
            email = text(row.get("email")).lower()
            if not email:
                report.skip(table, "no email address", row["id"])
                continue
            if email in seen_emails:
                report.skip(table, f"same email as legacy user {seen_emails[email]}", row["id"])
                continue
            seen_emails[email] = row["id"]
            legacy_status = text(row.get("status"))
            status = STATUS_MAP.get(legacy_status)
            if status is None:
                status = Member.Status.SUSPENDED
                report.note(f"user {row['id']}: unknown status {legacy_status!r}, imported as suspended")
            statuses[legacy_status] += 1
            password = text(row.get("password"))
            legacy_method = text(row.get("payment_method"))
            method = PAYMENT_METHOD_MAP.get(legacy_method)
            if method is None:
                dropped_methods[legacy_method].append(row["id"])
                method = Member.PaymentMethod.NONE
            payment_day = int(row.get("payment_day") or 0) or None
            if payment_day is not None and payment_day > 28:
                payment_day = 1  # as legacy did (02 §2.2)
            pounds = int(row.get("monthly_subscription") or 0)
            if row.get("inducted_by"):
                self.inducted_by[row["id"]] = row["inducted_by"]
            self.legacy_credit[row["id"]] = int(row.get("cash_balance") or 0)
            if text(row.get("hash")):
                self.user_hashes[row["id"]] = text(row.get("hash"))
            paypal_email = text(row.get("secondary_email"), 254).lower()  # legacy matched PayPal payments by it
            if paypal_email == email:
                paypal_email = ""
            batch.append(
                Member(
                    email=email,
                    password=from_legacy_hash(password) if password.startswith("$2") else unusable_password,
                    given_name=text(row.get("given_name"), 100),
                    family_name=text(row.get("family_name"), 100),
                    phone=text(row.get("phone"), 30),
                    emergency_contact=text(row.get("emergency_contact"), 255),
                    status=status,
                    trusted=flag(row.get("trusted")),
                    key_holder=flag(row.get("key_holder")),
                    founder=flag(row.get("founder")),
                    induction_completed=flag(row.get("induction_completed")),
                    rules_agreed_at=when(row.get("rules_agreed")),
                    profile_private=flag(row.get("profile_private")),
                    banned=flag(row.get("banned")),
                    banned_at=when(row.get("banned_date")),
                    banned_reason=text(row.get("banned_reason")),
                    notes=text(row.get("notes")),
                    payment_method=method,
                    payment_day=payment_day,
                    monthly_subscription_pence=pounds * 100 if pounds > 0 else None,
                    mandate_id=text(row.get("mandate_id"), 50),
                    paypal_email=paypal_email,
                    subscription_expires=day(row.get("subscription_expires")),
                    legacy_id=row["id"],
                    date_joined=when(row.get("created_at")) or self.now,
                )
            )
        Member.objects.bulk_create(batch, batch_size=500)
        report.imported[table] = len(batch)
        self.members = {m.legacy_id: m for m in Member.objects.filter(legacy_id__isnull=False)}
        self.link_inductions_confirmed()
        report.note("legacy statuses: " + ", ".join(f"{s or '(blank)'} {n}" for s, n in sorted(statuses.items())))
        for old in ("payment-warning", "on-hold"):
            if statuses[old]:
                report.note(f"{statuses[old]} '{old}' members imported as {STATUS_MAP[old].label.lower()} (❓)")
        for old, ids in sorted(dropped_methods.items()):
            sample = ", ".join(str(i) for i in ids[:10])
            report.note(
                f"{len(ids)} member(s) paying by '{old}', which isn't supported any more, imported with no payment "
                f"method: move them to Direct Debit before cutover (legacy ids {sample})"
            )

    def import_charges(self):
        """Subscription charges, as they are (08 §8.2). The payments they link to come with the payments import."""
        table, report = "subscription_charge", self.report
        batch, seen = [], set()
        for row in self.source.rows(table):
            report.read[table] += 1
            member = self.members.get(row.get("user_id"))
            if member is None:
                report.skip(table, "no such member", row["id"])
                continue
            charge_date = day(row.get("charge_date"))
            if charge_date is None:
                report.skip(table, "no charge date", row["id"])
                continue
            if (member.pk, charge_date) in seen:
                report.skip(table, "a second charge for the same member and date", row["id"])
                continue
            seen.add((member.pk, charge_date))
            status = text(row.get("status"))
            if status not in CHARGE_STATUSES:
                report.skip(table, f"unknown status {status!r}", row["id"])
                continue
            amount = int(row.get("amount") or 0)
            batch.append(
                Charge(
                    member=member,
                    kind=Charge.Kind.SUBSCRIPTION,
                    charge_date=charge_date,
                    amount_pence=amount if amount > 0 else None,
                    status=status,
                    paid_on=day(row.get("payment_date")) if status == Charge.Status.PAID else None,
                    created_at=when(row.get("created_at")) or self.now,
                    legacy_id=row["id"],
                )
            )
        Charge.objects.bulk_create(batch, batch_size=BATCH)
        report.imported[table] = len(batch)

    def import_profiles(self):
        table, report = "profile_data", self.report
        changed, pending_photos, pending_ids = [], 0, []
        for row in self.source.rows(table):
            report.read[table] += 1
            member = self.members.get(row.get("user_id"))
            if member is None:
                report.skip(table, "no such member", row["id"])
                continue
            member.photo_approved = flag(row.get("profile_photo"))
            member.photo_on_wall = flag(row.get("profile_photo_on_wall"))
            if flag(row.get("new_profile_photo")):
                pending_photos += 1
                pending_ids.append(member.legacy_id)
            changed.append(member)
        Member.objects.bulk_update(changed, ["photo_approved", "photo_on_wall"], batch_size=500)
        report.imported[table] = len(changed)
        report.note(f"social links and descriptions aren't imported yet; {pending_photos} new photos awaited approval")
        self.import_photos(changed, pending=set(pending_ids))

    def import_photos(self, members, pending):
        """Copy members' photos from legacy's S3 bucket to private storage (08 §8.2), if a photo source was given."""
        wanted = [(m, MemberPhoto.Kind.CURRENT) for m in members if m.photo_approved]
        wanted += [(m, MemberPhoto.Kind.PENDING) for m in members if m.legacy_id in pending]
        if self.photos is None:
            if wanted:
                self.report.note(f"photos not copied ({len(wanted)} to copy): run with --photos-from to copy them")
            return
        copied, missing = Counter(), []
        for member, kind in wanted:
            user_hash = self.user_hashes.get(member.legacy_id)
            found = self.photos.fetch(user_hash, kind) if user_hash else None
            if found is None:
                missing.append(member.legacy_id)
                continue
            copied[kind] += 1
            if self.photos.save:
                photo = MemberPhoto(member=member, kind=kind, uploaded_at=self.now)
                photo.image.save("photo.png", ContentFile(found[0]), save=False)
                photo.thumbnail.save("thumb.png", ContentFile(found[1]), save=False)
                photo.save()
        verb = "copied" if self.photos.save else "found (dry run: not copied)"
        self.report.note(
            f"photos {verb}: {copied[MemberPhoto.Kind.CURRENT]} approved, {copied[MemberPhoto.Kind.PENDING]} waiting "
            "for approval"
        )
        if missing:
            sample = ", ".join(str(i) for i in sorted(missing)[:10])
            self.report.note(f"{len(missing)} photo(s) couldn't be fetched (legacy user ids {sample})")

    def link_inductions_confirmed(self):
        """Who confirmed each member's general induction (05 §5.9). Legacy didn't record when."""
        unknown = []
        for legacy_id, confirmer_id in self.inducted_by.items():
            member, confirmer = self.members.get(legacy_id), self.members.get(confirmer_id)
            if member is None:
                continue
            if confirmer is None:
                unknown.append(legacy_id)
                continue
            Member.objects.filter(pk=member.pk).update(inducted_by=confirmer)
        if unknown:
            self.report.note(
                f"{len(unknown)} general induction(s) confirmed by someone not imported: "
                "listed as waiting to be confirmed (legacy ids " + ", ".join(str(i) for i in unknown[:10]) + ")"
            )
        if self.mark_inducted:
            self.mark_members_inducted()
        # The door now needs a confirmed general induction (05 §5.2), which legacy didn't check.
        unconfirmed = (
            Member.objects.filter(key_holder=True, inducted_by__isnull=True, inducted_at__isnull=True)
            .exclude(status=Member.Status.LEFT)
            .order_by("legacy_id")
            .values_list("legacy_id", flat=True)
        )
        if unconfirmed:
            sample = ", ".join(str(i) for i in unconfirmed[:10])
            self.report.note(
                f"⚠️ {len(unconfirmed)} key holder(s) without a confirmed general induction: the door won't open for "
                f"them until it's confirmed (legacy ids {sample})"
            )

    def mark_members_inducted(self):
        """--mark-inducted: everyone who has been a member counts as inducted, confirmed at the import."""
        members = Member.objects.filter(
            legacy_id__isnull=False, inducted_by__isnull=True, inducted_at__isnull=True
        ).exclude(status=Member.Status.SETTING_UP)
        ids = list(members.values_list("pk", flat=True))
        Member.objects.filter(pk__in=ids).update(induction_completed=True, inducted_at=self.now)
        MemberChange.objects.bulk_create(
            [
                MemberChange(
                    member_id=pk,
                    field="induction confirmed by",
                    new_value="the legacy import (--mark-inducted)",
                    changed_at=self.now,
                )
                for pk in ids
            ],
            batch_size=BATCH,
        )
        self.report.note(f"--mark-inducted: {len(ids)} member(s) given a confirmed general induction")

    def import_addresses(self):
        """Members' addresses (02 §2.3): the latest approved one, and a later change still waiting for approval."""
        table, report = "user_address", self.report
        by_member = defaultdict(list)
        for row in self.source.rows(table):
            report.read[table] += 1
            if row.get("user_id") not in self.members:
                report.skip(table, "no such member", row["id"])
                continue
            if not text(row.get("line_1")):
                report.skip(table, "no first line", row["id"])
                continue
            by_member[row["user_id"]].append(row)
        batch = []
        for user_id, rows in by_member.items():
            rows.sort(key=lambda r: (when(r.get("created_at")) or datetime.min.replace(tzinfo=UTC), r["id"]))
            approved = [r for r in rows if flag(r.get("approved"))]
            current = approved[-1] if approved else None
            for older in approved[:-1]:
                report.skip(table, "an older approved address (replaced)", older["id"])
            waiting = [r for r in rows if not flag(r.get("approved")) and (current is None or r["id"] > current["id"])]
            for older in waiting[:-1]:
                report.skip(table, "an older change waiting for approval (replaced)", older["id"])
            for r in rows:
                if not flag(r.get("approved")) and r not in waiting:
                    report.skip(table, "a change older than the approved address", r["id"])
            for row in [r for r in (current, waiting[-1] if waiting else None) if r]:
                batch.append(
                    Address(
                        member=self.members[user_id],
                        line_1=text(row.get("line_1"), 255),
                        line_2=text(row.get("line_2"), 255),
                        line_3=text(row.get("line_3"), 255),
                        line_4=text(row.get("line_4"), 255),
                        postcode=text(row.get("postcode"), 10).upper(),
                        approved=flag(row.get("approved")),
                        created_at=when(row.get("created_at")) or self.now,
                    )
                )
        Address.objects.bulk_create(batch, batch_size=BATCH)
        report.imported[table] = len(batch)
        waiting_count = sum(1 for a in batch if not a.approved)
        if waiting_count:
            report.note(f"{waiting_count} address change(s) waiting for approval: see Addresses in the admin")

    def import_audit_log(self):
        """The legacy audit log (02 §2.3): one row per admin change to a member's flags, as free text."""
        table, report = "audit_log", self.report
        batch = []
        for row in self.source.rows(table):
            report.read[table] += 1
            member = self.members.get(row.get("user_id"))
            if member is None:
                report.skip(table, "no such member", row["id"])
                continue
            action = text(row.get("action"))
            description = text(row.get("description"))
            batch.append(
                MemberChange(
                    member=member,
                    field="legacy",
                    new_value=(description if action in ("", "user_updated") else f"{action}: {description}")[:255],
                    changed_by=self.members.get(row.get("admin_id")),
                    changed_at=when(row.get("created_at")) or self.now,
                )
            )
        MemberChange.objects.bulk_create(batch, batch_size=500)
        report.imported[table] = len(batch)

    def import_roles(self):
        report = self.report
        for row in self.source.rows("roles"):
            report.read["roles"] += 1
            self.groups[row["id"]] = Group.objects.get_or_create(name=text(row.get("name"), 150))[0]
            report.imported["roles"] += 1

        admins, links = set(), set()
        for row in self.source.rows("role_user"):
            report.read["role_user"] += 1
            member, group = self.members.get(row.get("user_id")), self.groups.get(row.get("role_id"))
            if member is None or group is None:
                report.skip("role_user", "no such member or role", row["id"])
                continue
            links.add((member.pk, group.pk))
            if group.name == "admin":
                admins.add(member.pk)
        Through = Member.groups.through
        Through.objects.bulk_create([Through(member_id=m, group_id=g) for m, g in links], ignore_conflicts=True)
        Member.objects.filter(pk__in=admins).update(is_staff=True, is_superuser=True)
        # Legacy's comms role confirmed general inductions (01 §1.3, 05 §5.9).
        comms = next((g for g in self.groups.values() if g.name == "comms"), None)
        if comms is not None:
            confirm = Permission.objects.get(content_type__app_label="members", codename="confirm_general_inductions")
            comms.permissions.add(confirm)
        report.imported["role_user"] = len(links)
        report.note(f"{len(admins)} members of the 'admin' role can use the admin site (other roles: groups only)")

    # -- Fobs and devices --------------------------------------------------------------------------------------------

    def import_fobs(self):
        table, report = "key_fobs", self.report
        rows = list(self.source.rows(table))
        report.read[table] = len(rows)
        # Where two fobs clash, keep the active one, then the most recently changed.
        rows.sort(
            key=lambda r: (
                flag(r.get("active")) and not flag(r.get("lost")),
                when(r.get("updated_at")) or datetime.min.replace(tzinfo=UTC),
                r["id"],
            ),
            reverse=True,
        )
        formats = Counter()
        seen_tags, seen_cards = {}, {}
        batch = []
        for row in rows:
            member = self.members.get(row.get("user_id"))
            if member is None:
                report.skip(table, "no such member", row["id"])
                continue
            tag = tags.normalise(row.get("key_id"))
            if not tag:
                report.skip(table, "empty tag", row["id"])
                continue
            if len(tag) > 12:
                report.skip(table, "tag longer than 12 characters", row["id"])
                continue
            card = tags.card_number(tag)
            if tags.is_valid_full_tag(tag):
                formats["12 hex digits, valid checksum"] += 1
            elif tag.startswith("BB"):
                formats["'BB' + 10 (very old style)"] += 1
            else:
                formats[f"{len(tag)} characters, other"] += 1
            if tag in seen_tags:
                report.skip(table, "same tag as another fob", row["id"])
                continue
            if card is not None and card in seen_cards:
                report.skip(table, "same card number as another fob", row["id"])
                continue
            seen_tags[tag] = row["id"]
            if card is not None:
                seen_cards[card] = row["id"]
            lost = flag(row.get("lost"))
            batch.append(
                KeyFob(
                    member=member,
                    tag_id=tag,
                    card_number=card,
                    active=flag(row.get("active")) and not lost,
                    issued_at=when(row.get("created_at")) or self.now,
                    lost_at=(when(row.get("updated_at")) or self.now) if lost else None,
                    legacy_id=row["id"],
                )
            )
        KeyFob.objects.bulk_create(batch, batch_size=BATCH)
        report.imported[table] = len(batch)
        self.fobs = {f.legacy_id: f for f in KeyFob.objects.filter(legacy_id__isnull=False)}
        report.note("fob tag formats: " + ", ".join(f"{name} {n}" for name, n in formats.most_common()))

    def import_devices(self):
        table, report = "acs_nodes", self.report
        for row in self.source.rows(table):
            report.read[table] += 1
            device_id = slugify(text(row.get("device_id")))[:64]
            if not device_id or device_id in self.devices:
                report.skip(table, "missing or repeated device id", row["id"])
                continue
            if flag(row.get("entry_device")):
                role = Device.Role.ENTRY
            elif device_id == "main-door":
                role = Device.Role.ENTRY
                report.note(f"device {row['id']} (main-door) isn't marked as an entry device; imported as the door")
            else:
                role = Device.Role.EQUIPMENT
                if device_id not in self.equipment:
                    report.note(f"equipment device {row['id']} ({device_id}) matches no equipment slug")
            device = Device(
                name=text(row.get("name"), 100) or device_id,
                device_id=device_id,
                role=role,
                monitor_heartbeat=flag(row.get("monitor_heartbeat")),
                last_boot=when(row.get("last_boot")),
                last_heartbeat=when(row.get("last_heartbeat")),
                legacy_id=row["id"],
            )
            api_key = text(row.get("api_key"))
            if api_key:
                device.set_api_key(api_key)  # the same key, so the device needs no change (08 §8.2)
            else:
                device.generate_api_key()
                report.note(f"device {row['id']} ({device_id}) had no API key; it was given a new one")
            device.save()
            self.devices[device_id] = device
            report.imported[table] += 1

    # -- Equipment, inductions and sessions -------------------------------------------------------------------------

    def import_equipment(self):
        table, report = "equipment", self.report
        for row in self.source.rows(table):
            report.read[table] += 1
            category = text(row.get("induction_category"), 64)
            if category.lower() == BOX_INDUCTION_KEY:
                report.skip(table, "the Member Box item (becomes the box-paid flag)", row["id"])
                continue
            slug = slugify(text(row.get("slug")) or text(row.get("name")))[:60] or f"equipment-{row['id']}"
            base, n = slug, 2
            while slug in self.equipment:
                slug, n = f"{base}-{n}", n + 1
            per = text(row.get("usage_cost_per")).lower()
            rate = int(row.get("usage_cost") or 0)
            if rate and per not in ("", "hour"):
                report.note(f"equipment {row['id']} charges per {per!r}, which isn't supported; usage cost set to 0")
                rate = 0
            device_key = text(row.get("device_key"))
            if device_key and device_key != slug:
                report.note(f"equipment {row['id']}: device key {device_key!r} differs from its slug {slug!r}")
            permaloan = self.members.get(row.get("permaloan_user_id")) if flag(row.get("permaloan")) else None
            self.equipment[slug] = Equipment.objects.create(
                name=text(row.get("name"), 100) or slug,
                slug=slug,
                team=self.groups.get(row.get("managing_role_id")),
                manufacturer=text(row.get("manufacturer"), 100),
                model_number=text(row.get("model_number"), 100),
                serial_number=text(row.get("serial_number"), 100),
                asset_tag_id=text(row.get("asset_tag_id"), 50),
                room=text(row.get("room"), 100),
                description=text(row.get("description")),
                help_text=text(row.get("help_text")),
                ppe=self._ppe(row.get("ppe")),
                requires_induction=flag(row.get("requires_induction")),
                induction_category=category,
                induction_fee_pence=int(row.get("access_fee") or 0) * 100,  # legacy: whole pounds
                usage_cost_pence_per_hour=rate,  # legacy: pence
                status=Equipment.Status.WORKING if flag(row.get("working")) else Equipment.Status.OUT_OF_ORDER,
                status_since=when(row.get("updated_at")) or self.now,
                permaloan_member=permaloan,
                archived=flag(row.get("archive")),
                obtained_at=day(row.get("obtained_at")),
                removed_at=day(row.get("removed_at")),
                legacy_id=row["id"],
            )
            report.imported[table] += 1
        report.note("equipment photos aren't copied yet (S3 → Spaces)")

    @staticmethod
    def _ppe(value):
        value = text(value)
        if value.startswith("["):
            try:
                return ", ".join(str(item) for item in json.loads(value))[:255]
            except ValueError:
                pass
        return value[:255]

    def import_inductions(self):
        table, report = "inductions", self.report
        best = {}  # (user id, key) -> row
        box_rows = []
        for row in self.source.rows(table):
            report.read[table] += 1
            key = text(row.get("key"), 64)
            if key.lower() == BOX_INDUCTION_KEY:
                box_rows.append(row)
                report.skip(table, "Member Box (becomes the box-paid flag)", row["id"])
                continue
            if row.get("user_id") not in self.members:
                report.skip(table, "no such member", row["id"])
                continue
            pair = (row["user_id"], key)
            if pair in best:
                keep, drop = sorted([best[pair], row], key=self._induction_rank, reverse=True)
                report.skip(table, "a second induction for the same member and machine", drop["id"])
                best[pair] = keep
            else:
                best[pair] = row

        known = {e.induction_key for e in self.equipment.values()}
        unknown = Counter(key for _, key in best if key not in known)
        self.induction_payments = {row["id"]: row["payment_id"] for row in best.values() if row.get("payment_id")}
        batch = [
            Induction(
                member=self.members[row["user_id"]],
                category=key,
                paid=flag(row.get("paid")),
                trained_at=when(row.get("trained")),
                trainer=self.members.get(row.get("trainer_user_id")),
                is_trainer=flag(row.get("is_trainer")),
                created_at=when(row.get("created_at")) or self.now,
                legacy_id=row["id"],
            )
            for (_, key), row in best.items()
        ]
        Induction.objects.bulk_create(batch, batch_size=BATCH)
        report.imported[table] = len(batch)
        if unknown:
            report.note(
                "inductions for categories no equipment uses: " + ", ".join(f"{k} {n}" for k, n in unknown.items())
            )
        self._box_flags(box_rows)

    @staticmethod
    def _induction_rank(row):
        return (
            row.get("trained") is not None and when(row.get("trained")) is not None,
            flag(row.get("paid")),
            row["id"],
        )

    def _box_flags(self, box_rows):
        """Who gets the "box paid" flag: a paid Member Box induction whose payment really was paid (08 §8.2)."""
        paid_rows = [r for r in box_rows if flag(r.get("paid")) and r.get("user_id") in self.members]
        payment_ids = {r.get("payment_id") for r in paid_rows if r.get("payment_id")}
        payment_status = {}
        if payment_ids:
            for payment in self.source.rows("payments"):
                if payment["id"] in payment_ids:
                    payment_status[payment["id"]] = text(payment.get("status"))
        to_check = []
        for row in paid_rows:
            if payment_status.get(row.get("payment_id")) == "paid":
                self.box_paid_user_ids.append(row["user_id"])
            else:
                to_check.append(row["user_id"])
        # TODO(storage app): set the members' "box paid" flag (with the payment's date) instead of only reporting it.
        self.report.note(
            f"box paid: {len(self.box_paid_user_ids)} members qualify; {len(to_check)} have a paid induction but no "
            f"paid payment and need checking (legacy user ids {to_check[:20]}). Not stored until the storage app exists"
        )

    def import_sessions(self):
        table, report = "equipment_log", self.report
        unbilled, unbilled_pence, still_open, by_device = 0, 0, 0, Counter()
        batch = []
        for row in self.source.rows(table):
            report.read[table] += 1
            equipment = self.equipment.get(text(row.get("device")))
            member = self.members.get(row.get("user_id"))
            started = when(row.get("started"))
            if equipment is None:
                by_device[text(row.get("device"))] += 1
                report.skip(table, "no equipment with that device id", row["id"])
                continue
            if member is None or started is None:
                report.skip(table, "no such member, or no start time", row["id"])
                continue
            reason = SESSION_REASONS.get(text(row.get("reason")).lower())
            if reason is None:
                report.note(f"session {row['id']}: unknown reason {row.get('reason')!r}, imported as normal use")
                reason = UsageSession.Reason.NORMAL
            session = UsageSession(
                equipment=equipment,
                member=member,
                fob=self.fobs.get(row.get("key_fob_id")),
                started_at=started,
                last_heartbeat_at=when(row.get("last_update")),
                ended_at=when(row.get("finished")),
                reason=reason,
                removed=flag(row.get("removed")),
                legacy_id=row["id"],
            )
            if session.ended_at is None:
                still_open += 1
            else:
                session.ended_by = UsageSession.EndedBy.DEVICE
                session.fee_pence = rules.session_fee_pence(equipment, session)
                # The legacy system settled its own sessions; never charge them again here.
                if session.fee_pence:
                    session.charged_at = session.ended_at
                    if not flag(row.get("billed")):
                        unbilled += 1
                        unbilled_pence += session.fee_pence
            batch.append(session)
            if len(batch) >= BATCH:
                UsageSession.objects.bulk_create(batch)
                report.imported[table] += len(batch)
                batch = []
        UsageSession.objects.bulk_create(batch)
        report.imported[table] += len(batch)
        if by_device:
            report.note(
                "sessions on unknown devices: " + ", ".join(f"{d or '(blank)'} {n}" for d, n in by_device.items())
            )
        if still_open:
            report.note(f"{still_open} sessions were still open; the hourly tidy-up will close them")
        if unbilled:
            report.note(
                f"{unbilled} finished sessions the legacy system hadn't billed would cost £{unbilled_pence / 100:.2f} "
                "now; they're marked as settled so nobody is charged at cutover. Check against the legacy billing"
            )

    # -- Payments ----------------------------------------------------------------------------------------------------

    def import_payments(self):
        """The ledger (02 §2.4): amounts in pence, linked to the charges, machine sessions and inductions they paid for.
        Then each member's credit is checked against what legacy had cached (users.cash_balance).
        """
        table, report = "payments", self.report
        charges = dict(Charge.objects.filter(legacy_id__isnull=False).values_list("legacy_id", "pk"))
        sessions = dict(UsageSession.objects.filter(legacy_id__isnull=False).values_list("legacy_id", "pk"))
        consumables, unknown_sources, unknown_statuses = Counter(), Counter(), Counter()
        unmatched_members, negative_fees = 0, 0
        batch = []
        for row in self.source.rows(table):
            report.read[table] += 1
            legacy_reason = text(row.get("reason"))
            reference = text(row.get("reference"))
            reason = PAYMENT_REASONS.get(legacy_reason)
            if reason is None:
                consumables[legacy_reason or "(blank)"] += 1
                reason = Payment.Reason.CONSUMABLE
                reference = f"{legacy_reason}:{reference}" if reference else legacy_reason
            legacy_source = text(row.get("source"))
            source = PAYMENT_SOURCES.get(legacy_source)
            if source is None:
                unknown_sources[legacy_source or "(blank)"] += 1
                source = Payment.Source.MANUAL
            legacy_status = text(row.get("status"))
            status = PAYMENT_STATUSES.get(legacy_status)
            if status is None:
                unknown_statuses[legacy_status or "(blank)"] += 1
                status = Payment.Status.CANCELLED  # legacy didn't count it towards credit either
            user_id = row.get("user_id")
            member = self.members.get(user_id)
            if member is None and user_id:
                unmatched_members += 1
            fee = pence(row.get("fee"))
            if fee < 0:
                negative_fees += 1
                fee = 0
            payment = Payment(
                member=member,
                reason=reason,
                source=source,
                status=status,
                amount_pence=pence(row.get("amount")),
                fee_pence=fee,
                source_id=text(row.get("source_id"), 255),
                reference=reference[:255],
                created_at=when(row.get("created_at")) or self.now,
                paid_at=when(row.get("paid_at")),
                legacy_id=row["id"],
            )
            if reason == Payment.Reason.SUBSCRIPTION and reference.isdigit():
                payment.charge_id = charges.get(int(reference))
            if reason == Payment.Reason.EQUIPMENT_FEE and reference.partition(":")[0].isdigit():
                payment.usage_session_id = sessions.get(int(reference.partition(":")[0]))
            batch.append(payment)
            if len(batch) >= BATCH:
                Payment.objects.bulk_create(batch)
                report.imported[table] += len(batch)
                batch = []
        Payment.objects.bulk_create(batch)
        report.imported[table] += len(batch)

        self._link_induction_payments()
        if consumables:
            report.note(
                "payments for other services, imported as consumables: "
                + ", ".join(f"{r} {n}" for r, n in consumables.most_common())
            )
        if unknown_sources:
            report.note(
                "payments from unknown sources, imported as 'recorded by an admin': "
                + ", ".join(f"{s} {n}" for s, n in unknown_sources.most_common())
            )
        if unknown_statuses:
            report.note(
                "payments with unknown statuses, imported as cancelled (not counted towards credit): "
                + ", ".join(f"{s} {n}" for s, n in unknown_statuses.most_common())
            )
        if unmatched_members:
            report.note(f"{unmatched_members} payment(s) of members not imported kept with no member")
        if negative_fees:
            report.note(f"{negative_fees} payment(s) had a negative fee, imported as no fee")
        self._check_credit()

    def _link_induction_payments(self):
        payments = dict(Payment.objects.filter(legacy_id__isnull=False).values_list("legacy_id", "pk"))
        inductions = Induction.objects.filter(legacy_id__in=self.induction_payments)
        for induction in inductions:
            induction.payment_id = payments.get(self.induction_payments[induction.legacy_id])
        Induction.objects.bulk_update(inductions, ["payment"], batch_size=BATCH)

    def _check_credit(self):
        """Reconcile each member's credit with legacy's cached balance (08 §8.2)."""
        credit = credit_by_member()
        differences = []
        for legacy_id, member in self.members.items():
            legacy = self.legacy_credit.get(legacy_id, 0)
            if credit.get(member.pk, 0) != legacy:
                differences.append((legacy_id, credit.get(member.pk, 0) - legacy))
        if differences:
            sample = ", ".join(f"{i} ({d:+d}p)" for i, d in differences[:10])
            self.report.note(
                f"⚠️ credit differs from legacy's cached balance for {len(differences)} member(s), by "
                f"£{sum(d for _, d in differences) / 100:.2f} in all (legacy ids, new minus legacy: {sample})"
            )
        else:
            self.report.note("credit matches legacy's cached balance for every member")

    # -- The door log -----------------------------------------------------------------------------------------------

    def import_access_log(self):
        table, report = "access_log", self.report
        responses, services = Counter(), Counter()
        batch = []
        for row in self.source.rows(table):
            report.read[table] += 1
            response = text(row.get("response"))
            responses[response] += 1
            service = text(row.get("service"))
            device = self.devices.get(service)
            if device is None:
                services[service] += 1
            if response == "200":
                kind, reason = AccessEvent.Kind.ENTRY, ""
            elif response == "402":
                # "Not a keyholder": the member tapped but wasn't let in. Now a check-in, giving attendance (05 §5.2).
                kind, reason = AccessEvent.Kind.CHECK_IN, "not_keyholder"
            else:
                kind, reason = AccessEvent.Kind.REFUSED, f"legacy_{response or 'unknown'}"
            occurred = when(row.get("created_at")) or self.now
            batch.append(
                AccessEvent(
                    kind=kind,
                    member=self.members.get(row.get("user_id")),
                    fob=self.fobs.get(row.get("key_fob_id")),
                    device=device,
                    reason=reason,
                    occurred_at=occurred,
                    received_at=occurred,
                    delayed=flag(row.get("delayed")),
                )
            )
            if len(batch) >= BATCH:
                AccessEvent.objects.bulk_create(batch)
                report.imported[table] += len(batch)
                batch = []
        AccessEvent.objects.bulk_create(batch)
        report.imported[table] += len(batch)
        if responses:
            report.note("door log responses: " + ", ".join(f"{r or '(blank)'} {n}" for r, n in responses.most_common()))
        if services:
            report.note(
                "door log services with no device: " + ", ".join(f"{s or '(blank)'} {n}" for s, n in services.items())
            )

    def list_not_imported(self):
        waiting = [f"{table} ({self.source.count(table)} rows, {why})" for table, why in NOT_IMPORTED_YET]
        self.report.note("not imported yet: " + "; ".join(waiting))
        self.report.note("never imported (dropped or replaced): " + ", ".join(NEVER_IMPORTED))
