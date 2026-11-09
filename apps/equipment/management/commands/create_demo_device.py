"""
Set up a demo machine for the fake device page (/dev/fake-device/). Local development only.

Creates (or refreshes) the "Demo laser", its fob reader with a new API key, and four demo members with fobs:
trained, untrained, on the machine's team, and a former member. Safe to run again: it gives the reader a new key.
"""

from decimal import Decimal

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.access import tags
from apps.access.models import Device, KeyFob
from apps.core import fakes
from apps.equipment.models import Equipment, Induction
from apps.members.models import Member

SLUG = "demo-laser"
FOB_PREFIX = "70"
# (email name, given name, family name, status, trained, on team, card number)
PEOPLE = [
    ("trained", "Tara", "Trained", Member.Status.ACTIVE, True, False, 1000001),
    ("untrained", "Una", "Untrained", Member.Status.ACTIVE, False, False, 1000002),
    ("team", "Tom", "Team", Member.Status.ACTIVE, True, True, 1000003),
    ("left", "Lee", "Left", Member.Status.LEFT, True, False, 1000004),
]


class Command(BaseCommand):
    help = "Create a demo machine, its reader and members with fobs, for the fake device page. DEBUG only."

    def handle(self, *args, **options):
        if not fakes.allowed():
            raise CommandError("This is for local development only: run it with DEBUG=True.")

        team, _ = Group.objects.get_or_create(name=SLUG)
        equipment, _ = Equipment.objects.update_or_create(
            slug=SLUG,
            defaults=dict(
                name="Demo laser",
                team=team,
                requires_induction=True,
                induction_category=SLUG,
                usage_cost_pence_per_hour=300,
                meter_mode=Equipment.MeterMode.CURRENT,
                current_threshold_amps=Decimal("2.50"),
                archived=False,
            ),
        )
        device = Device.objects.filter(device_id=SLUG).first() or Device(device_id=SLUG)
        device.name = "Demo laser reader"
        device.role = Device.Role.EQUIPMENT
        key = device.generate_api_key()
        device.save()

        rows = []
        for email_name, given, family, status, is_trained, on_team, card in PEOPLE:
            email = f"demo-{email_name}@example.com"
            member = Member.objects.filter(email=email).first() or Member.objects.create_user(email, None)
            member.given_name, member.family_name, member.status = given, family, status
            member.save()
            KeyFob.objects.update_or_create(
                tag_id=tags.full_tag(FOB_PREFIX, card), defaults=dict(member=member, active=True, lost_at=None)
            )
            if is_trained:
                Induction.objects.update_or_create(
                    member=member, category=equipment.induction_key, defaults=dict(paid=True, trained_at=timezone.now())
                )
            else:
                Induction.objects.filter(member=member, category=equipment.induction_key).delete()
            if on_team:
                member.groups.add(team)
            rows.append((tags.format_card_number(card), tags.full_tag(FOB_PREFIX, card), f"{given} {family}"))

        self.stdout.write(f"Machine: {equipment.name} ({equipment.slug}), £3.00/hour, current-metered above 2.5 A")
        self.stdout.write(f"Reader API key (shown once): {key}")
        self.stdout.write("Fobs (type either the card number or the tag):")
        for card, tag, name in rows:
            self.stdout.write(f"  {card}  {tag}  {name}")
        self.stdout.write("Open http://localhost:8000/dev/fake-device/ and paste the key.")
