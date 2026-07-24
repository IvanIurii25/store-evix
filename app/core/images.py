"""Pure image resizing helper: original bytes → WebP variants (image-opt step 1).

The product/media pipeline serves responsive images from a single uploaded
original. This module owns the *pure, CPU-bound* transform: decode an original
raster (JPEG/PNG/…), downscale it to one or more target widths, and re-encode
each as WebP.

Deliberately free of I/O, DB, HTTP and settings — it takes and returns bytes so
the async call sites can offload it via :func:`asyncio.to_thread` without any
shared state. Only *downscaling* is performed: a requested width that is not
strictly smaller than the source is skipped (never upscale), so the returned
mapping may be smaller than ``widths`` (empty when the source is narrower than
every request — the caller then falls back to the original).

Alpha is preserved, never flattened onto a background: images with transparency
encode as RGBA WebP; opaque ones as RGB. Encoding strips metadata (a fresh WebP
carries none of the source EXIF/ICC) and uses ``method=6`` for better
compression at the given ``quality``.
"""

from collections.abc import Sequence
from io import BytesIO

from PIL import Image, UnidentifiedImageError

# Fixed responsive width set — the single source of truth for the WebP variant
# set produced by both the upload path and the backfill script. One WebP variant
# is generated per width per original.
RESPONSIVE_WIDTHS: tuple[int, ...] = (200, 400, 800, 1200)

# Hard ceiling on a source image's width/height (px). Rejects absurd / hostile
# uploads before the expensive resize; Pillow's own ``MAX_IMAGE_PIXELS`` bomb
# guard stays in force on top of this (never disabled).
MAX_UPLOAD_DIMENSION: int = 6000

# Modes carrying an alpha channel — encoded straight through as WebP RGBA.
_ALPHA_MODES: frozenset[str] = frozenset({"RGBA", "LA"})


class ImageValidationError(ValueError):
    """Raised when uploaded bytes are not an acceptable raster image.

    Covers both undecodable / non-raster input (SVG, PDF, garbage) and rasters
    whose dimensions exceed :data:`MAX_UPLOAD_DIMENSION`. Subclasses
    :class:`ValueError` so existing ``except ValueError`` sites keep working.
    """


def _normalise_mode(image: Image.Image) -> Image.Image:
    """Return an image in a WebP-encodable mode, preserving alpha when present.

    WebP accepts ``RGB``/``RGBA``, so every other mode is converted:

    * palette (``P``) images promote to ``RGBA`` when they carry transparency,
      else to ``RGB``;
    * ``CMYK`` (and any remaining exotic mode) converts to ``RGB``;
    * ``RGBA``/``LA`` and ``RGB``/``L`` are already fine and pass through.

    Transparency is never flattened onto a background.

    Args:
        image: The decoded source image.

    Returns:
        Image.Image: The same image if already encodable, otherwise a converted
        copy in ``RGB`` or ``RGBA``.
    """
    mode = image.mode
    if mode in _ALPHA_MODES or mode in {"RGB", "L"}:
        return image
    if mode == "P":
        target = "RGBA" if image.info.get("transparency") is not None else "RGB"
        return image.convert(target)
    return image.convert("RGB")


def _resized_webp(image: Image.Image, width: int, quality: int) -> bytes:
    """Downscale ``image`` to ``width`` (aspect-preserving) and encode as WebP.

    Args:
        image: The (already mode-normalised) source image.
        width: The target width in pixels; the height scales to keep the aspect
            ratio (at least one pixel).
        quality: The WebP quality (0–100).

    Returns:
        bytes: The WebP-encoded, metadata-free variant.
    """
    height = max(1, round(image.height * width / image.width))
    resized = image.resize((width, height), Image.Resampling.LANCZOS)
    try:
        buffer = BytesIO()
        resized.save(buffer, format="WEBP", quality=quality, method=6)
        return buffer.getvalue()
    finally:
        resized.close()


