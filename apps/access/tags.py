"""
RFID tag ids and card numbers (spec 05 §5.1).

A 125 kHz fob's full tag id, as BBMS has always stored it, is 12 hex characters:

    70 0012D687 33
    |  |        └─ checksum: the 5 bytes before it combined with XOR
    |  └────────── card number (8 hex digits) = 0001234567 in decimal, usually printed on the fob
    └───────────── vendor prefix

Some readers (including today's front door) only give the decimal card number, so BBMS stores the card
number separately and can match a fob by either. Confirmed with a real fob on 2026-11-05.
"""

import re

_HEX12 = re.compile(r"^[0-9A-F]{12}$")


def normalise(tag: str) -> str:
    """Upper-case, without spaces, as tags are stored."""
    return (tag or "").strip().replace(" ", "").upper()


def checksum(hex10: str) -> str:
    """XOR of the five bytes in a 10-hex-digit value, as 2 hex digits."""
    value = 0
    for i in range(0, 10, 2):
        value ^= int(hex10[i : i + 2], 16)
    return f"{value:02X}"


def is_valid_full_tag(tag: str) -> bool:
    """True for a 12-hex tag whose last byte is the correct checksum."""
    tag = normalise(tag)
    return bool(_HEX12.match(tag)) and checksum(tag[:10]) == tag[10:]


def card_number(tag: str) -> int | None:
    """The card number inside a valid full tag, or None if the tag isn't one."""
    tag = normalise(tag)
    if not is_valid_full_tag(tag):
        return None
    return int(tag[2:10], 16)


def full_tag(prefix_hex: str, card: int) -> str:
    """Build a full tag from a 2-hex-digit vendor prefix and a card number."""
    hex10 = f"{int(prefix_hex, 16):02X}{card:08X}"
    return hex10 + checksum(hex10)


def format_card_number(card: int) -> str:
    """As printed on fobs and typed by the USB reader: 10 decimal digits."""
    return f"{card:010d}"
