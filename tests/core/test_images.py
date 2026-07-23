"""Branch tests for :func:`app.core.images.validate_and_build_variants`.

The happy path and width arithmetic are covered by ``tests/images``; this file
targets the remaining error/edge branches of the validate-and-build gate:

* palette (``P``) mode normalisation, with and without transparency;
* the decompression-bomb rejection (``DecompressionBombError`` -> validation);
* the re-wrap of a downstream ``ValueError`` into ``ImageValidationError``.
"""

from __future__ import annotations

import pytest

import app.core.images as images_module
from app.core.images import (
    MAX_UPLOAD_DIMENSION,
    RESPONSIVE_WIDTHS,
    ImageValidationError,
    validate_and_build_variants,
)
from tests.core._images_fixtures import palette_png, rgb_png

# A source smaller than every RESPONSIVE_WIDTHS so every variant is produced.
_SMALL_W = 60
_SMALL_H = 50
# A pixel budget below the small image's area, to force the bomb guard.
_TINY_PIXEL_BUDGET = 100
# An oversized dimension to trip the MAX_UPLOAD_DIMENSION ceiling header check.
_OVERSIZE = MAX_UPLOAD_DIMENSION + 1


class TestImagesPaletteNormalisation:
    """Palette (``P``) sources route through both ``_normalise_mode`` branches."""

    def test_palette_with_transparency_builds_full_variant_set(self):
        # Arrange: a palette PNG carrying transparency (P -> RGBA branch).
        data = palette_png(transparent=True)

        # Act.
        variants = validate_and_build_variants(data)

        # Assert: every responsive width is produced from the small source.
        assert set(variants) == set(RESPONSIVE_WIDTHS), (
            "a transparent palette image must still yield the full width set"
        )

    def test_palette_without_transparency_builds_full_variant_set(self):
        # Arrange: a palette PNG without transparency (P -> RGB branch).
        data = palette_png(transparent=False)

        # Act.
        variants = validate_and_build_variants(data)

        # Assert: every responsive width is produced.
        assert set(variants) == set(RESPONSIVE_WIDTHS), (
            "an opaque palette image must still yield the full width set"
        )


class TestImagesRejection:
    """The gate rejects non-rasters, bombs and oversized dimensions."""

    def test_garbage_bytes_raise_validation_error(self):
        # Arrange: bytes that are not any decodable raster.
        data = b"this is definitely not an image"

        # Act / Assert: undecodable input is rejected.
        with pytest.raises(ImageValidationError):
            validate_and_build_variants(data)

    def test_decompression_bomb_raises_validation_error(self, monkeypatch):
        # Arrange: a modest image + a pixel budget lower than its area, so the
        # header-only size read trips Pillow's decompression-bomb guard.
        data = rgb_png(_SMALL_W, _SMALL_H)
        monkeypatch.setattr(images_module.Image, "MAX_IMAGE_PIXELS", _TINY_PIXEL_BUDGET)

        # Act / Assert: the bomb guard surfaces as a validation error.
        with pytest.raises(ImageValidationError):
            validate_and_build_variants(data)

    def test_oversized_dimension_raises_validation_error(self):
        # Arrange: an image whose width exceeds the upload ceiling.
        data = rgb_png(_OVERSIZE, 10)

        # Act / Assert: the dimension ceiling rejects it.
        with pytest.raises(ImageValidationError):
            validate_and_build_variants(data)


class TestImagesDownstreamError:
    """A downstream ``ValueError`` re-wraps into ``ImageValidationError``."""

    def test_build_value_error_wrapped_as_validation_error(self, monkeypatch):
        # Arrange: a valid image (passes header + dimension checks), but make the
        # variant builder raise a plain ValueError to hit the re-wrap branch.
        data = rgb_png(_SMALL_W, _SMALL_H)

        def _boom(*_args, **_kwargs):
            raise ValueError("downstream decode failure")

        monkeypatch.setattr(images_module, "generate_fixed_webp_set", _boom)

        # Act / Assert: the ValueError is normalised to ImageValidationError.
        with pytest.raises(ImageValidationError):
            validate_and_build_variants(data)
