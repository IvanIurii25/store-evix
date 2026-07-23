"""In-memory image byte builders shared by the image-core tests.

No binaries are committed; every fixture is generated with Pillow at call time so
the tests stay hermetic and fast.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image


def rgb_png(width: int, height: int) -> bytes:
    """Return an in-memory RGB PNG of the given size (solid red)."""
    buffer = BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def palette_png(*, transparent: bool, width: int = 60, height: int = 50) -> bytes:
    """Return an in-memory palette (``P``-mode) PNG, optionally with transparency."""
    image = Image.new("P", (width, height))
    if transparent:
        image.info["transparency"] = 0
    buffer = BytesIO()
    image.save(buffer, format="PNG", transparency=0 if transparent else None)
    return buffer.getvalue()


def cmyk_jpeg(width: int, height: int) -> bytes:
    """Return an in-memory CMYK JPEG (exercises the exotic-mode RGB convert)."""
    buffer = BytesIO()
    Image.new("CMYK", (width, height), (0, 0, 0, 0)).save(buffer, format="JPEG")
    return buffer.getvalue()
