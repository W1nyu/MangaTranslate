#!/usr/bin/env python3
"""Run reproducible OCR-pipeline regression timing fixtures.

The fixtures are synthetic, anonymised comic primitives rather than saved page
captures, so they can be committed safely.  They cover the visual conditions
that have repeatedly caused regressions: white speech bubbles, inverse-colour
caption boxes, vertical text, gradients, and a bubble cropped by the viewport.

The default fake OCR provider makes results deterministic and measures the
post-detection work (source filter, grouping, duplicate removal, bubble gate,
and translation handoff).  Pass ``--real-ocr`` to add a PaddleOCR measurement
on the exact same images when profiling a local machine.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

import numpy as np
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.bubble import partition_regions, to_gray  # noqa: E402
from app.ocr import (  # noqa: E402
    discard_contained_ocr_regions,
    filter_regions_by_source_language,
    group_ocr_regions,
)


CANVAS_WIDTH = 640
CANVAS_HEIGHT = 480
ARTWORK = 88
LIGHT_FILL = 245
DARK_FILL = 18
OUTLINE = 20


@dataclass(frozen=True)
class Fixture:
    case_id: str
    kind: str
    expected: str
    expected_boxes: int


def _draw_box(image: np.ndarray, left: int, top: int, right: int, bottom: int, *, fill: int, outline: int) -> None:
    """Draw a bounded box, keeping an offscreen side unoutlined."""
    bounded_left, bounded_top = max(0, left), max(0, top)
    bounded_right, bounded_bottom = min(image.shape[1], right), min(image.shape[0], bottom)
    image[bounded_top:bounded_bottom, bounded_left:bounded_right] = fill
    if left >= 0:
        image[bounded_top:bounded_bottom, bounded_left:bounded_left + 3] = outline
    if right <= image.shape[1]:
        image[bounded_top:bounded_bottom, bounded_right - 3:bounded_right] = outline
    if top >= 0:
        image[bounded_top:bounded_top + 3, bounded_left:bounded_right] = outline
    if bottom <= image.shape[0]:
        image[bounded_bottom - 3:bounded_bottom, bounded_left:bounded_right] = outline


def _draw_glyphs(image: np.ndarray, box: tuple[int, int, int, int], *, value: int, vertical: bool = False) -> None:
    left, top, right, bottom = box
    if vertical:
        for x in range(left, right, 12):
            image[top:bottom:4, x:x + 3] = value
        return
    for y in range(top, bottom, 10):
        image[y:y + 3, left:right:4] = value


def image_and_region(fixture: Fixture) -> tuple[Image.Image, dict[str, float | str]]:
    image = np.full((CANVAS_HEIGHT, CANVAS_WIDTH), ARTWORK, dtype=np.uint8)
    if fixture.kind == "dark":
        bounds = (130, 105, 510, 310)
        text_box = (180, 155, 460, 255)
        _draw_box(image, *bounds, fill=DARK_FILL, outline=LIGHT_FILL)
        _draw_glyphs(image, text_box, value=LIGHT_FILL)
    elif fixture.kind == "vertical":
        bounds = (245, 60, 390, 400)
        text_box = (285, 115, 350, 345)
        _draw_box(image, *bounds, fill=LIGHT_FILL, outline=OUTLINE)
        _draw_glyphs(image, text_box, value=30, vertical=True)
    elif fixture.kind == "gradient":
        bounds = (120, 90, 520, 330)
        text_box = (180, 150, 460, 260)
        _draw_box(image, *bounds, fill=LIGHT_FILL, outline=OUTLINE)
        image[93:327, 123:517] = np.linspace(96, 244, 234, dtype=np.uint8)[:, None]
        _draw_glyphs(image, text_box, value=30)
    elif fixture.kind == "clipped":
        bounds = (130, 305, 510, CANVAS_HEIGHT + 140)
        text_box = (180, 365, 460, 440)
        _draw_box(image, *bounds, fill=LIGHT_FILL, outline=OUTLINE)
        _draw_glyphs(image, text_box, value=30)
    else:
        bounds = (120, 90, 520, 330)
        text_box = (180, 150, 460, 260)
        _draw_box(image, *bounds, fill=LIGHT_FILL, outline=OUTLINE)
        _draw_glyphs(image, text_box, value=30)
    left, top, right, bottom = text_box
    return Image.fromarray(image, mode="L").convert("RGB"), {
        "x": float(left),
        "y": float(top),
        "width": float(right - left),
        "height": float(bottom - top),
        "text": "SAMPLE DIALOGUE",
        "confidence": 0.99,
    }


class FakeTranslationProvider:
    """A no-network provider that keeps benchmark runs safe and repeatable."""

    def translate_many(self, texts: Iterable[str]) -> list[str]:
        return [f"ko:{text}" for text in texts]


def load_fixtures(path: Path) -> list[Fixture]:
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    return [
        Fixture(
            case_id=str(item["id"]),
            kind=str(item["kind"]),
            expected=str(item["expected"]),
            expected_boxes=int(item["expectedBoxes"]),
        )
        for item in payload
    ]


def run_fixture(fixture: Fixture, provider: FakeTranslationProvider, real_ocr: Any | None = None) -> dict[str, Any]:
    decode_started = perf_counter()
    image, fake_region = image_and_region(fixture)
    decode_ms = (perf_counter() - decode_started) * 1_000
    ocr_started = perf_counter()
    recognized = real_ocr.recognize(image, "en") if real_ocr else [fake_region]
    ocr_ms = (perf_counter() - ocr_started) * 1_000

    filter_started = perf_counter()
    filtered = filter_regions_by_source_language(recognized, "en")
    filter_ms = (perf_counter() - filter_started) * 1_000
    grouping_started = perf_counter()
    grouped = discard_contained_ocr_regions(group_ocr_regions(filtered))
    grouping_ms = (perf_counter() - grouping_started) * 1_000
    confidence_started = perf_counter()
    confident = [region for region in grouped if float(region["confidence"]) >= 0.5]
    confidence_filter_ms = (perf_counter() - confidence_started) * 1_000
    bubble_started = perf_counter()
    readable, clipped, absent = partition_regions(to_gray(image), confident)
    bubble_filter_ms = (perf_counter() - bubble_started) * 1_000

    translation_started = perf_counter()
    provider.translate_many(str(region["text"]) for region in readable)
    translation_ms = (perf_counter() - translation_started) * 1_000
    response_started = perf_counter()
    rendered = [
        {"x": region["x"], "y": region["y"], "translation": f"ko:{region['text']}"}
        for region in readable
    ]
    response_build_ms = (perf_counter() - response_started) * 1_000
    state = "complete" if readable else "clipped" if clipped else "absent" if absent else "empty"
    timings = {
        "decode_ms": decode_ms,
        "ocr_ms": ocr_ms,
        "filter_ms": filter_ms,
        "grouping_ms": grouping_ms,
        "confidence_filter_ms": confidence_filter_ms,
        "bubble_filter_ms": bubble_filter_ms,
        "translation_ms": translation_ms,
        "response_build_ms": response_build_ms,
    }
    slowest_stage, slowest_stage_ms = max(timings.items(), key=lambda item: item[1])
    return {
        "case": fixture.case_id,
        "expected": fixture.expected,
        "state": state,
        "expected_boxes": fixture.expected_boxes,
        "recognized_boxes": len(recognized),
        **{name: round(value, 2) for name, value in timings.items()},
        "total_ms": round(sum(timings.values()), 2),
        "slowest_stage": slowest_stage.removesuffix("_ms"),
        "slowest_stage_ms": round(slowest_stage_ms, 2),
        "rendered_boxes": len(rendered),
        "passed": state == fixture.expected and (real_ocr is not None or len(recognized) == fixture.expected_boxes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=PROJECT_ROOT / "tests" / "fixtures" / "bench_ocr_cases.json",
        help="JSON fixture manifest (default: %(default)s)",
    )
    parser.add_argument("--iterations", type=int, default=3, help="Runs per fixture (default: %(default)s)")
    parser.add_argument("--output", type=Path, help="Optional CSV file; otherwise print CSV to stdout")
    parser.add_argument("--real-ocr", action="store_true", help="Use the local PaddleOCR model instead of fixture boxes")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be at least 1")

    fixtures = load_fixtures(args.fixtures)
    real_ocr = None
    if args.real_ocr:
        from app.ocr import PaddleOcrEngine

        real_ocr = PaddleOcrEngine()
    provider = FakeTranslationProvider()
    rows = [
        {"iteration": iteration + 1, **run_fixture(fixture, provider, real_ocr)}
        for iteration in range(args.iterations)
        for fixture in fixtures
    ]
    fieldnames = list(rows[0]) if rows else ["iteration", "case", "expected", "state", "passed"]
    destination = args.output.open("w", newline="", encoding="utf-8") if args.output else sys.stdout
    try:
        writer = csv.DictWriter(destination, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if args.output:
            destination.close()
    return 0 if all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
