"""The members' lockers page (spec 06 §6.2, "Pages")."""

from itertools import groupby

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from . import rules


@login_required
def lockers(request):
    member = request.user
    available = sorted(
        rules.available_lockers(),
        key=lambda item: (item[0].location.sort_order, item[0].location.name, item[0].size.sort_order, item[0].number),
    )
    # Location → size → [(locker, monthly rent)], for the page.
    by_location = [
        (location, [(size, list(items)) for size, items in groupby(group, key=lambda item: item[0].size)])
        for location, group in groupby(available, key=lambda item: item[0].location)
    ]
    mine = [(rental, rules.rental_rent_pence(rental)) for rental in rules.open_rentals(member)]
    context = {
        "by_location": by_location,
        "mine": mine,
        "limit": rules.locker_limit(member),
    }
    return render(request, "storage/lockers.html", context)
