"""The portal's colour themes. A member picks one on their home page; the choice is kept in a cookie in their browser.

Each theme is a set of colour variables in static/css/site.css (`body[data-theme="<key>"]`). To add one, add its
variables there and a line here.
"""

COOKIE = "theme"
DEFAULT = "night"

# key: (name, colours for the swatch on the switch)
THEMES = {
    "night": ("Black and lime", ("#151a19", "#c3ed52")),
    "forest": ("Green and peach", ("#19251f", "#91c6a4", "#f0b48a")),
}


def current(request) -> str:
    theme = request.COOKIES.get(COOKIE, "")
    return theme if theme in THEMES else DEFAULT
