from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import translate_image
from app.models import TranslateImageRequest
from app.ocr import group_ocr_regions, is_probably_source_language
from app.translation import ProviderSelection, ProviderTranslationResult


def region(
    text: str,
    x: float,
    y: float,
    width: float = 100,
    height: float = 18,
    confidence: float = 0.95,
) -> dict[str, float | str]:
    return {
        "text": text,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "confidence": confidence,
    }


class OcrGroupingTests(unittest.TestCase):
    def test_wrapped_lines_merge_to_one_bubble_with_union_geometry(self) -> None:
        grouped = group_ocr_regions(
            [
                region("Don't worry.", 100, 20, 120, 18, 0.98),
                region("Everything will", 108, 43, 104, 18, 0.94),
                region("be fine.", 119, 66, 80, 18, 0.90),
            ]
        )

        self.assertEqual(len(grouped), 1)
        bubble = grouped[0]
        self.assertEqual(bubble["text"], "Don't worry. Everything will be fine.")
        self.assertEqual((bubble["x"], bubble["y"], bubble["width"], bubble["height"]), (100.0, 20.0, 120.0, 64.0))
        expected_confidence = (0.98 * 120 * 18 + 0.94 * 104 * 18 + 0.90 * 80 * 18) / ((120 + 104 + 80) * 18)
        self.assertAlmostEqual(float(bubble["confidence"]), expected_confidence)

    def test_nearby_bubbles_stay_separate_and_results_have_reading_order(self) -> None:
        grouped = group_ocr_regions(
            [
                # Deliberately shuffled to prove that output does not depend on the
                # order PaddleOCR happened to return.
                region("Right bottom", 220, 35, 90),
                region("Left bottom", 20, 34, 90),
                region("Right top", 220, 12, 90),
                region("Left top", 20, 10, 90),
                # Same horizontal position as the left bubble but too far away to
                # be treated as another wrapped line.
                region("A separate bubble", 20, 88, 140),
            ]
        )

        self.assertEqual([item["text"] for item in grouped], ["Left top Left bottom", "Right top Right bottom", "A separate bubble"])
        self.assertEqual([(item["x"], item["y"]) for item in grouped], [(20.0, 10.0), (220.0, 12.0), (20.0, 88.0)])

    def test_same_baseline_fragment_stays_in_its_wrapped_speech_bubble(self) -> None:
        """A detector can isolate the final bold word of one printed line.

        The original lettering is:
            I can unlock
            the entrance to the
            ancient ruin in the
            southern sea!

        Paddle sometimes returns the second line as ``THE ENTRANCE TO`` and a
        separate adjacent ``THE`` polygon.  It must still become one complete
        translation target, in visual reading order.
        """
        grouped = group_ocr_regions(
            [
                # Deliberately shuffled; Paddle result order is not stable.
                region("SOUTHERN SEA!", 122, 81, 154),
                region("THE", 238, 35, 31),
                region("I CAN UNLOCK", 137, 12, 132),
                region("ANCIENT RUIN IN THE", 109, 58, 193),
                region("THE ENTRANCE TO", 104, 35, 127),
            ]
        )

        self.assertEqual(len(grouped), 1)
        bubble = grouped[0]
        self.assertEqual(
            bubble["text"],
            "I CAN UNLOCK THE ENTRANCE TO THE ANCIENT RUIN IN THE SOUTHERN SEA!",
        )
        self.assertEqual((bubble["x"], bubble["y"], bubble["width"], bubble["height"]), (104.0, 12.0, 198.0, 87.0))

    def test_offset_middle_row_is_rejoined_after_greedy_stacker_skips_it(self) -> None:
        """Recover an entire middle row, not merely a same-baseline fragment.

        Here the top and third rows align, so a one-pass stacker joins them.
        The visually middle row is far to the right and its polygon slightly
        overlaps the third row vertically, so it cannot directly join either
        in that pass.  Once the enclosing rows form one group, it is safe to
        insert the skipped row using its close horizontal edge relationship.
        """
        grouped = group_ocr_regions(
            [
                region("SOUTHERN SEA!", 110, 68, 140),
                region("THE", 381, 33, 31),
                region("I CAN UNLOCK", 100, 10, 100),
                region("ANCIENT RUIN IN THE", 100, 45, 150),
                region("THE ENTRANCE TO", 250, 33, 127),
            ]
        )

        self.assertEqual(len(grouped), 1)
        bubble = grouped[0]
        self.assertEqual(
            bubble["text"],
            "I CAN UNLOCK THE ENTRANCE TO THE ANCIENT RUIN IN THE SOUTHERN SEA!",
        )
        self.assertEqual((bubble["x"], bubble["y"], bubble["width"], bubble["height"]), (100.0, 10.0, 312.0, 76.0))

    def test_interleaved_row_from_a_nearby_bubble_stays_separate_when_offset(self) -> None:
        """Do not absorb a side caption merely because it is between two rows."""
        grouped = group_ocr_regions(
            [
                region("Top of left bubble", 20, 10, 140),
                region("Side caption", 330, 32, 100),
                region("Bottom of left bubble", 20, 54, 150),
            ]
        )

        self.assertEqual(
            [item["text"] for item in grouped],
            ["Top of left bubble Bottom of left bubble", "Side caption"],
        )


