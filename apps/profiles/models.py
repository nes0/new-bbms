"""
Member profiles (spec 02 §2.3 `profile_data`, 06 §6.1) and what the groups page says about each role.

A member's photos are `members.MemberPhoto` (they're needed for door access, 05 §5.2); the rules for uploading,
approving and showing them are in `photos.py`.
"""

from django.conf import settings
from django.contrib.auth.models import Group
from django.db import models


class Profile(models.Model):
    """What a member says about themselves. Every member has one (made when the member is)."""

    member = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    tagline = models.CharField(max_length=250, blank=True, help_text="A line about you, under your name.")
    description = models.TextField("about you", blank=True, max_length=5000)
    skills = models.JSONField(default=list, blank=True, help_text="Keys from profiles.skills.")
    website = models.URLField(max_length=200, blank=True)
    github = models.CharField("GitHub username", max_length=50, blank=True)
    twitter = models.CharField("X (Twitter) username", max_length=50, blank=True)
    facebook = models.CharField("Facebook username", max_length=50, blank=True)
    irc = models.CharField("IRC nickname", max_length=50, blank=True)
    photo_private = models.BooleanField(
        "only members see my photo", default=False, help_text="Visitors who aren't logged in see a blank picture."
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Profile of {self.member}"

    @property
    def links(self):
        """(label, url) for each link the member gave."""
        found = []
        if self.website:
            found.append(("Website", self.website))
        if self.github:
            found.append(("GitHub", f"https://github.com/{self.github}"))
        if self.twitter:
            found.append(("X (Twitter)", f"https://x.com/{self.twitter}"))
        if self.facebook:
            found.append(("Facebook", f"https://www.facebook.com/{self.facebook}"))
        return found


class RoleInfo(models.Model):
    """How a role (a group of members, 01 §1.3) is described on the groups page."""

    group = models.OneToOneField(Group, on_delete=models.CASCADE, related_name="info")
    title = models.CharField(max_length=100, blank=True, help_text="Shown instead of the role's name.")
    description = models.TextField(blank=True)
    email = models.EmailField("contact email", blank=True, help_text="Shown to members on the groups page.")
    listed = models.BooleanField("show on the groups page", default=True)

    class Meta:
        verbose_name = "role description"

    def __str__(self):
        return self.title or self.group.name


class DiscordChannel(models.Model):
    """A Discord topic channel for a group (06 §6.1, §6.14). A group can have several: groups are broader than the
    server's topics (e.g. a woodwork group with #woodwork, #wood-lathe and #tablesaw). Members choose topic channels
    on Discord itself; the groups page links to them.
    """

    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="discord_channels")
    name = models.CharField(max_length=100, help_text="As it shows on Discord, e.g. #wood-lathe.")
    channel_id = models.CharField(
        "channel id",
        max_length=32,
        blank=True,
        help_text="So the groups page can link to it: on Discord, turn on Developer Mode, then right-click the "
        "channel → Copy Channel ID.",
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]
        constraints = [models.UniqueConstraint(fields=["group", "name"], name="one_discord_channel_name_per_group")]

    def __str__(self):
        return self.name

    @property
    def url(self):
        from django.conf import settings

        if self.channel_id and settings.DISCORD_GUILD_ID:
            return f"https://discord.com/channels/{settings.DISCORD_GUILD_ID}/{self.channel_id}"
        return ""