def generate_webp_variants(
    data: bytes, widths: Sequence[int], *, quality: int = 80
) -> dict[int, bytes]:
    """Produce downscaled WebP variants of an original image.

    Decodes ``data`` and, for each requested width strictly smaller than the
    source width, emits an aspect-preserving WebP (LANCZOS resample). Widths at
    or above the source width are skipped — the image is never upscaled — so the
    result contains only the widths actually produced and is empty when the
    source is narrower than every requested width.

    Args:
        data: The original raster image bytes (e.g. JPEG or PNG).
        widths: The candidate target widths in pixels.
        quality: The WebP encoding quality (0–100), default ``80``.

    Returns:
        dict[int, bytes]: A mapping of each produced width to its WebP bytes,
        omitting any skipped (too-large) width.

    Raises:
        ValueError: If ``data`` is not a decodable image.
    """
    try:
        with Image.open(BytesIO(data)) as opened:
            opened.load()
            source = _normalise_mode(opened)
            original_width = source.width
            variants: dict[int, bytes] = {}
            for width in widths:
                if width < original_width and width not in variants:
                    variants[width] = _resized_webp(source, width, quality)
            return variants
    except UnidentifiedImageError as exc:
        raise ValueError("data is not a decodable image") from exc


def validate_and_build_variants(data: bytes) -> dict[int, bytes]:
    """Validate raw upload bytes and produce the responsive WebP variant set.

    The single "converter + gate" used by both upload paths: it decodes ``data``
    to confirm it is a genuine raster (rejecting SVG/PDF/garbage), enforces the
    :data:`MAX_UPLOAD_DIMENSION` ceiling on either dimension, and — when valid —
    returns the same fixed :data:`RESPONSIVE_WIDTHS` WebP set the backfill
    produces via :func:`generate_fixed_webp_set`.

    The dimension check reads only the header via :meth:`PIL.Image.Image.size`
    (no full decode), so an oversized image is rejected before the costly
    resize. Pillow's ``MAX_IMAGE_PIXELS`` decompression-bomb guard is left at its
    default and is *not* disabled.

    This function is synchronous and CPU-bound like the rest of this module;
    async callers MUST wrap it in :func:`asyncio.to_thread`.

    Args:
        data: The raw uploaded file bytes.

    Returns:
        dict[int, bytes]: A mapping of every :data:`RESPONSIVE_WIDTHS` width to
        its WebP bytes (widths sharing an effective width share the same bytes).

    Raises:
        ImageValidationError: If ``data`` is not a decodable raster, or if its
            width or height exceeds :data:`MAX_UPLOAD_DIMENSION`.
    """
    try:
        with Image.open(BytesIO(data)) as opened:
            width, height = opened.size
    except UnidentifiedImageError as exc:
        raise ImageValidationError("data is not a decodable image") from exc
    except Image.DecompressionBombError as exc:
        raise ImageValidationError("image exceeds the pixel-bomb limit") from exc

    if width > MAX_UPLOAD_DIMENSION or height > MAX_UPLOAD_DIMENSION:
        raise ImageValidationError(
            f"image dimension exceeds {MAX_UPLOAD_DIMENSION}px (got {width}x{height})"
        )

    try:
        return generate_fixed_webp_set(data, RESPONSIVE_WIDTHS)
    except ValueError as exc:
        raise ImageValidationError(str(exc)) from exc


def generate_fixed_webp_set(
    data: bytes, widths: Sequence[int], *, quality: int = 80
) -> dict[int, bytes]:
    """Produce a WebP variant for *every* requested width (never up-scaling).

    Unlike :func:`generate_webp_variants` — which *omits* any width at or above
    the source width — this returns an entry for **every** requested width, so
    downstream ``srcset`` URLs built from ``widths`` never 404. A requested width
    larger than the source is capped to the source width (its "effective" width),
    and several requested widths that cap to the same effective width share the
    identical encoded bytes.

    Encoding happens once per *distinct effective width*: e.g. a 700px original
    with ``widths=(200, 400, 800, 1200)`` encodes at 200, 400 and 700, then keys
    ``800`` and ``1200`` both reuse the 700px bytes. Duplicate requested widths
    collapse to a single key.

    Args:
        data: The original raster image bytes (e.g. JPEG or PNG).
        widths: The requested target widths in pixels.
        quality: The WebP encoding quality (0–100), default ``80``.

    Returns:
        dict[int, bytes]: A mapping of *every* requested width to its WebP bytes;
        widths sharing an effective width map to the same bytes object.

    Raises:
        ValueError: If ``data`` is not a decodable image.
    """
    try:
        with Image.open(BytesIO(data)) as opened:
            opened.load()
            source = _normalise_mode(opened)
            original_width = source.width
            encoded: dict[int, bytes] = {}
            variants: dict[int, bytes] = {}
            for width in dict.fromkeys(widths):
                effective = min(width, original_width)
                if effective not in encoded:
                    encoded[effective] = _resized_webp(source, effective, quality)
                variants[width] = encoded[effective]
            return variants
    except UnidentifiedImageError as exc:
        raise ValueError("data is not a decodable image") from exc