class OcrSourceLanguageFilterTests(unittest.TestCase):
    def test_rejects_clear_script_mismatches_but_keeps_short_ambiguous_text(self) -> None:
        self.assertFalse(is_probably_source_language("안녕하세요", "en"))
        self.assertFalse(is_probably_source_language("Привет мир", "en"))
        self.assertFalse(is_probably_source_language("こんにちは", "en"))
        self.assertTrue(is_probably_source_language("No", "ko"))

    def test_conservative_latin_hints_reject_only_clear_other_languages(self) -> None:
        self.assertFalse(is_probably_source_language("Hola, ¿cómo estás?", "en"))
        self.assertFalse(is_probably_source_language("Hello, this is a test.", "es"))
        self.assertFalse(is_probably_source_language("Hello, this is a test.", "ko"))
        self.assertTrue(is_probably_source_language("Bonjour", "en"))

    def test_cjk_keeps_han_only_text_when_japanese_chinese_are_ambiguous(self) -> None:
        self.assertFalse(is_probably_source_language("これは日本語です", "zh"))
        self.assertTrue(is_probably_source_language("你好世界", "ja"))


class TranslateImageGroupingTests(unittest.TestCase):
    def test_response_exposes_stage_timings_and_the_slowest_stage(self) -> None:
        image = Image.new("RGB", (8, 8), "white")
        encoded = BytesIO()
        image.save(encoded, format="PNG")
        request = TranslateImageRequest(
            image=f"data:image/png;base64,{base64.b64encode(encoded.getvalue()).decode('ascii')}",
            cacheScope="https://comic.test/chapter/395",
            sourceLanguage="en",
            targetLanguage="ko",
        )
        recognized = [region("Hello there.", 10, 10)]

        # Tick pairs bracket each pipeline stage. The final tick belongs to
        # totalMs, which starts when the endpoint begins processing.
        clock_ticks = [
            0.0,
            1.0,
            1.012,
            2.0,
            2.450,
            3.0,
            3.003,
            4.0,
            4.006,
            5.0,
            5.002,
            6.0,
            6.001,
            7.0,
            7.800,
            8.0,
            8.010,
            9.0,
        ]
        with (
            patch("app.main.ocr_engine.loaded_languages", return_value=["en"]),
            patch("app.main.ocr_engine.recognize", return_value=recognized),
            patch(
                "app.main.translation_service.translate_many_with_provider",
                return_value=ProviderTranslationResult(
                    ["안녕하세요."],
                    ProviderSelection("auto", "deepl"),
                ),
            ),
            patch("app.main.perf_counter", side_effect=clock_ticks),
            patch("app.main.logger.info") as log_info,
        ):
            response = translate_image(request)

        timings = response.processingTimings
        self.assertIsNotNone(timings)
        assert timings is not None  # Help static type checkers after assertIsNotNone.
        self.assertEqual(timings.decodeMs, 12.0)
        self.assertEqual(timings.ocrMs, 450.0)
        self.assertEqual(timings.filterMs, 3.0)
        self.assertEqual(timings.groupingMs, 6.0)
        self.assertEqual(timings.confidenceFilterMs, 2.0)
        self.assertEqual(timings.bubbleFilterMs, 1.0)
        self.assertEqual(timings.translationMs, 800.0)
        self.assertEqual(timings.responseBuildMs, 10.0)
        self.assertEqual(timings.totalMs, 9000.0)
        self.assertEqual(timings.slowestStage, "translation")
        self.assertEqual(timings.slowestStageMs, 800.0)
        self.assertTrue(timings.ocrModelWasLoaded)
        self.assertEqual(
            (
                timings.recognizedRegionCount,
                timings.sourceMatchedRegionCount,
                timings.groupedRegionCount,
                timings.translatedRegionCount,
            ),
            (1, 1, 1, 1),
        )
        self.assertIn("bottleneck=%s:%.1f", log_info.call_args.args[0])

    def test_translation_receives_grouped_text_before_confidence_filtering(self) -> None:
        image = Image.new("RGB", (8, 8), "white")
        encoded = BytesIO()
        image.save(encoded, format="PNG")
        request = TranslateImageRequest(
            image=f"data:image/png;base64,{base64.b64encode(encoded.getvalue()).decode('ascii')}",
            cacheScope="https://comic.test/chapter/395",
            sourceLanguage="en",
            targetLanguage="ko",
            minConfidence=0.55,
        )
        recognized = [
            region("Don't", 10, 10, 80, 18, 0.98),
            # This individual line is below the threshold. It should remain part
            # of the otherwise reliable bubble instead of being silently dropped.
            region("worry.", 16, 33, 70, 18, 0.42),
        ]

        with (
            patch("app.main.ocr_engine.recognize", return_value=recognized),
            patch(
                "app.main.translation_service.translate_many_with_provider",
                return_value=ProviderTranslationResult(
                    ["걱정하지 마세요."],
                    ProviderSelection("auto", "azure"),
                ),
            ) as translate_many,
        ):
            response = translate_image(request)

        self.assertEqual(translate_many.call_args.args[0], ["Don't worry."])
        self.assertEqual(translate_many.call_args.args[4], "auto")
        self.assertEqual(translate_many.call_args.kwargs["cache_scope"], "https://comic.test/chapter/395")
        self.assertEqual(len(response.regions), 1)
        self.assertEqual(response.regions[0].text, "Don't worry.")
        self.assertEqual(response.regions[0].translation, "걱정하지 마세요.")
        self.assertEqual(response.translationProvider, "azure")


    def test_translation_drops_clear_foreign_ocr_regions_before_grouping(self) -> None:
        image = Image.new("RGB", (8, 8), "white")
        encoded = BytesIO()
        image.save(encoded, format="PNG")
        request = TranslateImageRequest(
            image=f"data:image/png;base64,{base64.b64encode(encoded.getvalue()).decode('ascii')}",
            cacheScope="https://comic.test/chapter/395",
            sourceLanguage="en",
            targetLanguage="ko",
        )
        recognized = [
            region("Hello, this is a test.", 10, 10),
            region("안녕하세요", 10, 40),
        ]

        with (
            patch("app.main.ocr_engine.recognize", return_value=recognized),
            patch(
                "app.main.translation_service.translate_many_with_provider",
                return_value=ProviderTranslationResult(
                    ["translated"],
                    ProviderSelection("auto", "azure"),
                ),
            ) as translate_many,
        ):
            response = translate_image(request)

        self.assertEqual(translate_many.call_args.args[0], ["Hello, this is a test."])
        self.assertEqual([item.text for item in response.regions], ["Hello, this is a test."])


if __name__ == "__main__":
    unittest.main()
