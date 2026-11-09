from django.conf import settings

from apps.site_settings.models import SiteSettings


def site(request):
    """Values every template can use. The space name is an admin setting (02 §2.8)."""
    return {"SITE_NAME": SiteSettings.load().space_name, "DEMO_SITE": settings.DEMO_SITE}
