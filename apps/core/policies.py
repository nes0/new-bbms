"""The space's policies, kept as markdown files in apps/core/policies/ (spec 06 §6.8).

The files are part of the code (changed by pull request), so their HTML is trusted and isn't sanitised.
"""

from functools import cache
from pathlib import Path

import markdown

POLICIES = Path(__file__).resolve().parent / "policies"


@cache
def html(name, top_heading=2):
    """A policy as HTML, e.g. html("member-agreement"). Its "# Heading" becomes <h2 id="heading"> (`top_heading`), to
    sit under the page's own <h1>. Unknown names raise FileNotFoundError.
    """
    if "/" in name or "." in name:
        raise FileNotFoundError(name)
    text = (POLICIES / f"{name}.md").read_text(encoding="utf-8")
    return markdown.markdown(
        text, extensions=["sane_lists", "toc"], extension_configs={"toc": {"baselevel": top_heading}}
    )


# The resources page's lists (06 §6.8): (file name, title), in the order shown.
CORE_RULES = [
    ("rules", "Rules"),
    ("code-of-conduct", "Code of conduct"),
    ("grievance-procedure", "Grievance procedure"),
    ("member-agreement", "Member agreement"),
    ("trusted-member", "Trusted member"),
    ("permaloan", "Permaloan"),
]
OTHER_POLICIES = [
    ("3-week-bins", "3 week bins"),
    ("member-storage", "Member storage and ongoing projects"),
]
TITLES = dict(CORE_RULES + OTHER_POLICIES)
