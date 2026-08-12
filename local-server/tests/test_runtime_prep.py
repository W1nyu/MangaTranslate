from __future__ import annotations

from pathlib import Path
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ocr import PaddleOcrEngine
from app.translation import TranslationService


class FakeTranslation:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def translate(self, text: str) -> str:
        self.inputs.append(text)
        return text


class RuntimePreparationTests(unittest.TestCase):
    def test_argos_is_forced_to_the_offline_runtime(self) -> None:
        self.assertEqual(os.environ["ARGOS_CHUNK_TYPE"], "MINISBD")
        self.assertEqual(os.environ["ARGOS_DEVICE_TYPE"], "cpu")
        self.assertEqual(os.environ["ARGOS_MODEL_PROVIDER"], "OPENNMT")

    def test_translation_warmup_primes_the_selected_model(self) -> None:
        translator = FakeTranslation()
        service = TranslationService()
        with patch.object(TranslationService, "_load_translation", return_value=translator):
            service.warmup("en", "ko")
        self.assertEqual(translator.inputs, ["."])

    def test_ocr_warmup_runs_a_small_inference(self) -> None:
        engine = PaddleOcrEngine()
        with patch.object(engine, "recognize", return_value=[]) as recognize:
            engine.warmup("en")
        image, language = recognize.call_args.args
        self.assertEqual(image.size, (64, 64))
        self.assertEqual(language, "en")


if __name__ == "__main__":
    unittest.main()
