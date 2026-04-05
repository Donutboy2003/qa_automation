# utils/image_compressor.py
#
# Fetch an image from a URL (or accept raw bytes), compress it with Pillow,
# and return the result ready to send to an AI vision API.
#
# Why compress? OpenAI's vision API charges per token, and token cost scales
# with image size. Resizing and re-encoding a large image before sending it
# can cut cost by 70-90% with little to no impact on alt text quality.

from __future__ import annotations

import io
from dataclasses import dataclass

import requests
from PIL import Image
from utils.http_helpers import _get

# Defaults — override per-call if needed
DEFAULT_MAX_WIDTH  = 800
DEFAULT_MAX_HEIGHT = 800
DEFAULT_QUALITY    = 75    # JPEG quality 1-95; 75 is a good balance
DEFAULT_FORMAT     = "JPEG"


@dataclass
class CompressedImage:
    """
    Result of a compress operation.

    Attributes:
        data:            The compressed image as raw bytes
        mime_type:       e.g. "image/jpeg" or "image/png"
        original_size_kb: Size of the original image in KB
        compressed_size_kb: Size of the compressed image in KB
        original_dims:   (width, height) before resizing
        compressed_dims: (width, height) after resizing
    """
    data:                bytes
    mime_type:           str
    original_size_kb:    float
    compressed_size_kb:  float
    original_dims:       tuple[int, int]
    compressed_dims:     tuple[int, int]

    @property
    def size_reduction_pct(self) -> float:
        """How much smaller the compressed image is as a percentage."""
        if self.original_size_kb == 0:
            return 0.0
        return 100 * (1 - self.compressed_size_kb / self.original_size_kb)

    def summary(self) -> str:
        return (
            f"{self.original_dims[0]}x{self.original_dims[1]}px → "
            f"{self.compressed_dims[0]}x{self.compressed_dims[1]}px  |  "
            f"{self.original_size_kb:.1f} KB → {self.compressed_size_kb:.1f} KB  "
            f"({self.size_reduction_pct:.0f}% reduction)"
        )


def compress_image_bytes(
    raw_bytes: bytes,
    max_width:  int = DEFAULT_MAX_WIDTH,
    max_height: int = DEFAULT_MAX_HEIGHT,
    quality:    int = DEFAULT_QUALITY,
    fmt:        str = DEFAULT_FORMAT,
) -> CompressedImage:
    """
    Compress raw image bytes using Pillow.

    Resizes the image so neither dimension exceeds max_width/max_height
    (preserving aspect ratio), then re-encodes at the given quality.

    Args:
        raw_bytes:  The original image bytes (any format Pillow can read)
        max_width:  Max pixel width after resizing
        max_height: Max pixel height after resizing
        quality:    JPEG quality (1-95). Ignored for PNG (lossless).
        fmt:        Output format — "JPEG" or "PNG".
                    Use PNG for images with transparency, JPEG for everything else.

    Returns:
        CompressedImage with the compressed bytes and metadata.
    """
    img = Image.open(io.BytesIO(raw_bytes))
    original_dims = img.size

    # JPEG doesn't support transparency — convert to RGB if needed
    if fmt.upper() == "JPEG" and img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")

    # Resize — thumbnail() shrinks in-place and never upscales
    img.thumbnail((max_width, max_height), Image.LANCZOS)
    compressed_dims = img.size

    buf = io.BytesIO()
    save_kwargs: dict = {"format": fmt, "optimize": True}
    if fmt.upper() == "JPEG":
        save_kwargs["quality"] = quality
    img.save(buf, **save_kwargs)

    compressed_bytes = buf.getvalue()
    mime = "image/jpeg" if fmt.upper() == "JPEG" else "image/png"

    return CompressedImage(
        data=compressed_bytes,
        mime_type=mime,
        original_size_kb=len(raw_bytes) / 1024,
        compressed_size_kb=len(compressed_bytes) / 1024,
        original_dims=original_dims,
        compressed_dims=compressed_dims,
    )


def fetch_and_compress(
    url:        str,
    max_width:  int = DEFAULT_MAX_WIDTH,
    max_height: int = DEFAULT_MAX_HEIGHT,
    quality:    int = DEFAULT_QUALITY,
    fmt:        str = DEFAULT_FORMAT,
    timeout:    int = 15,
) -> CompressedImage:
    """
    Fetch an image from a URL and compress it in one step.

    Args:
        url:        The image URL to fetch
        max_width:  Max pixel width after resizing
        max_height: Max pixel height after resizing
        quality:    JPEG quality (1-95)
        fmt:        Output format — "JPEG" or "PNG"
        timeout:    HTTP request timeout in seconds

    Returns:
        CompressedImage with the compressed bytes and metadata.

    Raises:
        requests.HTTPError: if the image URL returns a non-200 status
        PIL.UnidentifiedImageError: if the URL doesn't point to a valid image
    """
    resp = _get(url, timeout=timeout)
    resp.raise_for_status()
    return compress_image_bytes(resp.content, max_width, max_height, quality, fmt)


def save_compressed(image: CompressedImage, path: str) -> None:
    """Save a CompressedImage to disk — handy for visually checking quality."""
    with open(path, "wb") as f:
        f.write(image.data)
