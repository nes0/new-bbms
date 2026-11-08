"""
The member directory, member pages, editing your profile, photos and the groups page (spec 06 §6.1).

Who sees what:
- **Members** (logged in, and a current member: active, leaving, suspended or honorary; or an admin) see every
  listed member, and the groups page.
- **Visitors** see the list only if the trustees turn it on (Settings → Privacy), and never members who chose a
  private profile.
- Listed: active and honorary members who aren't banned (legacy: active, not leaving).
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.db.models import Prefetch, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from apps.members.models import Member, MemberPhoto
from apps.site_settings.models import SiteSettings

from . import photos, skills
from .forms import ProfileForm
from .models import Profile

LISTED_STATUSES = (Member.Status.ACTIVE, Member.Status.HONORARY)
VIEWER_STATUSES = (*Member.STATUSES_WITH_ACCESS, Member.Status.SUSPENDED)
TRUSTEE_ROLE = "trustees"  # shown with a star in the directory, as legacy did


def is_member(user) -> bool:
    """A current member (or an admin): sees the whole directory and the groups page."""
    if not user.is_authenticated or user.banned:
        return False
    return user.status in VIEWER_STATUSES or user.is_staff


def listed_members(viewer):
    """The members `viewer` may see in the directory, or None if they may not see it at all."""
    members = Member.objects.filter(status__in=LISTED_STATUSES, banned=False)
    if is_member(viewer):
        return members
    if not SiteSettings.load().public_member_directory:
        return None
    return members.filter(profile_private=False)


def _profile(member) -> Profile:
    """Every member has one (made with the member); this makes one if something slipped through."""
    try:
        return member.profile
    except Profile.DoesNotExist:
        return Profile.objects.get_or_create(member=member)[0]


def _with_photos(members):
    current = MemberPhoto.objects.filter(kind=MemberPhoto.Kind.CURRENT).only("pk", "member_id", "kind")
    return members.select_related("profile").prefetch_related(
        Prefetch("photos", queryset=current, to_attr="current_photos"),
        Prefetch("groups", queryset=Group.objects.filter(name=TRUSTEE_ROLE), to_attr="trustee_groups"),
    )


def _card(viewer, member):
    """What the directory shows for one member."""
    profile = _profile(member)
    has_photo = bool(member.current_photos) and photos.can_see(viewer, member)
    return {
        "member": member,
        "profile": profile,
        "photo_url": reverse("profiles:photo", args=[member.pk, "thumb"]) if has_photo else "",
        "trustee": bool(member.trustee_groups),
    }


def directory(request):
    members = listed_members(request.user)
    if members is None:
        if request.user.is_authenticated:
            raise Http404  # e.g. someone still setting up, or who has left
        return redirect(f"{reverse('login')}?next={request.path}")
    query = request.GET.get("q", "").strip()
    skill = request.GET.get("skill", "")
    if query:
        members = members.filter(Q(given_name__icontains=query) | Q(family_name__icontains=query))
    if skill in skills.BY_KEY:
        # JSON "contains" isn't available on every database, so filter here (a few hundred members at most).
        members = [m for m in _with_photos(members) if skill in _profile(m).skills]
    else:
        members = _with_photos(members)
    members = sorted(members, key=lambda m: (m.given_name.lower(), m.family_name.lower()))
    context = {
        "cards": [_card(request.user, m) for m in members],
        "q": query,
        "skill": skill,
        "skills": skills.SKILLS,
        "is_member": is_member(request.user),
    }
    return render(request, "profiles/directory.html", context)


def member_page(request, member_id):
    members = listed_members(request.user)
    member = None
    if members is not None:
        member = _with_photos(members.filter(pk=member_id)).first()
    if member is None and request.user.is_authenticated and request.user.pk == member_id:
        member = _with_photos(Member.objects.filter(pk=member_id)).first()  # your own page, whatever your status
    if member is None:
        raise Http404
    card = _card(request.user, member)
    pending = None
    if request.user.pk == member.pk:
        pending = member.photos.filter(kind=MemberPhoto.Kind.PENDING).first()
    context = {
        **card,
        "card": card,
        "full_photo_url": reverse("profiles:photo", args=[member.pk, "full"]) if card["photo_url"] else "",
        "skills": skills.skills_for(card["profile"].skills),
        "groups": _groups_of(member) if is_member(request.user) else [],
        "is_own": request.user.pk == member.pk,
        "show_discord": is_member(request.user),  # members only: how to find each other on the server
        "pending": pending,
    }
    return render(request, "profiles/member.html", context)


def _groups_of(member):
    groups = member.groups.select_related("info").order_by("name")
    return [g for g in groups if not hasattr(g, "info") or g.info.listed]


@require_http_methods(["GET"])
def photo(request, member_id, size):
    """A member's photo, if the viewer may see it. `?waiting=1` for one waiting for approval (owner and admins)."""
    member = get_object_or_404(Member.objects.select_related("profile"), pk=member_id)
    kind = MemberPhoto.Kind.PENDING if request.GET.get("waiting") else MemberPhoto.Kind.CURRENT
    found = member.photos.filter(kind=kind).first()
    may_see_member = request.user.pk == member.pk or photos.can_moderate(request.user)
    if not may_see_member:
        members = listed_members(request.user)
        may_see_member = members is not None and members.filter(pk=member.pk).exists()
    if (
        found is None
        or size not in ("full", "thumb")
        or not may_see_member
        or not photos.can_see(request.user, member, kind)
    ):
        raise Http404
    file = found.image if size == "full" else found.thumbnail
    response = FileResponse(file.open("rb"), content_type="image/png")
    response["Cache-Control"] = "private, max-age=600"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def edit(request):
    member = request.user
    profile = _profile(member)
    form = ProfileForm(request.POST or None, request.FILES or None, instance=profile)
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data.get("photo")
        if upload:
            try:
                photos.upload(member, upload)
            except photos.BadPhoto as error:
                form.add_error("photo", str(error))
        if not form.errors:
            form.save()
            messages.success(
                request,
                "Profile saved. A trustee checks your new photo before it shows." if upload else "Profile saved.",
            )
            return redirect("profiles:member", member.pk)
    context = {
        "form": form,
        "member": member,
        "current": member.photos.filter(kind=MemberPhoto.Kind.CURRENT).first(),
        "pending": member.photos.filter(kind=MemberPhoto.Kind.PENDING).first(),
    }
    return render(request, "profiles/edit.html", context)


