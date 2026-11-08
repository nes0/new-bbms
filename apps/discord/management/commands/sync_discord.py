from django.core.management.base import BaseCommand

from apps.discord import client, linking


class Command(BaseCommand):
    help = "Put every linked member's Discord member / former-member role right. Runs nightly (spec 06 §6.14)."

    def handle(self, *args, **options):
        if not client.enabled():
            self.stdout.write("Discord isn't configured: nothing to do.")
            return
        results = linking.sync_all()
        self.stdout.write("Discord roles: " + ", ".join(f"{key} {value}" for key, value in results.items()))
