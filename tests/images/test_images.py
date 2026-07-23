"""Unit tests for the pure image helpers in :mod:`app.core.images`.

Pure tests: fixtures are generated in-memory with Pillow, no binaries committed.
"""

from io import BytesIO

import pytest
from PIL import Image

from app.core.images import generate_fixed_webp_set, generate_webp_variants


def _jpeg_bytes(width: int, height: int) -> bytes:
    """Return an in-memory JPEG of the given size (solid red)."""
    buffer = BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format="JPEG")
    return buffer.getvalue()


def _rgba_png_bytes(width: int, height: int) -> bytes:
    """Return an in-memory RGBA PNG with a semi-transparent fill."""
    buffer = BytesIO()
    Image.new("RGBA", (width, height), (10, 200, 50, 128)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_smaller_widths_produced_with_correct_output_width():
    """Widths below the original yield WEBP variants at the requested widths."""
    data = _jpeg_bytes(1000, 800)

    variants = generate_webp_variants(data, [200, 400])

    assert set(variants) == {200, 400}
    for width, out in variants.items():
        image = Image.open(BytesIO(out))
        assert image.format == "WEBP"
        assert image.width == width


def test_widths_at_or_above_original_are_skipped():
    """Widths >= the source width are omitted (no upscaling)."""
    data = _jpeg_bytes(1000, 800)

    variants = generate_webp_variants(data, [500, 1000, 1500])

    assert set(variants) == {500}


def test_original_narrower_than_all_requested_returns_empty():
    """A source narrower than every request yields an empty mapping."""
    data = _jpeg_bytes(300, 200)

    variants = generate_webp_variants(data, [400, 800])

    assert variants == {}


def test_rgba_png_preserves_alpha_in_webp():
    """An RGBA source round-trips to a WEBP variant that keeps its alpha."""
    data = _rgba_png_bytes(1000, 800)

    variants = generate_webp_variants(data, [400])

    image = Image.open(BytesIO(variants[400]))
    assert image.format == "WEBP"
    assert "A" in image.mode


def test_non_image_bytes_raise_value_error():
    """Undecodable input surfaces as a clear ValueError."""
    with pytest.raises(ValueError):
        generate_webp_variants(b"not an image at all", [200])


def test_fixed_set_returns_every_requested_width():
    """Every requested width is present as a key, unlike the omitting variant."""
    data = _jpeg_bytes(2000, 1000)

    variants = generate_fixed_webp_set(data, [200, 400, 800, 1200])

    assert set(variants) == {200, 400, 800, 1200}
    for width, out in variants.items():
        image = Image.open(BytesIO(out))
        assert image.format == "WEBP"
        assert image.width == width


def test_fixed_set_large_original_yields_distinct_bytes():
    """A wide original encodes each requested width at its own effective width."""
    data = _jpeg_bytes(2000, 1000)

    variants = generate_fixed_webp_set(data, [200, 400, 800, 1200])

    byte_values = list(variants.values())
    assert len({id(b) for b in byte_values}) == len(byte_values)
    for width, out in variants.items():
        assert Image.open(BytesIO(out)).width == width


def test_fixed_set_caps_and_reuses_bytes_for_large_keys():
    """Widths above the source cap to it and share the identical bytes object."""
    data = _jpeg_bytes(700, 500)

    variants = generate_fixed_webp_set(data, [200, 400, 800, 1200])

    assert set(variants) == {200, 400, 800, 1200}
    # 800 and 1200 both cap to the 700px effective width → same reused bytes.
    assert variants[800] == variants[1200]
    assert variants[800] is variants[1200]
    assert Image.open(BytesIO(variants[800])).width == 700
    # 200 and 400 stay distinct (below the source width).
    assert variants[200] != variants[400]


def test_fixed_set_dedups_duplicate_requested_widths():
    """Repeated requested widths collapse to a single key."""
    data = _jpeg_bytes(1000, 800)

    variants = generate_fixed_webp_set(data, [400, 400, 200])

    assert set(variants) == {400, 200}


def test_fixed_set_preserves_alpha():
    """An RGBA source keeps its alpha channel across the produced widths."""
    data = _rgba_png_bytes(1000, 800)

    variants = generate_fixed_webp_set(data, [400, 800])

    for out in variants.values():
        image = Image.open(BytesIO(out))
        assert image.format == "WEBP"
        assert "A" in image.mode


def test_fixed_set_non_image_bytes_raise_value_error():
    """Undecodable input surfaces as a clear ValueError."""
    with pytest.raises(ValueError):
        generate_fixed_webp_set(b"not an image at all", [200])
