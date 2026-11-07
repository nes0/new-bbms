from django.conf import settings


def site(request):
    """Values every template can use."""
    return {"SITE_NAME": settings.SITE_NAME}
