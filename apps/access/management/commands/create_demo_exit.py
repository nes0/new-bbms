"""
Set up the fake exit node page (/dev/fake-exit/). Local development only.

Creates (or refreshes) an exit reader and a front-door reader, each with a new API key, and seven made-up members with
fobs and generated avatar photos. Some are checked in already (one is using a demo machine), so the screen has
something to show. Among them: one with a private profile (shown as "a member") and one whose photo is for members
only (shown by name, without a photo). Safe to run again.
"""

import io
from datetime import timedelta

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFont

from apps.access import tags
from apps.access.models import AccessEvent, Device, KeyFob
from apps.equipment.models import Equipment, UsageSession
from apps.members.models import Member, MemberPhoto
from apps.profiles import photos

FOB_PREFIX = "70"
# (email name, given, family, card, avatar colour, checked in this many minutes ago (None: out), privacy)
PEOPLE = [
    ("kim", "Kim", "Kay", 1000011, (64, 120, 200), 150, ""),
    ("sam", "Sam", "Stone", 1000012, (200, 90, 60), 45, ""),
    ("ari", "Ari", "Ash", 1000013, (60, 150, 100), 20, "using"),
    ("pat", "Pat", "Private", 1000014, (140, 90, 170), 10, "private profile"),
    ("mel", "Mel", "Members-only", 1000015, (180, 150, 40), 5, "members-only photo"),
    ("jo", "Jo", "Jones", 1000016, (40, 140, 150), None, ""),
    ("no", "Noor", "No-photo", 1000017, None, None, ""),
]


def avatar(initials: str, colour) -> ContentFile:
    """A made-up "photo": the member's initials on a coloured square."""
    image = Image.new("RGB", (500, 500), colour)
    draw = ImageDraw.Draw(image)
    draw.ellipse((150, 70, 350, 270), fill=tuple(min(255, c + 60) for c in colour))  # a head
    draw.ellipse((70, 300, 430, 660), fill=tuple(min(255, c + 60) for c in colour))  # shoulders
    font = ImageFont.load_default(size=110)
    draw.text((250, 175), initials, fill=(255, 255, 255), font=font, anchor="mm")
    out = io.BytesIO()
    image.save(out, "PNG")
    return ContentFile(out.getvalue(), name="avatar.png")


class Command(BaseCommand):
    help = "Create an exit reader, a front-door reader and demo members for the fake exit node page. DEBUG only."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("This is for local development only: run it with DEBUG=True.")
        now = timezone.now()
        keys = {}
        for device_id, name, role in (
            ("demo-exit", "Demo exit node", Device.Role.EXIT),
            ("demo-door", "Demo front door", Device.Role.ENTRY),
        ):
            device = Device.objects.filter(device_id=device_id).first() or Device(device_id=device_id)
            device.name, device.role, device.enabled = name, role, True
            keys[device_id] = device.generate_api_key()
            device.save()

        lathe, _ = Equipment.objects.update_or_create(
            slug="demo-lathe", defaults=dict(name="Demo lathe", requires_induction=False, archived=False)
        )
        rows = []
        for email_name, given, family, card, colour, minutes_ago, note in PEOPLE:
            email = f"demo-{email_name}@example.com"
            member = Member.objects.filter(email=email).first() or Member.objects.create_user(email, None)
            member.given_name, member.family_name = given, family
            member.status = Member.Status.ACTIVE
            member.email_verified_at = member.email_verified_at or now
            member.profile_private = note == "private profile"
            # Key holders, so the front door opens for them (05 §5.2).
            member.trusted = member.key_holder = member.induction_completed = True
            member.inducted_at = member.inducted_at or now
            member.save()
            member.profile.photo_private = note == "members-only photo"
            member.profile.save()
            MemberPhoto.objects.filter(member=member).delete()
            member.photo_approved = False
            if colour:
                full, thumb = photos.process(avatar(given[0] + family[0], colour))
                photos._store(member, MemberPhoto.Kind.CURRENT, full, thumb)
                member.photo_approved = True
            member.save(update_fields=["photo_approved"])
            KeyFob.objects.update_or_create(
                tag_id=tags.full_tag(FOB_PREFIX, card), defaults=dict(member=member, active=True, lost_at=None)
            )

            # Start from a clean slate today: out, unless the table says they came in a while ago.
            AccessEvent.objects.filter(member=member, occurred_at__gte=now - timedelta(days=1)).delete()
            UsageSession.objects.filter(member=member, equipment=lathe, ended_at__isnull=True).delete()
            if minutes_ago is not None:
                AccessEvent.objects.create(
                    member=member, kind=AccessEvent.Kind.CHECK_IN, occurred_at=now - timedelta(minutes=minutes_ago)
                )
            if note == "using":
                UsageSession.objects.create(
                    equipment=lathe, member=member, started_at=now - timedelta(minutes=12), last_heartbeat_at=now
                )
            state = "in" if minutes_ago is not None else "out"
            rows.append((tags.format_card_number(card), f"{given} {family}", state, note))

        self.stdout.write(f"Exit node API key (shown once):   {keys['demo-exit']}")
        self.stdout.write(f"Front door API key (optional):    {keys['demo-door']}")
        self.stdout.write("Fobs:")
        for card, name, state, note in rows:
            self.stdout.write(f"  {card}  {name:<20} {state:<4} {note}")
        self.stdout.write("Open http://localhost:8000/dev/fake-exit/ and paste the keys.")
