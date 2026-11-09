"""
Fill an empty database with a made-up makerspace for a demo site (DEMO_SITE=True), so people can try BBMS out:
about two dozen members (famous makers, scientists and engineers, plus one fictional troublemaker), all sharing one
password; the space's machines, each with a fob reader for the fake device page; the front door and the exit node;
three months of visits, machine use, payments and expenses; an open proposal; and a retired machine with a history.

Nobody here is a real member: every address is @example.com. Prints the devices' API keys once (only their hashes are
kept), and writes them to --keys-file if given, for the instructions.

    DEMO_SITE=True DEMO_PASSWORD='…' python manage.py seed_demo [--keys-file keys.json]
"""

import json
import os
import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.access import tags
from apps.access.management.commands.create_demo_exit import avatar
from apps.access.models import AccessEvent, Device, KeyFob
from apps.core import fakes
from apps.equipment import rules
from apps.equipment.models import Equipment, Induction, MachineStatusChange, ServiceRecord, UsageSession
from apps.expenses import claims
from apps.expenses.models import Expense
from apps.members.models import Address, Member, MemberPhoto
from apps.payments import services
from apps.payments.credit import credit_pence
from apps.payments.models import Payment
from apps.profiles import photos
from apps.proposals import voting
from apps.proposals.models import Proposal

FOB_PREFIX = "70"
FIRST_CARD = 2026001  # member n gets card FIRST_CARD + n

S = Member.Status
P = Member.PaymentMethod
# (given, family, status, roles, key holder, payment method, £ a month, photo colour, trained on)
# Roles: "trustee" (can do everything, admin site included) or a team's group name.
MEMBERS = [
    ("Ada", "Lovelace", S.ACTIVE, {"trustee"}, True, P.CREDIT, 30, (64, 120, 200), {"laser-cutter", "3d-printers"}),
    (
        "Isambard",
        "Brunel",
        S.ACTIVE,
        {"trustee", "metalwork"},
        True,
        P.STANDING_ORDER,
        35,
        (90, 90, 90),
        {"metal-lathe", "milling-machine", "mig-welder"},
    ),
    (
        "Grace",
        "Hopper",
        S.ACTIVE,
        {"trustee", "finance"},
        True,
        P.STANDING_ORDER,
        30,
        (40, 140, 150),
        {"laser-cutter", "cnc-engraver"},
    ),
    (
        "Magnus",
        "Volk",
        S.ACTIVE,
        {"trustee", "acs"},
        True,
        P.CREDIT,
        25,
        (200, 90, 60),
        {"metal-lathe", "woodwork-machines"},
    ),
    ("Rosalind", "Franklin", S.ACTIVE, {"trustee"}, True, P.STANDING_ORDER, 30, (140, 90, 170), {"3d-printers"}),
    (
        "Hedy",
        "Lamarr",
        S.ACTIVE,
        {"laser-cutter"},
        True,
        P.CREDIT,
        25,
        (180, 60, 110),
        {"laser-cutter", "cnc-engraver"},
    ),
    (
        "Thomas",
        "Chippendale",
        S.ACTIVE,
        {"woodwork"},
        True,
        P.STANDING_ORDER,
        25,
        (150, 100, 50),
        {"woodwork-machines", "wood-lathe", "band-saw"},
    ),
    (
        "Henry",
        "Maudslay",
        S.ACTIVE,
        {"metalwork"},
        True,
        P.CREDIT,
        25,
        (70, 110, 70),
        {"metal-lathe", "milling-machine"},
    ),
    (
        "Charles",
        "Babbage",
        S.ACTIVE,
        {"3d-printers"},
        True,
        P.CASH,
        20,
        (60, 150, 100),
        {"3d-printers", "laser-cutter"},
    ),
    ("William", "Morris", S.ACTIVE, {"textiles"}, True, P.STANDING_ORDER, 20, (110, 140, 60), set()),
    ("Barbara", "Hepworth", S.ACTIVE, set(), True, P.CREDIT, 20, (180, 150, 40), {"wood-lathe", "band-saw"}),
    ("Alan", "Turing", S.ACTIVE, set(), True, P.STANDING_ORDER, 25, (50, 80, 160), {"laser-cutter", "3d-printers"}),
    ("Marie", "Curie", S.ACTIVE, set(), True, P.CREDIT, 20, (120, 60, 60), {"cnc-engraver"}),
    ("Michael", "Faraday", S.HONORARY, set(), True, P.NONE, 0, (90, 120, 160), {"metal-lathe"}),
    ("Katherine", "Johnson", S.ACTIVE, set(), False, P.STANDING_ORDER, 15, (160, 80, 130), {"3d-printers"}),
    ("Emmy", "Noether", S.ACTIVE, set(), False, P.CREDIT, 15, None, set()),
    ("Eileen", "Gray", S.ACTIVE, set(), True, P.STANDING_ORDER, 20, (100, 100, 140), {"woodwork-machines"}),
    ("Lise", "Meitner", S.ACTIVE, set(), False, P.CASH, 15, None, {"band-saw"}),
    ("Nikola", "Tesla", S.ACTIVE, set(), False, P.CREDIT, 15, (60, 60, 120), {"laser-cutter"}),
    ("Mary", "Anning", S.SETTING_UP, set(), False, P.NONE, 20, None, set()),
    ("James", "Watt", S.LEAVING, set(), True, P.STANDING_ORDER, 20, (130, 110, 90), {"milling-machine"}),
    ("George", "Stephenson", S.LEFT, set(), False, P.NONE, 20, None, {"metal-lathe"}),
    ("Thomas", "Edison", S.SUSPENDED, set(), False, P.STANDING_ORDER, 20, None, {"3d-printers"}),
    ("Victor", "Frankenstein", S.ACTIVE, set(), False, P.CREDIT, 15, None, {"laser-cutter"}),
]
TRAINERS = {
    ("Hedy", "laser-cutter"),
    ("Ada", "laser-cutter"),
    ("Thomas", "woodwork-machines"),
    ("Henry", "metal-lathe"),
    ("Henry", "milling-machine"),
    ("Charles", "3d-printers"),
    ("Barbara", "wood-lathe"),
    ("Isambard", "mig-welder"),
    ("Grace", "cnc-engraver"),
    ("Thomas", "band-saw"),
    ("Thomas", "wood-lathe"),
}
BANNED = ("Victor", "Unauthorised experiments in the workshop after hours.")

