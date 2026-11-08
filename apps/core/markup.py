"""Markdown written on the site (e.g. a proposal's description), turned into HTML that's safe to show.

Unlike the policies (`policies.py`, part of the code), this text is typed into a form, so:
- raw HTML isn't passed through: `<script>` shows as the text "<script>";
- links and images keep their address only if it's http(s), mailto or on this site (no `javascript:`).
"""

from xml.etree.ElementTree import Element

import markdown
from django.utils.safestring import mark_safe
from markdown.treeprocessors import Treeprocessor

SAFE_SCHEMES = ("http://", "https://", "mailto:")


def _safe_address(value: str) -> bool:
    value = value.strip()
    lowered = "".join(value.split()).lower()  # "java\nscript:" is still javascript:
    if lowered.startswith(SAFE_SCHEMES):
        return True
    return value.startswith(("/", "#", "?")) and not value.startswith("//")


class _ScrubAddresses(Treeprocessor):
    def run(self, root: Element):
        for element in root.iter():
            for attribute in ("href", "src"):
                if attribute in element.attrib and not _safe_address(element.attrib[attribute]):
                    del element.attrib[attribute]
            if element.tag == "a" and "href" in element.attrib and element.attrib["href"].startswith("http"):
                element.set("rel", "nofollow noopener")


def _converter() -> markdown.Markdown:
    md = markdown.Markdown(extensions=["sane_lists"])
    md.preprocessors.deregister("html_block")  # raw HTML blocks become ordinary (escaped) text
    md.inlinePatterns.deregister("html")  # and so do inline tags
    md.treeprocessors.register(_ScrubAddresses(md), "scrub_addresses", 0)
    return md


def to_html(text: str) -> str:
    """The markdown `text` as safe HTML (marked safe for templates)."""
    return mark_safe(_converter().convert(text or ""))  # noqa: S308 - raw HTML and unsafe addresses are removed
