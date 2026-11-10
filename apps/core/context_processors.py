from django.conf import settings

from apps.site_settings.models import SiteSettings


def site(request):
    """Values every template can use. The space name is an admin setting (02 §2.8).

    IS_MEMBER: whether the members' pages (directory, who's in, equipment, proposals, stats, feedback) are open to
    the viewer, so the header doesn't offer links that would say "Not found" (e.g. to someone still setting up).
    """
    from apps.profiles.views import is_member

    config = SiteSettings.load()
    user = getattr(request, "user", None)
    member = bool(user and is_member(user))
    return {
        "SITE_NAME": config.space_name,
        "DEMO_SITE": settings.DEMO_SITE,
        "IS_MEMBER": member,
        "SHOW_DIRECTORY": member or bool(user and user.is_authenticated and config.public_member_directory),
    }
