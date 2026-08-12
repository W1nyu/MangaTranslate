from __future__ import annotations

from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ocr import PaddleOcrEngine


class V3Result:
    json = {
        "res": {
            "rec_texts": ["Hello", "SEOUL"],
            "rec_scores": [0.98, 0.91],
            "rec_polys": [
                [[10, 20], [50, 20], [50, 36], [10, 36]],
                [[60, 45], [120, 45], [120, 63], [60, 63]],
            ],
        }
    }


class V3Reader:
    def predict(self, _image: np.ndarray):
        yield V3Result()


class OcrAdapterTests(unittest.TestCase):
    def test_v3_result_converts_polygons_to_overlay_regions(self) -> None:
        regions = PaddleOcrEngine()._recognize_v3(V3Reader(), np.zeros((100, 150, 3), dtype=np.uint8))
        self.assertEqual(len(regions), 2)
        self.assertEqual(regions[0]["text"], "Hello")
        self.assertEqual(regions[0]["x"], 10.0)
        self.assertEqual(regions[1]["width"], 60.0)

    def test_horizontal_reader_uses_bounded_detector_and_skips_orientation_model(self) -> None:
        calls: list[dict[str, object]] = []

        class FakePaddleOCR:
            def __init__(self, **kwargs: object) -> None:
                calls.append(kwargs)

        module = types.ModuleType("paddleocr")
        module.PaddleOCR = FakePaddleOCR
        with patch.dict(sys.modules, {"paddleocr": module}):
            PaddleOcrEngine()._reader_for("en")

        self.assertEqual(len(calls), 1)
        options = calls[0]
        self.assertFalse(options["enable_mkldnn"])
        self.assertEqual(options["cpu_threads"], 8)
        self.assertFalse(options["use_textline_orientation"])
        self.assertEqual(options["text_det_limit_side_len"], 960)
        self.assertEqual(options["text_det_limit_type"], "max")

    def test_cjk_reader_preserves_orientation_and_detector_defaults(self) -> None:
        calls: list[dict[str, object]] = []

        class FakePaddleOCR:
            def __init__(self, **kwargs: object) -> None:
                calls.append(kwargs)

        module = types.ModuleType("paddleocr")
        module.PaddleOCR = FakePaddleOCR
        with patch.dict(sys.modules, {"paddleocr": module}):
            PaddleOcrEngine()._reader_for("ko")

        self.assertEqual(len(calls), 1)
        options = calls[0]
        self.assertFalse(options["enable_mkldnn"])
        self.assertEqual(options["cpu_threads"], 8)
        self.assertTrue(options["use_textline_orientation"])
        self.assertNotIn("text_det_limit_side_len", options)
        self.assertNotIn("text_det_limit_type", options)

if __name__ == "__main__":
    unittest.main()
