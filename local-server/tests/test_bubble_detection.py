from __future__ import annotations

from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bubble import analyze_bubble, partition_regions


IMAGE_W, IMAGE_H = 600, 500
ARTWORK = 90
FILL = 245
OUTLINE = 20


def canvas(background: int = ARTWORK) -> np.ndarray:
    return np.full((IMAGE_H, IMAGE_W), background, dtype=np.uint8)


def draw_bubble(image: np.ndarray, left: int, top: int, right: int, bottom: int) -> None:
    """A light fill with a dark outline, clipped to the image."""
    l, t = max(0, left), max(0, top)
    r, b = min(image.shape[1], right), min(image.shape[0], bottom)
    if r <= l or b <= t:
        return
    image[t:b, l:r] = FILL
    if left >= 0:
        image[t:b, l:min(l + 3, r)] = OUTLINE if left == l else FILL
    if right <= image.shape[1]:
        image[t:b, max(l, r - 3):r] = OUTLINE if right == r else FILL
    if top >= 0:
        image[t:min(t + 3, b), l:r] = OUTLINE if top == t else FILL
    if bottom <= image.shape[0]:
        image[max(t, b - 3):b, l:r] = OUTLINE if bottom == b else FILL


def draw_text(image: np.ndarray, left: int, top: int, right: int, bottom: int) -> None:
    """Dark glyph-like strokes, leaving fill visible between them."""
    for y in range(top, bottom, 6):
        image[y:y + 3, left:right:2] = 30


def region(left: int, top: int, right: int, bottom: int) -> dict[str, float]:
    return {"x": float(left), "y": float(top), "width": float(right - left), "height": float(bottom - top)}


class BubbleAnalysisTests(unittest.TestCase):
    def test_text_in_a_fully_visible_bubble_is_complete(self) -> None:
        image = canvas()
        draw_bubble(image, 120, 100, 460, 300)
        draw_text(image, 160, 150, 420, 250)

        self.assertEqual(analyze_bubble(image, region(160, 150, 420, 250)).status, "complete")

    def test_a_bubble_running_off_the_bottom_is_clipped(self) -> None:
        image = canvas()
        # The fill continues past the bottom of the capture.
        draw_bubble(image, 120, 320, 460, IMAGE_H + 200)
        draw_text(image, 160, 380, 420, 460)

        analysis = analyze_bubble(image, region(160, 380, 420, 460))
        self.assertEqual(analysis.status, "clipped")
        self.assertIn("bottom", analysis.edges)

    def test_a_bubble_running_off_the_top_is_clipped(self) -> None:
        image = canvas()
        draw_bubble(image, 120, -200, 460, 180)
        draw_text(image, 160, 40, 420, 120)

        analysis = analyze_bubble(image, region(160, 40, 420, 120))
        self.assertEqual(analysis.status, "clipped")
        self.assertIn("top", analysis.edges)

    def test_a_bubble_running_off_the_side_is_clipped(self) -> None:
        image = canvas()
        draw_bubble(image, -150, 100, 300, 320)
        draw_text(image, 40, 160, 260, 260)

        analysis = analyze_bubble(image, region(40, 160, 260, 260))
        self.assertEqual(analysis.status, "clipped")
        self.assertIn("left", analysis.edges)

    def test_text_whose_own_box_leaves_the_capture_is_clipped(self) -> None:
        image = canvas()
        draw_bubble(image, 100, 100, 460, 320)
        draw_text(image, 160, 150, 420, 250)

        analysis = analyze_bubble(image, region(0, 150, 420, 250))
        self.assertEqual(analysis.status, "clipped")
        self.assertIn("left", analysis.edges)

    def test_lettering_on_dark_artwork_has_no_bubble(self) -> None:
        image = canvas()
        # A sound effect drawn straight onto the art: no light fill around it.
        draw_text(image, 160, 150, 420, 250)

        self.assertEqual(analyze_bubble(image, region(160, 150, 420, 250)).status, "absent")

    def test_a_narrow_caption_box_is_still_complete(self) -> None:
        image = canvas()
        draw_bubble(image, 60, 40, 540, 110)
        draw_text(image, 90, 60, 510, 92)

        self.assertEqual(analyze_bubble(image, region(90, 60, 510, 92)).status, "complete")

    def test_two_bubbles_do_not_interfere(self) -> None:
        image = canvas()
        draw_bubble(image, 40, 60, 260, 200)
        draw_text(image, 70, 90, 230, 170)
        draw_bubble(image, 330, 260, 560, 420)
        draw_text(image, 360, 290, 530, 390)

        self.assertEqual(analyze_bubble(image, region(70, 90, 230, 170)).status, "complete")
        self.assertEqual(analyze_bubble(image, region(360, 290, 530, 390)).status, "complete")

    def test_a_light_but_textured_bubble_is_still_complete(self) -> None:
        rng = np.random.default_rng(3)
        image = canvas()
        draw_bubble(image, 120, 100, 460, 300)
        # Paper texture and JPEG ringing must not end the search.
        noise = rng.integers(-12, 13, size=image.shape, dtype=np.int16)
        image = np.clip(image.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        draw_text(image, 160, 150, 420, 250)

        self.assertEqual(analyze_bubble(image, region(160, 150, 420, 250)).status, "complete")

    def test_a_degenerate_region_is_left_alone(self) -> None:
        image = canvas()
        self.assertEqual(analyze_bubble(image, region(10, 10, 11, 11)).status, "complete")
        self.assertEqual(analyze_bubble(image, {"x": "?", "y": 0, "width": 5, "height": 5}).status, "complete")


class PartitionRegionsTests(unittest.TestCase):
    def test_regions_are_split_and_clipped_geometry_is_reported(self) -> None:
        image = canvas()
        draw_bubble(image, 40, 60, 300, 220)
        draw_text(image, 70, 90, 270, 190)
        draw_bubble(image, 330, 380, 560, IMAGE_H + 120)
        draw_text(image, 360, 410, 530, 470)
        draw_text(image, 60, 300, 280, 350)  # bare artwork lettering

        good = region(70, 90, 270, 190)
        cut = region(360, 410, 530, 470)
        bare = region(60, 300, 280, 350)
        usable, clipped, absent = partition_regions(image, [good, cut, bare])

        self.assertEqual(usable, [good])
        self.assertEqual(absent, 1)
        self.assertEqual(len(clipped), 1)
        self.assertEqual(clipped[0]["y"], 410.0)
        self.assertIn("bottom", clipped[0]["edges"])

    def test_an_empty_input_is_handled(self) -> None:
        self.assertEqual(partition_regions(canvas(), []), ([], [], 0))


if __name__ == "__main__":
    unittest.main()