@login_required
@require_POST
def remove_photo(request):
    kind = MemberPhoto.Kind.PENDING if request.POST.get("which") == "waiting" else MemberPhoto.Kind.CURRENT
    photos.remove(request.user, kind, request.user)
    messages.success(request, "Photo deleted.")
    return redirect("profiles:edit")


# --- groups ---


def _member_only(request):
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next={request.path}")
    if not is_member(request.user):
        raise Http404
    return None


def groups(request):
    if (response := _member_only(request)) is not None:
        return response
    listed = (
        Group.objects.select_related("info")
        .prefetch_related("discord_channels")
        .exclude(info__listed=False)
        .order_by("name")
    )
    return render(request, "profiles/groups.html", {"groups": listed})


def group(request, name):
    if (response := _member_only(request)) is not None:
        return response
    found = get_object_or_404(Group.objects.select_related("info").exclude(info__listed=False), name=name)
    members = _with_photos(found.user_set.filter(status__in=LISTED_STATUSES, banned=False)).order_by(
        "given_name", "family_name"
    )
    return render(
        request,
        "profiles/group.html",
        {"group": found, "info": getattr(found, "info", None), "cards": [_card(request.user, m) for m in members]},
    )


def credits(request):
    return render(request, "profiles/credits.html", {"skills": [s for s in skills.SKILLS if s.credit]})
