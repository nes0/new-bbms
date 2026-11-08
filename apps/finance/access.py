"""Who sees the finance pages (spec 01 §1.3): the `finance` role and admins."""

from django.core.exceptions import PermissionDenied


def can_see(user) -> bool:
    return user.is_authenticated and (user.has_perm("finance.see_finance") or user.has_perm("finance.manage_finance"))


def can_manage(user) -> bool:
    """Import statements and put payments right."""
    return user.is_authenticated and user.has_perm("finance.manage_finance")


def require_see(user):
    if not can_see(user):
        raise PermissionDenied


def require_manage(user):
    if not can_manage(user):
        raise PermissionDenied