M = Equipment.MeterMode
# (name, slug, room, induction category ("" = none), access fee £, £ an hour, team, PPE, metered by current at (A),
#  bought for £, service every (hours), description)
MACHINES = [
    (
        "Laser cutter",
        "laser-cutter",
        "Laser room",
        "laser-cutter",
        40,
        3,
        "laser-cutter",
        "",
        2.5,
        4500,
        50,
        "100 W CO2 laser, 900 × 600 mm bed. Cuts plywood and acrylic up to 6 mm; never PVC.",
    ),
    (
        "Pillar drill",
        "pillar-drill",
        "Workshop",
        "",
        0,
        0,
        "woodwork",
        "Eye protection",
        None,
        350,
        None,
        "Floor-standing, 16 speeds, with a drill press vice.",
    ),
    (
        "Table saw",
        "table-saw",
        "Workshop",
        "woodwork-machines",
        25,
        0,
        "woodwork",
        "Eye protection, ear defenders",
        None,
        1800,
        None,
        '10" cabinet saw with a riving knife and sliding table. Always use the push stick.',
    ),
    (
        "Planer thicknesser",
        "planer",
        "Workshop",
        "woodwork-machines",
        25,
        0,
        "woodwork",
        "Eye protection, ear defenders",
        None,
        1200,
        None,
        "260 mm planer thicknesser. Shares the table saw's access fee.",
    ),
    (
        "Wood lathe",
        "wood-lathe",
        "Workshop",
        "wood-lathe",
        15,
        0,
        "woodwork",
        "Face shield",
        None,
        900,
        None,
        "Variable speed, 400 mm swing.",
    ),
    (
        "Band saw",
        "band-saw",
        "Workshop",
        "band-saw",
        10,
        0,
        "woodwork",
        "Eye protection",
        None,
        600,
        None,
        '14" band saw for curves and resawing.',
    ),
    (
        "Metal lathe",
        "metal-lathe",
        "Metal shop",
        "metal-lathe",
        30,
        0,
        "metalwork",
        "Eye protection",
        None,
        3200,
        200,
        "Myford-style centre lathe with quick-change tool post.",
    ),
    (
        "Milling machine",
        "milling-machine",
        "Metal shop",
        "milling-machine",
        30,
        0,
        "metalwork",
        "Eye protection",
        None,
        2800,
        200,
        "Turret mill with a digital readout.",
    ),
    (
        "MIG welder",
        "mig-welder",
        "Metal shop",
        "mig-welder",
        30,
        2,
        "metalwork",
        "Welding helmet, gloves, leather apron",
        5.0,
        900,
        None,
        "250 A MIG welder, gas cylinder in the cage.",
    ),
    (
        "3D printer (Prusa 1)",
        "prusa-1",
        "Electronics room",
        "3d-printers",
        10,
        0.5,
        "3d-printers",
        "",
        0.3,
        950,
        300,
        "Prusa MK4, 0.4 mm nozzle, PLA and PETG.",
    ),
    (
        "3D printer (Prusa 2)",
        "prusa-2",
        "Electronics room",
        "3d-printers",
        10,
        0.5,
        "3d-printers",
        "",
        0.3,
        950,
        300,
        "Prusa MK4, 0.6 mm nozzle for quick prints.",
    ),
    (
        "3D printer (Bambu)",
        "bambu-x1",
        "Electronics room",
        "3d-printers",
        10,
        1,
        "3d-printers",
        "",
        0.3,
        1400,
        300,
        "Bambu Lab X1 Carbon with the four-colour AMS.",
    ),
    (
        "CNC engraver",
        "cnc-engraver",
        "Laser room",
        "cnc-engraver",
        20,
        2,
        "laser-cutter",
        "Eye protection",
        1.0,
        1100,
        None,
        "Desktop CNC router, 300 × 180 mm, for engraving and PCBs.",
    ),
    (
        "Sewing machine",
        "sewing-machine",
        "Craft room",
        "",
        0,
        0,
        "textiles",
        "",
        None,
        400,
        None,
        "Janome computerised sewing machine.",
    ),
    ("Overlocker", "overlocker", "Craft room", "", 0, 0, "textiles", "", None, 300, None, "Four-thread overlocker."),
    (
        "Vinyl cutter",
        "vinyl-cutter",
        "Craft room",
        "",
        0,
        0,
        "textiles",
        "",
        None,
        250,
        None,
        "Roland 60 cm vinyl cutter for stickers and heat transfer.",
    ),
]
OUT_OF_ORDER = ("mig-welder", "Wire feed keeps jamming; a new liner is on order.")
RETIRED = (
    "Old laser (K40)",
    "old-k40",
    "laser-cutter",
    350,
    2,
    "Retired: the tube failed and it was replaced by the big laser cutter.",
)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


