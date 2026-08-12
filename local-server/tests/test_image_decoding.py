from __future__ import annotations

import base64
import unittest
from io import BytesIO

from fastapi import HTTPException
from PIL import Image

from app.main import MAX_IMAGE_BYTES, MAX_IMAGE_EDGE, decode_image


def data_url(image: Image.Image, image_format: str = "JPEG", **save_options: object) -> str:
    buffer = BytesIO()
    image.save(buffer, format=image_format, **save_options)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    mime = "jpeg" if image_format == "JPEG" else image_format.lower()
    return f"data:image/{mime};base64,{encoded}"


class DecodeImageTests(unittest.TestCase):
    """The extension captures JPEG screenshots; PNG callers must keep working."""

    def test_jpeg_data_url_decodes_to_rgb(self) -> None:
        decoded = decode_image(data_url(Image.new("RGB", (320, 200), "white"), quality=92))
        self.assertEqual(decoded.size, (320, 200))
        self.assertEqual(decoded.mode, "RGB")

    def test_png_data_url_still_decodes(self) -> None:
        decoded = decode_image(data_url(Image.new("RGB", (64, 48), "white"), "PNG"))
        self.assertEqual(decoded.size, (64, 48))

    def test_bare_base64_without_data_url_prefix_decodes(self) -> None:
        prefixed = data_url(Image.new("RGB", (32, 32), "white"))
        decoded = decode_image(prefixed.split(",", 1)[1])
        self.assertEqual(decoded.size, (32, 32))

    def test_grayscale_jpeg_is_converted_to_rgb(self) -> None:
        decoded = decode_image(data_url(Image.new("L", (48, 48), 128)))
        self.assertEqual(decoded.mode, "RGB")

    def test_non_base64_payload_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            decode_image("data:image/jpeg;base64,not-valid-base64!!")
        self.assertEqual(raised.exception.status_code, 400)

    def test_base64_that_is_not_an_image_is_rejected(self) -> None:
        encoded = base64.b64encode(b"this is plainly not an image").decode("ascii")
        with self.assertRaises(HTTPException) as raised:
            decode_image(f"data:image/jpeg;base64,{encoded}")
        self.assertEqual(raised.exception.status_code, 400)

    def test_oversized_payload_is_rejected_before_decoding(self) -> None:
        oversized = base64.b64encode(b"\x00" * (MAX_IMAGE_BYTES + 1)).decode("ascii")
        with self.assertRaises(HTTPException) as raised:
            decode_image(f"data:image/jpeg;base64,{oversized}")
        self.assertEqual(raised.exception.status_code, 413)

    def test_edge_limit_is_enforced_for_jpeg(self) -> None:
        # A single-colour JPEG this wide stays far below the byte limit, so this
        # asserts the dimension guard rather than the payload-size guard.
        wide = Image.new("RGB", (MAX_IMAGE_EDGE + 1, 8), "white")
        with self.assertRaises(HTTPException) as raised:
            decode_image(data_url(wide))
        self.assertEqual(raised.exception.status_code, 413)


if __name__ == "__main__":
    unittest.main()
