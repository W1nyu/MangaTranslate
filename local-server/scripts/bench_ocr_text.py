#!/usr/bin/env python3
"""Measure real OCR accuracy and speed on rendered comic-style dialogue.

``bench_ocr.py`` checks the post-OCR pipeline with synthetic glyph stripes.
This script instead renders real all-caps dialogue in comic-like Windows fonts
(white outlined bubbles, inverse caption boxes, noisy artwork), runs the
server's own ``PaddleOcrEngine`` and pipeline on it, and scores every bubble
against its ground truth: bubble recall, exact-text rate, character error rate
and OCR milliseconds per page.

    .\\.venv\\Scripts\\python.exe scripts\\bench_ocr_text.py
    .\\.venv\\Scripts\\python.exe scripts\\bench_ocr_text.py --engine paddle --scale 0.7 --jpeg 70

Pages are generated from fixed seeds, so runs are comparable.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import sys
from pathlib import Path
from time import perf_counter

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

FONT_DIRECTORY = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
FONT_FILES = ("comicbd.ttf", "comic.ttf", "comicz.ttf", "segoeprb.ttf", "ariblk.ttf", "arialbd.ttf")

LINES = (
    "I THOUGHT YOU WERE GONE.", "WHAT THE HELL ARE YOU DOING HERE?!",
    "DON'T TELL ME... YOU'RE THE ONE WHO DID THIS?", "HEY! WAIT UP!",
    "YOU'VE GOT SOME NERVE SHOWING YOUR FACE AROUND HERE.", "SO THAT'S HOW IT IS.",
    "HE'S NOT WRONG, THOUGH.", "IT'S NOT LIKE I CARE OR ANYTHING.",
    "THE ENTRANCE TO THE DUNGEON IS RIGHT OVER THERE.", "I KNEW IT. HE WAS HIDING HIS REAL POWER ALL ALONG.",
    "WHO ARE YOU CALLING SHORT?!", "MY BAD.", "I'M FINE. JUST A LITTLE TIRED.",
    "YOU DON'T LOOK FINE TO ME.", "I SAID I'M FINE!", "...OKAY. IF YOU SAY SO.",
    "WARNING! A HIGH-LEVEL MONSTER HAS APPEARED.", "TCH. NOT NOW...",
    "SHE'S THE STRONGEST HUNTER IN THE GUILD.", "WE HAVE TO GET OUT OF HERE, NOW!",
    "THAT'S IMPOSSIBLE! NOBODY COULD SURVIVE THAT!", "LISTEN CAREFULLY. I WON'T SAY THIS TWICE.",
    "HOW LONG HAVE YOU BEEN STANDING THERE?", "THANK YOU FOR SAVING ME.",
    "WAIT, WHAT DID YOU JUST SAY?", "YOUR LEVEL IS TOO LOW TO ENTER THIS GATE.",
    "STOP RIGHT THERE!", "HUH? WHERE AM I?", "QUEST COMPLETE! REWARD: 500 GOLD.",
    "PLEASE, JUST LET ME GO HOME.", "NO WAY... IS THAT REALLY HIM?",
)


def _fonts() -> list[Path]:
    fonts = [FONT_DIRECTORY / name for name in FONT_FILES if (FONT_DIRECTORY / name).is_file()]
    if not fonts:
        raise SystemExit(f"No benchmark fonts found in {FONT_DIRECTORY}.")
    return fonts


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _artwork(rng: random.Random, width: int, height: int) -> Image.Image:
    base = np.full((height, width, 3), rng.randint(90, 170), dtype=np.int16)
    noise = np.random.default_rng(rng.randint(0, 1 << 30)).integers(-25, 25, (height, width, 1))
    image = Image.fromarray(np.clip(base + noise, 0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(image)
    for _ in range(40):
        x0, y0 = rng.randint(0, width), rng.randint(0, height)
        shade = rng.randint(20, 230)
        draw.line(
            (x0, y0, x0 + rng.randint(-300, 300), y0 + rng.randint(-300, 300)),
            fill=(shade, shade, shade),
            width=rng.randint(2, 12),
        )
    return image.filter(ImageFilter.GaussianBlur(1.2))


def make_page(seed: int, fonts: list[Path], width: int = 1440, height: int = 1300):
    """One viewport-sized page and the ground-truth text and box of each bubble."""
    rng = random.Random(seed)
    image = _artwork(rng, width, height)
    draw = ImageDraw.Draw(image)
    truths: list[dict[str, object]] = []
    slots = [(60, 60), (760, 120), (80, 520), (740, 600), (200, 950), (820, 1000)]
    rng.shuffle(slots)
    for index, (left, top) in enumerate(slots[: rng.randint(4, 5)]):
        text = rng.choice(LINES)
        font = ImageFont.truetype(str(rng.choice(fonts)), rng.choice([21, 24, 27, 30, 34]))
        lines = _wrap(draw, text, font, rng.randint(260, 420))
        ascent, descent = font.getmetrics()
        line_height = int((ascent + descent) * 1.08)
        text_width = max(int(draw.textlength(line, font=font)) for line in lines)
        text_height = line_height * len(lines)
        dark = index == 0 and seed % 3 == 0
        pad_x, pad_y = (28, 22) if dark else (int(text_width * 0.22) + 24, int(text_height * 0.32) + 22)
        right, bottom = left + text_width + 2 * pad_x, top + text_height + 2 * pad_y
        if right >= width - 10 or bottom >= height - 10:
            continue
        if dark:
            draw.rectangle((left, top, right, bottom), fill=(16, 16, 16), outline=(240, 240, 240), width=3)
            colour = (245, 245, 245)
        else:
            draw.ellipse((left, top, right, bottom), fill=(250, 250, 250), outline=(15, 15, 15), width=4)
            colour = (12, 12, 12)
        y = top + pad_y
        for line in lines:
            draw.text((left + (right - left - draw.textlength(line, font=font)) / 2, y), line, font=font, fill=colour)
            y += line_height
        truths.append({"text": " ".join(lines), "box": (left, top, right, bottom)})
    return image.convert("RGB"), truths


def _levenshtein(first: str, second: str) -> int:
    previous = list(range(len(second) + 1))
    for row, first_character in enumerate(first, 1):
        current = [row]
        for column, second_character in enumerate(second, 1):
            current.append(
                min(previous[column] + 1, current[column - 1] + 1, previous[column - 1] + (first_character != second_character))
            )
        previous = current
    return previous[-1]


def _normalized(text: str) -> str:
    return " ".join(text.upper().split())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", choices=("auto", "onnxruntime", "paddle"), default="auto")
    parser.add_argument("--pages", type=int, default=12)
    parser.add_argument("--scale", type=float, default=1.0, help="Downscale pages to mimic a lower capture density")
    parser.add_argument("--jpeg", type=int, default=0, help="Re-encode pages as JPEG at this quality (0 = off)")
    parser.add_argument("--show-failures", action="store_true")
    args = parser.parse_args()

    os.environ["OCR_ENGINE"] = args.engine
    from app.bubble import partition_regions, to_gray
    from app.ocr import PaddleOcrEngine, discard_contained_ocr_regions, filter_regions_by_source_language, group_ocr_regions

    fonts = _fonts()
    engine = PaddleOcrEngine()
    engine.warmup("en")
    totals = {"bubbles": 0, "found": 0, "exact": 0, "edits": 0, "characters": 0, "ms": 0.0}
    failures: list[dict[str, object]] = []
    for seed in range(args.pages):
        image, truths = make_page(seed, fonts)
        if args.scale != 1.0:
            image = image.resize((round(image.width * args.scale), round(image.height * args.scale)), Image.LANCZOS)
            truths = [{**truth, "box": tuple(value * args.scale for value in truth["box"])} for truth in truths]
        if args.jpeg:
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=args.jpeg)
            buffer.seek(0)
            image = Image.open(buffer).convert("RGB")

        started_at = perf_counter()
        regions = engine.recognize(image, "en")
        totals["ms"] += (perf_counter() - started_at) * 1000
        regions = discard_contained_ocr_regions(group_ocr_regions(filter_regions_by_source_language(regions, "en")))
        regions = [region for region in regions if float(region["confidence"]) >= 0.55]
        readable, _clipped, _bubbleless = partition_regions(to_gray(image), regions)

        for truth in truths:
            left, top, right, bottom = truth["box"]
            inside = sorted(
                (
                    region
                    for region in readable
                    if left <= float(region["x"]) + float(region["width"]) / 2 <= right
                    and top <= float(region["y"]) + float(region["height"]) / 2 <= bottom
                ),
                key=lambda region: (float(region["y"]), float(region["x"])),
            )
            expected = _normalized(str(truth["text"]))
            totals["bubbles"] += 1
            totals["characters"] += len(expected)
            if not inside:
                totals["edits"] += len(expected)
                failures.append({"seed": seed, "expected": expected, "got": None})
                continue
            totals["found"] += 1
            got = _normalized(" ".join(str(region["text"]) for region in inside))
            distance = _levenshtein(expected, got)
            totals["edits"] += distance
            if distance == 0:
                totals["exact"] += 1
            else:
                failures.append({"seed": seed, "expected": expected, "got": got})

    summary = {
        "engine": engine.loaded_engines().get("en"),
        "pages": args.pages,
        "bubbles": totals["bubbles"],
        "bubbleRecall": round(totals["found"] / totals["bubbles"], 3),
        "exactRate": round(totals["exact"] / totals["bubbles"], 3),
        "characterErrorRate": round(totals["edits"] / totals["characters"], 4),
        "ocrMsPerPage": round(totals["ms"] / args.pages, 1),
    }
    print(json.dumps(summary))
    if args.show_failures:
        for failure in failures:
            print(json.dumps(failure))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
