from __future__ import annotations

import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ocr import (
    CJK_TEXT_DETECTION_LIMIT_SIDE_LEN,
    FAST_TEXT_DETECTION_LIMIT_SIDE_LEN,
    TEXT_DETECTION_BOX_THRESHOLD,
    TEXT_DETECTION_THRESHOLD,
    PaddleOcrEngine,
    normalize_ocr_text_for_translation,
)


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
        calls = _build_readers("en")

        self.assertEqual(len(calls), 1)
        options = calls[0]
        self.assertEqual(options["engine"], "onnxruntime")
        self.assertEqual(options["engine_config"]["intra_op_num_threads"], 8)
        self.assertFalse(options["use_textline_orientation"])
        self.assertEqual(options["text_det_limit_side_len"], FAST_TEXT_DETECTION_LIMIT_SIDE_LEN)
        self.assertEqual(options["text_det_limit_type"], "max")
        self.assertEqual(options["text_det_thresh"], TEXT_DETECTION_THRESHOLD)
        self.assertEqual(options["text_det_box_thresh"], TEXT_DETECTION_BOX_THRESHOLD)

    def test_cjk_reader_preserves_orientation_with_a_bounded_detector(self) -> None:
        calls = _build_readers("ko")

        self.assertEqual(len(calls), 1)
        options = calls[0]
        self.assertEqual(options["engine"], "onnxruntime")
        self.assertTrue(options["use_textline_orientation"])
        self.assertEqual(options["text_det_limit_side_len"], CJK_TEXT_DETECTION_LIMIT_SIDE_LEN)
        self.assertEqual(options["text_det_limit_type"], "max")
        self.assertEqual(options["text_det_thresh"], TEXT_DETECTION_THRESHOLD)
        self.assertEqual(options["text_det_box_thresh"], TEXT_DETECTION_BOX_THRESHOLD)

    def test_reader_falls_back_to_paddle_when_onnx_cannot_load(self) -> None:
        engine = PaddleOcrEngine()
        calls = _build_readers("en", onnx_fails=True, engine=engine)

        self.assertEqual([options.get("engine") for options in calls], ["onnxruntime", None])
        paddle_options = calls[1]
        self.assertFalse(paddle_options["enable_mkldnn"])
        self.assertEqual(paddle_options["cpu_threads"], 8)
        self.assertEqual(paddle_options["text_det_limit_side_len"], FAST_TEXT_DETECTION_LIMIT_SIDE_LEN)
        self.assertEqual(engine.loaded_engines(), {"en": "paddle"})

    def test_paddle_engine_can_be_forced_from_the_environment(self) -> None:
        calls = _build_readers("en", preference="paddle")

        self.assertEqual(len(calls), 1)
        self.assertNotIn("engine", calls[0])
        self.assertFalse(calls[0]["enable_mkldnn"])

    def test_recognition_switches_to_paddle_when_onnx_fails_on_a_screen(self) -> None:
        engine = PaddleOcrEngine()
        engine._readers["en"] = FailingReader()
        engine._engines["en"] = "onnxruntime"
        built: list[dict[str, object]] = []

        class FakePaddleOCR(V3Reader):
            def __init__(self, **kwargs: object) -> None:
                built.append(kwargs)

        module = types.ModuleType("paddleocr")
        module.PaddleOCR = FakePaddleOCR
        with patch.dict(sys.modules, {"paddleocr": module}), patch.dict(os.environ, {"OCR_ENGINE": "auto"}):
            regions = engine.recognize(Image.new("RGB", (150, 100), "white"), "en")

        self.assertEqual([region["text"] for region in regions], ["Hello", "SEOUL"])
        self.assertNotIn("engine", built[0])
        self.assertEqual(engine.loaded_engines(), {"en": "paddle"})


class FailingReader:
    def predict(self, _image: np.ndarray):
        raise RuntimeError("onnx shape error")
        yield  # pragma: no cover


def _build_readers(
    language: str,
    *,
    onnx_fails: bool = False,
    preference: str = "auto",
    engine: PaddleOcrEngine | None = None,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    class FakePaddleOCR:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)
            if onnx_fails and kwargs.get("engine") == "onnxruntime":
                raise RuntimeError("model download failed")

        def predict(self, _image: np.ndarray):
            return iter(())

    module = types.ModuleType("paddleocr")
    module.PaddleOCR = FakePaddleOCR
    with patch.dict(sys.modules, {"paddleocr": module}), patch.dict(os.environ, {"OCR_ENGINE": preference}):
        (engine or PaddleOcrEngine())._reader_for(language)
    return calls


class OcrTextRepairTests(unittest.TestCase):
    def test_misread_letters_inside_words_are_repaired_for_translation(self) -> None:
        cases = {
            "YOU WERE G0NE FOR SO L0NG.": "YOU WERE GONE FOR SO LONG.",
            "1'M FINE. 1T'S OKAY.": "I'M FINE. IT'S OKAY.",
            "| KNOW WHAT 1 SAW.": "I KNOW WHAT I SAW.",
            "N0 WAY! HELL0?": "NO WAY! HELLO?",
            "he1lo there": "hello there",
            "0KAY, LET'S G0!": "OKAY, LET'S GO!",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_ocr_text_for_translation(raw, "en"), expected)

    def test_real_numbers_and_labels_are_left_alone(self) -> None:
        for text in ("TAKE 1", "1ST PLACE", "LV10 HUNTER", "10K GOLD", "THE 1990S", "PS1", "1 MORE TIME", "COVID19", "B1 FLOOR"):
            with self.subTest(text=text):
                self.assertEqual(normalize_ocr_text_for_translation(text, "en"), text)

    def test_non_latin_sources_are_untouched(self) -> None:
        self.assertEqual(normalize_ocr_text_for_translation("1층으로 G0", "ko"), "1층으로 G0")

if __name__ == "__main__":
    unittest.main()
