"""
QR codes for device screens, in a raw format a microcontroller can draw directly (spec 05 §5.4).

Format (`application/octet-stream`):

    byte 0          N, the number of modules (squares) per side, e.g. 29
    bytes 1 ...     N rows, top to bottom. Each row is ceil(N / 8) bytes; bit 7 (the most significant bit)
                    of the first byte is the leftmost module. 1 = dark, 0 = light. Spare bits at the end of
                    a row are 0.

So the whole thing is 1 + N * ceil(N / 8) bytes (117 bytes for a typical 29 x 29 code). The code has no quiet
zone (white border); the device should leave at least 4 light modules around it. Error correction is level M.
"""

import hashlib

import segno


def make(url: str) -> segno.QRCode:
    return segno.make(url, error="m", micro=False)


def packed_rows(url: str) -> tuple[int, list[bytes]]:
    """The code's size and its rows, each packed into bytes as described above."""
    code = make(url)
    rows = []
    for row in code.matrix_iter(border=0):
        packed = bytearray((len(row) + 7) // 8)
        for x, dark in enumerate(row):
            if dark:
                packed[x // 8] |= 0x80 >> (x % 8)
        rows.append(bytes(packed))
    return len(rows), rows


def raw(url: str) -> bytes:
    size, rows = packed_rows(url)
    return bytes([size]) + b"".join(rows)


def etag(url: str) -> str:
    """Changes only when the URL changes, so a device can cache the code."""
    return '"' + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16] + '"'