class Command(BaseCommand):
    help = "Fill an empty database with a made-up makerspace, for a demo site (DEMO_SITE=True) only."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default=os.environ.get("DEMO_PASSWORD"),
            help="The password every demo member logs in with (default: the DEMO_PASSWORD environment variable).",
        )
        parser.add_argument("--keys-file", help="Also write the devices' API keys to this JSON file.")

    def handle(self, *args, **options):
        if not fakes.allowed():
            raise CommandError("This is for a demo site only: set DEMO_SITE=True (or DEBUG=True when developing).")
        if not options["password"]:
            raise CommandError("Give the members' password with --password or DEMO_PASSWORD.")
        if Member.objects.exists():
            raise CommandError("The database already has members: seed_demo only fills an empty one.")
        self.rng = random.Random(2026)
        self.today = timezone.localdate()
        self.now = timezone.now()
        with transaction.atomic():
            groups = self.groups()
            machines = self.machines(groups)
            members = self.members(options["password"], groups)
            self.training(members, machines)
            self.history(members, machines)
            self.today_at_the_space(members, machines)
            self.extras(members, machines)
            keys = self.devices(machines)
        self.report(members, machines, keys, options.get("keys_file"))

    # -- people and roles -----------------------------------------------------------------------------------------

    def groups(self):
        names = {"finance", "acs", "woodwork", "metalwork", "laser-cutter", "3d-printers", "textiles"}
        groups = {name: Group.objects.get_or_create(name=name)[0] for name in names}
        groups["finance"].permissions.add(
            *Permission.objects.filter(content_type__app_label__in=("finance",)),
            *Permission.objects.filter(codename__in=("approve_expense", "see_all_expenses")),
        )
        return groups

    def members(self, password, groups):
        members = {}
        for n, (given, family, status, roles, key_holder, method, monthly, colour, _) in enumerate(MEMBERS):
            joined = self.now - timedelta(days=60 + 47 * n % 900)
            member = Member.objects.create_user(f"{given.lower()}.{family.lower()}@example.com", password)
            member.given_name, member.family_name, member.status = given, family, status
            member.date_joined = joined
            member.phone = f"07700 900{n:03d}"  # Ofcom's range for drama
            member.emergency_contact = "A friend, 07700 900999"
            member.monthly_subscription_pence = monthly * 100 or None
            member.payment_method = method
            member.payment_day = 1 + n % 28 if method else None
            if status != S.SETTING_UP:
                member.email_verified_at = joined
                member.induction_completed = True
                member.rules_agreed_at = member.inducted_at = joined + timedelta(days=3)
                member.trusted = member.key_holder = key_holder
            if status in (S.ACTIVE, S.HONORARY):
                member.subscription_expires = self.today + timedelta(days=40 + n)
            elif status == S.LEAVING:
                member.subscription_expires = self.today + timedelta(days=12)
            elif status == S.LEFT:
                member.subscription_expires = self.today - timedelta(days=50)
            elif status == S.SUSPENDED:
                member.subscription_expires = self.today - timedelta(days=9)
                member.suspended_at = self.now - timedelta(days=2)
            if "trustee" in roles:
                member.is_staff = member.is_superuser = True
            if given == BANNED[0]:
                member.banned, member.banned_at, member.banned_reason = True, self.now - timedelta(days=5), BANNED[1]
                member.status = S.LEFT
            member.save()
            for role in roles - {"trustee"}:
                member.groups.add(groups[role])
            if colour:
                full, thumb = photos.process(avatar(given[0] + family[0], colour))
                photos._store(member, MemberPhoto.Kind.CURRENT, full, thumb)
                member.photo_approved = True
                member.save(update_fields=["photo_approved"])
            Address.objects.create(
                member=member,
                line_1=f"{n + 1} Madeira Drive",
                line_2="Brighton",
                postcode="BN2 1TB",
                approved=status != S.SETTING_UP,
            )
            KeyFob.objects.create(member=member, tag_id=tags.full_tag(FOB_PREFIX, FIRST_CARD + n), active=True)
            members[given + " " + family] = member
        return members

    # -- machines ---------------------------------------------------------------------------------------------------

    def machines(self, groups):
        machines = {}
        for name, slug, room, category, fee, rate, team, ppe, amps, bought, service, about in MACHINES:
            machines[slug] = Equipment.objects.create(
                name=name,
                slug=slug,
                room=room,
                team=groups[team],
                ppe=ppe,
                description=about,
                requires_induction=bool(category),
                induction_category=category if category != slug else "",
                induction_fee_pence=fee * 100 if category else 0,
                usage_cost_pence_per_hour=int(rate * 100),
                meter_mode=M.CURRENT if amps else M.SESSION,
                current_threshold_amps=Decimal(str(amps)) if amps else None,
                service_interval_hours=service,
                purchase_price_pence=bought * 100,
                obtained_at=self.today - timedelta(days=400 + 37 * len(machines)),
            )
        name, slug, team, bought, rate, note = RETIRED
        machines[slug] = Equipment.objects.create(
            name=name,
            slug=slug,
            room="Laser room",
            team=groups[team],
            requires_induction=True,
            induction_fee_pence=2500,
            usage_cost_pence_per_hour=rate * 100,
            purchase_price_pence=bought * 100,
            obtained_at=self.today - timedelta(days=1600),
            description=note,
        )
        return machines

    def category_machines(self, machines, category):
        return [m for m in machines.values() if m.induction_key == category and not m.archived]

    def training(self, members, machines):
        """Access fees paid and sign-offs, as each member's `trained on` says."""
        for given, family, _status, *_, trained in MEMBERS:
            member = members[given + " " + family]
            for category in sorted(trained):
                machine = self.category_machines(machines, category)[0]
                when = member.date_joined + timedelta(days=self.rng.randint(5, 50))
                if machine.induction_fee_pence:
                    payment = services.pay_induction(member, machine, Payment.Source.STRIPE)
                    self.backdate(payment, when)
                induction, _ = Induction.objects.get_or_create(member=member, category=category)
                induction.trained_at = when + timedelta(days=4)
                induction.is_trainer = (given, category) in TRAINERS
                induction.save()
        # Two members waiting to be trained: paid, not signed off yet.
        for who, slug in (("Emmy Noether", "laser-cutter"), ("Katherine Johnson", "metal-lathe")):
            self.backdate(
                services.pay_induction(members[who], machines[slug], Payment.Source.STRIPE),
                self.now - timedelta(days=3),
            )
        trainers = {(g, c): m for (g, c) in TRAINERS for m in members.values() if m.given_name == g}
        for induction in Induction.objects.filter(trained_at__isnull=False):
            trainer = next(
                (m for (g, c), m in trainers.items() if c == induction.category and m != induction.member), None
            )
            Induction.objects.filter(pk=induction.pk).update(trainer=trainer)

    def backdate(self, payment, when):
        Payment.objects.filter(pk=payment.pk).update(created_at=when, paid_at=when if payment.paid_at else None)

    # -- three months of history --------------------------------------------------------------------------------------

    def history(self, members, machines):
        rng, people = self.rng, list(members.values())
        current = [m for m in people if m.status in (S.ACTIVE, S.HONORARY, S.LEAVING) and not m.banned]
        # Monthly subscriptions, and credit topped up now and then.
        for member in people:
            if not member.monthly_subscription_pence or member.status == S.SETTING_UP:
                continue
            months = 6 if member.status != S.LEFT else 3
            source = {
                P.CREDIT: Payment.Source.BALANCE,
                P.STANDING_ORDER: Payment.Source.STANDING_ORDER,
                P.CASH: Payment.Source.CASH,
            }.get(member.payment_method, Payment.Source.STANDING_ORDER)
            for back in range(months, 0, -1):
                day = self.today - timedelta(days=30 * back - (member.payment_day or 1) % 28)
                if member.status == S.LEFT and back < 3:
                    continue
                if source == Payment.Source.BALANCE:
                    top_up = services.record_payment(
                        member,
                        Payment.Reason.BALANCE,
                        Payment.Source.STRIPE,
                        member.monthly_subscription_pence + 1500,
                        fee_pence=int((member.monthly_subscription_pence + 1500) * 0.015) + 20,
                    )
                    self.backdate(top_up, at(day, 9) - timedelta(days=2))
                pay = services.record_payment(
                    member,
                    Payment.Reason.SUBSCRIPTION,
                    source,
                    member.monthly_subscription_pence,
                    reference=day.strftime("%Y-%m"),
                )
                self.backdate(pay, at(day, 10))
        # Visits and machine sessions, most days.
        trained = {}
        for induction in Induction.objects.filter(trained_at__isnull=False):
            for machine in self.category_machines(machines, induction.category):
                trained.setdefault(induction.member_id, []).append(machine)
        free = [m for m in machines.values() if not m.requires_induction and not m.archived]
        for back in range(90, 0, -1):
            day = self.today - timedelta(days=back)
            for member in rng.sample(current, rng.randint(2, 7) if day.weekday() < 5 else rng.randint(5, 11)):
                arrive = at(day, rng.choice((10, 13, 17, 18, 19)), rng.randint(0, 59))
                AccessEvent.objects.create(
                    member=member,
                    kind=AccessEvent.Kind.ENTRY if member.key_holder else AccessEvent.Kind.CHECK_IN,
                    occurred_at=arrive,
                )
                options = trained.get(member.pk, []) + free
                if rng.random() < 0.7:
                    machine = rng.choice(options)
                    start = arrive + timedelta(minutes=rng.randint(5, 40))
                    session = UsageSession.objects.create(equipment=machine, member=member, started_at=start)
                    end = start + timedelta(minutes=rng.randint(10, 150))
                    rules.end_session(session, when=end, ended_by=UsageSession.EndedBy.TAP)
                    for payment in session.payments.all():
                        self.backdate(payment, end)
                    UsageSession.objects.filter(pk=session.pk).update(charged_at=end)
                AccessEvent.objects.create(
                    member=member, kind=AccessEvent.Kind.EXIT, occurred_at=arrive + timedelta(hours=rng.randint(2, 4))
                )
        # The retired laser's life: access fees and use, years ago.
        old = machines["old-k40"]
        for i, member in enumerate(people[:9]):
            when = self.now - timedelta(days=1500 - 60 * i)
            self.backdate(services.pay_induction(member, old, Payment.Source.CASH), when)
            for j in range(4):
                start = when + timedelta(days=30 * (j + 1), hours=3)
                session = UsageSession.objects.create(equipment=old, member=member, started_at=start)
                rules.end_session(
                    session, when=start + timedelta(minutes=45 + 20 * j), ended_by=UsageSession.EndedBy.TAP
                )
                for payment in session.payments.all():
                    self.backdate(payment, start)
        Expense.objects.create(
            member=members["Hedy Lamarr"],
            category=Expense.Category.EQUIPMENT_REPAIR,
            equipment=old,
            description="Replacement air assist pump for the K40.",
            amount_pence=3499,
            spent_on=self.today - timedelta(days=900),
            status=Expense.Status.APPROVED,
            decided_by=members["Grace Hopper"],
            decided_at=self.now - timedelta(days=898),
        )
        old.archived, old.removed_at = True, self.today - timedelta(days=200)
        old.save(update_fields=["archived", "removed_at"])
        Induction.objects.filter(category=old.induction_key).update(trained_at=self.now - timedelta(days=1400))
        # Everyone current ends up with some credit, so their fobs work on the machines straight away.
        for member in current:
            short = 2000 - credit_pence(member)
            if short > 0:
                top_up = services.record_payment(
                    member, Payment.Reason.BALANCE, Payment.Source.STRIPE, short + 500 - short % 500
                )
                self.backdate(top_up, self.now - timedelta(days=1))

    def today_at_the_space(self, members, machines):
        """A few people in right now (for the exit node's screen), one of them using the laser cutter."""
        for minutes, who in (
            (150, "Ada Lovelace"),
            (95, "Thomas Chippendale"),
            (45, "Alan Turing"),
            (20, "Hedy Lamarr"),
            (10, "Katherine Johnson"),
        ):
            member = members[who]
            AccessEvent.objects.create(
                member=member,
                kind=AccessEvent.Kind.ENTRY if member.key_holder else AccessEvent.Kind.CHECK_IN,
                occurred_at=self.now - timedelta(minutes=minutes),
            )
        UsageSession.objects.create(
            equipment=machines["laser-cutter"],
            member=members["Alan Turing"],
            started_at=self.now - timedelta(minutes=12),
            last_heartbeat_at=self.now,
        )

    def extras(self, members, machines):
        grace, hedy = members["Grace Hopper"], members["Hedy Lamarr"]
        # A machine out of order, with a reason.
        slug, note = OUT_OF_ORDER
        welder = machines[slug]
        welder.status, welder.status_note, welder.status_since = Equipment.Status.OUT_OF_ORDER, note, self.now
        welder.save()
        MachineStatusChange.objects.create(
            equipment=welder,
            status=welder.status,
            note=note,
            reported_by=members["Henry Maudslay"],
            source=MachineStatusChange.Source.WEB,
        )
        # Service records.
        for slug, days, text in (
            ("laser-cutter", 20, "Cleaned the mirrors and lens; checked alignment."),
            ("metal-lathe", 60, "Changed the gearbox oil."),
            ("prusa-1", 35, "New nozzle and PEI sheet."),
        ):
            ServiceRecord.objects.create(
                equipment=machines[slug],
                serviced_at=self.today - timedelta(days=days),
                recorded_by=members["Isambard Brunel"],
                note=text,
            )
        # Expenses: one approved (paid into credit), two waiting for a decision.
        laser_tube = Expense.objects.create(
            member=hedy,
            category=Expense.Category.EQUIPMENT_REPAIR,
            equipment=machines["laser-cutter"],
            description="Replacement laser tube.",
            amount_pence=18000,
            spent_on=self.today - timedelta(days=25),
        )
        claims.approve(laser_tube.pk, grace)
        Expense.objects.create(
            member=members["Thomas Chippendale"],
            category=Expense.Category.CONSUMABLES,
            equipment=machines["table-saw"],
            description="Two new blades for the table saw.",
            amount_pence=8650,
            spent_on=self.today - timedelta(days=4),
        )
        Expense.objects.create(
            member=members["William Morris"],
            category=Expense.Category.CONSUMABLES,
            description="Thread and needles for the sewing machine.",
            amount_pence=1720,
            spent_on=self.today - timedelta(days=2),
        )
        # A proposal open for votes.
        proposal = Proposal.objects.create(
            title="Buy a second laser cutter",
            description="The laser cutter is booked solid most evenings. This proposes buying a second, smaller "
            "laser (about **£2,500**) from the equipment fund, kept in the laser room.\n\n- Same access fee as the "
            "big laser\n- £2 an hour",
            start_date=self.today - timedelta(days=2),
            end_date=self.today + timedelta(days=12),
            created_by=members["Ada Lovelace"],
        )
        voting.open_voting(proposal.pk)

    def devices(self, machines):
        keys = {}
        for slug, machine in machines.items():
            if machine.archived:
                continue
            device = Device(device_id=slug, name=f"{machine.name} reader", role=Device.Role.EQUIPMENT)
            keys[slug] = device.generate_api_key()
            device.save()
        for device_id, name, role in (
            ("front-door", "Front door", Device.Role.ENTRY),
            ("exit-node", "Exit node", Device.Role.EXIT),
        ):
            device = Device(device_id=device_id, name=name, role=role)
            keys[device_id] = device.generate_api_key()
            device.save()
        return keys

    # -- what to tell people ----------------------------------------------------------------------------------------

    def report(self, members, machines, keys, keys_file):
        rows = []
        for n, (given, family, _status, roles, _key_holder, *_rest) in enumerate(MEMBERS):
            member = members[given + " " + family]
            rows.append(
                {
                    "name": member.full_name,
                    "email": member.email,
                    "status": member.get_status_display(),
                    "banned": member.banned,
                    "roles": sorted(roles),
                    "key_holder": bool(member.key_holder),
                    "card": tags.format_card_number(FIRST_CARD + n),
                    "tag": tags.full_tag(FOB_PREFIX, FIRST_CARD + n),
                }
            )
        data = {
            "members": rows,
            "keys": keys,
            "machines": [{"name": m.name, "slug": m.slug, "archived": m.archived} for m in machines.values()],
        }
        if keys_file:
            with open(keys_file, "w") as out:
                json.dump(data, out, indent=2)
        for row in rows:
            self.stdout.write(f"  {row['card']}  {row['name']:<22} {row['email']:<32} {row['status']}")
        for device_id, key in keys.items():
            self.stdout.write(f"  {device_id:<16} {key}")
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(rows)} members and {len(machines)} machines."))
