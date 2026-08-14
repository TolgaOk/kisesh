"""Validate the self-contained README branding assets."""

from __future__ import annotations

import base64
import struct
import unittest
import xml.etree.ElementTree as ElementTree
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).parents[1]
SVG_NAMESPACE = "http://www.w3.org/2000/svg"


def _png_chunks(data: bytes) -> Iterator[tuple[bytes, bytes]]:
    """Yield validated chunk types and payloads from one PNG file."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid PNG signature")
    offset = 8
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        payload_start = offset + 8
        payload_end = payload_start + length
        yield chunk_type, data[payload_start:payload_end]
        offset = payload_end + 4


def _png_dimensions(path: Path) -> tuple[int, int]:
    """Read dimensions from a validated PNG header without decoding its pixels."""
    for chunk_type, payload in _png_chunks(path.read_bytes()):
        if chunk_type == b"IHDR":
            return struct.unpack(">II", payload[:8])
    raise ValueError("PNG has no image header")


def _paeth(left: int, above: int, upper_left: int) -> int:
    """Return the PNG Paeth predictor nearest its linear estimate."""
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    return above if above_distance <= upper_left_distance else upper_left


def _unfilter(scanline: bytes, previous: bytes, filter_type: int) -> bytes:
    """Reverse one standard PNG scanline filter for RGBA pixels."""
    row = bytearray(scanline)
    for index, value in enumerate(row):
        left = row[index - 4] if index >= 4 else 0
        above = previous[index] if previous else 0
        upper_left = previous[index - 4] if previous and index >= 4 else 0
        match filter_type:
            case 0:
                predictor = 0
            case 1:
                predictor = left
            case 2:
                predictor = above
            case 3:
                predictor = (left + above) // 2
            case 4:
                predictor = _paeth(left, above, upper_left)
            case _:
                raise ValueError(f"unknown PNG filter: {filter_type}")
        row[index] = (value + predictor) & 0xFF
    return bytes(row)


@dataclass(frozen=True, slots=True)
class _RgbaImage:
    """Decoded eight-bit RGBA image used for exact asset assertions."""

    width: int
    height: int
    pixels: bytes

    def pixel(self, x: int, y: int) -> tuple[int, int, int, int]:
        """Return one pixel after validating its coordinates."""
        if not 0 <= x < self.width or not 0 <= y < self.height:
            raise IndexError((x, y))
        offset = (y * self.width + x) * 4
        red, green, blue, alpha = self.pixels[offset : offset + 4]
        return red, green, blue, alpha

    def colors(self, left: int, top: int, right: int, bottom: int) -> set[tuple[int, ...]]:
        """Return every color present inside a half-open rectangular region."""
        return {self.pixel(x, y) for y in range(top, bottom) for x in range(left, right)}


def _decode_rgba_png(data: bytes) -> _RgbaImage:
    """Decode the noninterlaced eight-bit RGBA subset used by the logo."""
    width = height = 0
    compressed = bytearray()
    for chunk_type, payload in _png_chunks(data):
        if chunk_type == b"IHDR":
            width, height, depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if (depth, color_type, compression, filtering, interlace) != (8, 6, 0, 0, 0):
                raise ValueError("unsupported PNG encoding")
        elif chunk_type == b"IDAT":
            compressed.extend(payload)
    stride = width * 4
    encoded = zlib.decompress(compressed)
    rows: list[bytes] = []
    offset = 0
    for _ in range(height):
        filter_type = encoded[offset]
        scanline = encoded[offset + 1 : offset + stride + 1]
        rows.append(_unfilter(scanline, rows[-1] if rows else b"", filter_type))
        offset += stride + 1
    if offset != len(encoded):
        raise ValueError("unexpected PNG scanline data")
    return _RgbaImage(width, height, b"".join(rows))


def _logo(variant: str) -> tuple[ElementTree.Element, _RgbaImage]:
    """Load one SVG and decode its self-contained raster base."""
    root = ElementTree.parse(PROJECT / "docs" / f"kisesh-logo-{variant}.svg").getroot()
    image = root.find(f"{{{SVG_NAMESPACE}}}image")
    if image is None:
        raise ValueError("logo has no raster base")
    media_type, separator, payload = image.attrib.get("href", "").partition(",")
    if media_type != "data:image/png;base64" or not separator:
        raise ValueError("logo raster is not self-contained")
    return root, _decode_rgba_png(base64.b64decode(payload, validate=True))


class BrandingAssetTests(unittest.TestCase):
    def test_readme_uses_two_x_theme_specific_session_screenshots(self) -> None:
        readme = (PROJECT / "README.md").read_text(encoding="utf-8")
        self.assertIn('srcset="docs/kisesh-dark.png"', readme)
        self.assertIn('srcset="docs/kisesh-light.png"', readme)
        self.assertIn(
            '<img alt="KiSesh session manager" src="docs/kisesh-light.png" width="650">',
            readme,
        )
        for variant in ("light", "dark"):
            with self.subTest(variant=variant):
                self.assertEqual(
                    _png_dimensions(PROJECT / "docs" / f"kisesh-{variant}.png"),
                    (1300, 816),
                )
        self.assertFalse((PROJECT / "docs" / "kisesh.png").exists())

    def test_readme_uses_theme_specific_self_contained_svg_animation(self) -> None:
        readme = (PROJECT / "README.md").read_text(encoding="utf-8")
        self.assertIn('media="(prefers-color-scheme: dark)"', readme)
        self.assertIn('srcset="docs/kisesh-logo-dark.svg"', readme)
        self.assertIn('srcset="docs/kisesh-logo-light.svg"', readme)
        self.assertFalse((PROJECT / "docs" / "kisesh-logo-light.gif").exists())
        self.assertFalse((PROJECT / "docs" / "kisesh-logo-dark.gif").exists())

        for variant in ("light", "dark"):
            with self.subTest(variant=variant):
                root, _ = _logo(variant)
                style = root.findtext(f"{{{SVG_NAMESPACE}}}style", default="")
                cursor = root.find(f"{{{SVG_NAMESPACE}}}rect[@class='cursor']")
                self.assertIn("animation: kisesh-blink 1.1s step-end infinite", style)
                self.assertIn("50%, 100% { opacity: 0; }", style)
                self.assertIsNotNone(cursor)

    def test_logo_layers_keep_raster_backgrounds_clear_for_vector_boundaries(self) -> None:
        expected_backgrounds = {
            "light": (255, 255, 255, 255),
            "dark": (13, 13, 18, 255),
        }
        expected_paths = (
            "M497 209H756L770 216 756 223H497A7 7 0 0 1 497 209Z",
            "M756 209H886L900 216 886 223H756L770 216Z",
        )
        for variant, background in expected_backgrounds.items():
            with self.subTest(variant=variant):
                root, raster = _logo(variant)
                group = root.find(f"{{{SVG_NAMESPACE}}}g")
                if group is None:
                    self.fail("logo has no native boundary group")
                paths = tuple(
                    element.attrib["d"] for element in group.findall(f"{{{SVG_NAMESPACE}}}path")
                )
                self.assertEqual(paths, expected_paths)
                self.assertEqual(raster.pixel(0, 0), background)
                self.assertEqual(raster.colors(1580, 120, 1740, 380), {background})
                self.assertEqual(raster.colors(590, 410, 1810, 452), {background})


if __name__ == "__main__":
    unittest.main()
