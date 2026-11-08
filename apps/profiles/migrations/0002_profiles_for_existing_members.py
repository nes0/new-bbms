from django.conf import settings
from django.db import migrations


def make_profiles(apps, schema_editor):
    Member = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    Profile = apps.get_model("profiles", "Profile")
    have = set(Profile.objects.values_list("member_id", flat=True))
    Profile.objects.bulk_create(
        [Profile(member_id=pk) for pk in Member.objects.values_list("pk", flat=True) if pk not in have],
        batch_size=500,
    )


class Migration(migrations.Migration):
    dependencies = [("profiles", "0001_initial"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [migrations.RunPython(make_profiles, migrations.RunPython.noop)]
