from django.core.management.base import BaseCommand

from apps.moodle import client, completion


class Command(BaseCommand):
    help = "Ask Moodle whether members waiting for sign-off have finished their courses. Every 15 minutes (06 §6.12)."

    def handle(self, *args, **options):
        if not client.enabled():
            self.stdout.write("Moodle isn't set up (its address in Settings, and MOODLE_TOKEN): nothing to check.")
            return
        summary = completion.check_waiting()
        self.stdout.write(
            f"Checked {summary.checked} member(s); {summary.completed} finished their course."
            + (" Moodle couldn't be reached." if summary.unavailable else "")
        )
