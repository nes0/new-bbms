"""Every member has a profile, made when the member is."""

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def make_profile(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        Profile.objects.get_or_create(member=instance)
