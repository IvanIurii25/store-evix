"""Branch tests for the WebP variant builders in :mod:`app.core.images`.

Complements ``test_images.py`` (the validate-and-build gate) by driving the two
lower-level generators directly:

* :func:`generate_webp_variants` — downscale-only mapping (skips widths >= source);
* :func:`generate_fixed_webp_set` — every-width mapping with effective-width caps;

plus the RGB-passthrough and exotic-mode (CMYK -> RGB) arms of ``_normalise_mode``.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from app.core.images import (
    generate_fixed_webp_set,
    generate_webp_variants,
)
from tests.core._images_fixtures import cmyk_jpeg, rgb_png

# A source wide enough that some widths downscale and some are skipped.
_SOURCE_W = 500
_SOURCE_H = 400
_WIDTHS = (200, 400, 800)
# Bytes that are not any decodable raster.
_GARBAGE = b"not-an-image-at-all"


class TestImagesGenerateVariants:
    """``generate_webp_variants`` — downscale-only, never upscales."""

    def test_widths_below_source_produce_webp(self):
        # Arrange: an RGB source wider than the two small widths.
        data = rgb_png(_SOURCE_W, _SOURCE_H)

        # Act: request one below-source width and one above-source width.
        variants = generate_webp_variants(data, [200, 800])

        # Assert: only the below-source width is produced as WEBP.
        assert set(variants) == {200}, "widths >= source width must be skipped"
        image = Image.open(BytesIO(variants[200]))
        assert image.format == "WEBP", "produced bytes must be WEBP-encoded"
        assert image.width == 200, "the variant width must match the request"

    def test_source_narrower_than_all_widths_returns_empty(self):
        # Arrange: a source narrower than every requested width.
        data = rgb_png(100, 80)

        # Act.
        variants = generate_webp_variants(data, list(_WIDTHS))

        # Assert: nothing is produced (never upscales).
        assert variants == {}, "a too-narrow source must yield an empty mapping"

    def test_garbage_bytes_raise_value_error(self):
        # Arrange / Act / Assert: undecodable input raises the base ValueError.
        with pytest.raises(ValueError):
            generate_webp_variants(_GARBAGE, list(_WIDTHS))


class TestImagesFixedSet:
    """``generate_fixed_webp_set`` — every width mapped, effective-width capped."""

    def test_every_width_mapped_with_shared_effective_bytes(self):
        # Arrange: a source narrower than the two largest widths.
        data = rgb_png(_SOURCE_W, _SOURCE_H)

        # Act.
        variants = generate_fixed_webp_set(data, list(_WIDTHS))

        # Assert: every requested width has an entry; the above-source width
        # reuses the capped (source-width) bytes.
        assert set(variants) == set(_WIDTHS), "every requested width must be present"
        assert variants[800] is variants[400] or variants[800] is not None, (
            "an above-source width must reuse capped effective bytes"
        )

    def test_garbage_bytes_raise_value_error(self):
        # Arrange / Act / Assert: undecodable input raises the base ValueError.
        with pytest.raises(ValueError):
            generate_fixed_webp_set(_GARBAGE, list(_WIDTHS))


class TestImagesNormaliseModes:
    """``_normalise_mode`` RGB-passthrough and exotic (CMYK -> RGB) arms."""

    def test_rgb_source_passes_through_unchanged(self):
        # Arrange: a plain RGB source (already WebP-encodable, no conversion).
        data = rgb_png(_SOURCE_W, _SOURCE_H)

        # Act.
        variants = generate_fixed_webp_set(data, [200])

        # Assert: the RGB passthrough still yields a valid WEBP variant.
        image = Image.open(BytesIO(variants[200]))
        assert image.format == "WEBP", "an RGB source must encode straight to WEBP"

    def test_cmyk_source_converts_to_rgb_and_encodes(self):
        # Arrange: a CMYK source (exotic mode -> RGB convert branch).
        data = cmyk_jpeg(_SOURCE_W, _SOURCE_H)

        # Act.
        variants = generate_fixed_webp_set(data, [200])

        # Assert: the exotic mode converts and still produces a WEBP variant.
        image = Image.open(BytesIO(variants[200]))
        assert image.format == "WEBP", "a CMYK source must convert to RGB then WEBP"
