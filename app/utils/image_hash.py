"""Perceptual image hashing for replay detection.

We never retain verification photographs, so a plain checksum would be useless
(re-encoding changes every byte) and storing the image is off the table. A
difference hash captures enough structure to recognise the same scene
photographed twice, while being irreversible — the 64-bit hash cannot be turned
back into an image, so this adds no privacy exposure.
"""

import base64
import binascii
import io

from PIL import Image, UnidentifiedImageError

# Hash grid width; 8 produces a 64-bit hash (8 rows x 8 comparisons).
_HASH_SIZE = 8


class ImageDecodeError(Exception):
    """Submitted payload was not a decodable image."""


def decode_base64_image(image_base64: str) -> Image.Image:
    """Decode a base64 payload into an image, rejecting anything malformed."""
    # Tolerate a data-URI prefix, which mobile clients commonly include.
    if "," in image_base64[:64] and image_base64.lstrip().startswith("data:"):
        image_base64 = image_base64.split(",", 1)[1]

    try:
        raw = base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageDecodeError(f"Payload is not valid base64: {exc}") from exc

    if not raw:
        raise ImageDecodeError("Payload is empty")

    try:
        image = Image.open(io.BytesIO(raw))
        # Force decoding now so a truncated file fails here rather than later.
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageDecodeError(f"Payload is not a decodable image: {exc}") from exc

    return image


def difference_hash(image: Image.Image, hash_size: int = _HASH_SIZE) -> str:
    """Compute a dHash: compare each pixel to its right-hand neighbour.

    Resizing to a tiny grid discards colour, resolution and compression noise,
    leaving coarse structure. Two photographs of the same mat in the same spot
    differ in only a few bits; two genuinely different scenes differ in many.

    Known limitation: an image with no coarse structure — a blank wall, an
    almost-black frame — downsamples to a uniform grid and hashes to all zeros,
    so such images collide with each other. Replay detection therefore treats a
    match as a signal to flag, never as proof; the policy layer decides what to
    do about it.
    """
    resized = image.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    # tobytes() on an 8-bit greyscale image yields one byte per pixel in row
    # order, which is exactly the grid we need.
    pixels = list(resized.tobytes())

    bits: list[str] = []
    for row in range(hash_size):
        offset = row * (hash_size + 1)
        for col in range(hash_size):
            left = pixels[offset + col]
            right = pixels[offset + col + 1]
            bits.append("1" if left > right else "0")

    value = int("".join(bits), 2)
    # Fixed-width hex so hashes are directly comparable as strings.
    return f"{value:0{hash_size * hash_size // 4}x}"


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """Number of differing bits between two hashes of equal length."""
    if len(hash_a) != len(hash_b):
        raise ValueError("Cannot compare hashes of different lengths")
    return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")


def hash_base64_image(image_base64: str) -> str:
    """Convenience: decode a base64 payload and return its perceptual hash."""
    return difference_hash(decode_base64_image(image_base64))
