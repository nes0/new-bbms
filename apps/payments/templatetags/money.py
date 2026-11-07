from django import template

register = template.Library()


@register.filter
def pounds(pence) -> str:
    """1234 -> "£12.34", -50 -> "−£0.50"."""
    pence = int(pence or 0)
    sign = "−" if pence < 0 else ""
    return f"{sign}£{abs(pence) / 100:,.2f}"
