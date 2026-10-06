"""Screenshot redaction: the fields HighhX classifies as secret are blacked out before an image is
stored, shown to a model or handed to anyone.

Secret fields are the ones the rest of HighhX already treats as secret (``StateElement.secret``):
password inputs, and fields whose ``autocomplete`` says password, card (``cc-…``) or one-time code;
on the desktop, the accessibility API's secure text fields. Browsers draw a password as bullets,
but card numbers, one-time codes and a password shown with "show password" are drawn in clear —
those are what this removes.

What it does not do: it is not general PII detection. Text that merely looks sensitive (an API key
printed on a page, a private message) is not found — HighhX does not claim to. Fields inside
cross-origin frames are not seen by the page query, and on the desktop only the frontmost
application's secure fields are known. The live view (a screencast to the person's own loopback
console, never stored) is not redacted.
"""

from __future__ import annotations

from collections.abc import Iterable

from highhx.perception.png import decode, encode, fill

Box = tuple[int, int, int, int]
"""x, y, width, height in image pixels."""

PAD = 2
"""Pixels added around each field, so a focus ring or caret cannot show an edge of the text."""

SECRET_FIELDS_JS = """(() => [...document.querySelectorAll('input, textarea')].filter((e) => {
  const kind = (e.getAttribute('type') || '').toLowerCase();
  const auto = (e.getAttribute('autocomplete') || '').toLowerCase();
  return kind === 'password' || auto.includes('password') || auto.includes('cc-') || auto.includes('one-time-code');
}).map((e) => e.getBoundingClientRect()).filter((r) => r.width > 0 && r.height > 0)
  .map((r) => [r.x, r.y, r.width, r.height]))()"""
"""The page's secret fields, as client rectangles (CSS pixels, layout viewport) — the same rule as
``StateElement.secret``."""


def redact_png(data: bytes, boxes: Iterable[Box]) -> tuple[bytes, int]:
    """``data`` with every box filled black (clipped to the image), and how many boxes touched
    it. An image no box touches is returned as it was (not re-encoded)."""
    wanted = [b for b in boxes if b[2] > 0 and b[3] > 0]
    if not wanted:
        return data, 0
    image = decode(data)
    rgb = bytearray(image.rgb)
    count = 0
    for x, y, width, height in wanted:
        left, top = max(0, x - PAD), max(0, y - PAD)
        right, bottom = min(image.width, x + width + PAD), min(image.height, y + height + PAD)
        if right <= left or bottom <= top:
            continue
        fill(rgb, image.width, (left, top, right - left, bottom - top), (0, 0, 0))
        count += 1
    if not count:
        return data, 0
    return encode(image.width, image.height, bytes(rgb)), count


def scaled(boxes: Iterable[tuple[float, float, float, float]], scale: float, dx: float = 0, dy: float = 0) -> list[Box]:
    """Boxes from another space into image pixels: ``(v - offset) * scale``, rounded outward."""
    import math

    out = []
    for x, y, width, height in boxes:
        left, top = math.floor((x - dx) * scale), math.floor((y - dy) * scale)
        right, bottom = math.ceil((x - dx + width) * scale), math.ceil((y - dy + height) * scale)
        out.append((left, top, right - left, bottom - top))
    return out
