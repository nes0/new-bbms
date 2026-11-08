from .models import Notification


def unread(request):
    """How many unread notifications the member has, for the link in the page header."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {"unread_notifications": Notification.objects.filter(member=user, unread=True).count()}
