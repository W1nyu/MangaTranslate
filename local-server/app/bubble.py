"""Decide whether an OCR region really sits inside a fully visible speech bubble.

Two problems this solves for the reader:

1. Text recognised on artwork - sound effects, signage, lettering baked into the
   drawing - produced a translation patch floating where there is no bubble.
2. Scrolling puts half a bubble on screen. Reading that half yields a truncated
   sentence, and translating it produces confident nonsense.

Both are decided from the captured pixels: a speech bubble is usually a light
fill enclosing dark text, but webtoons also use black/dark caption boxes with
white lettering. If that fill runs off the edge of the captured image, the
bubble is cut and the region is left for a later capture that contains all of
it. The checks are deliberately conservative - an inconclusive region is kept,
because dropping real dialogue is worse than tolerating an occasional stray
patch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal, Sequence

import numpy as np


# A bubble fill is far lighter than inked artwork. Text sitting on anything
# darker than this is treated as lettering on the drawing rather than dialogue.
LIGHT_FILL_THRESHOLD = 165
# A black caption box with white lettering used to be discarded as artwork
# because the light-fill-only rule treated its dark background as "absent".
# Dark fills are valid when the OCR box itself contains enough bright glyph
# pixels to establish the inverse text/background contrast.
DARK_FILL_THRESHOLD = 80
MIN_INK_CONTRAST = 64
MIN_INK_RATIO = 0.012
# How far a pixel may drift from the sampled fill and still count as bubble.
# Generous enough for paper texture, screentone and JPEG ringing.
FILL_TOLERANCE = 46
# Fraction of a row or column that must still be fill for the bubble to be
# considered to continue in that direction. Below 1.0 so a rounded outline or an
# ascender poking sideways does not end the search early.
FILL_LINE_RATIO = 0.7
# Never search further than this from the text, in pixels, so an open white page
# background cannot turn into an unbounded scan.
MAX_EXPAND_PX = 260
MIN_EXPAND_PX = 40
# A grouping box is tight around the glyphs, so a descender or an antialiased
# stroke routinely spills a pixel or two past it. Those lines are skipped rather
# than mistaken for the bubble's outline.
EDGE_GUTTER_PX = 3
# A bubble outline is drawn several pixels thick and spans the whole width, so a
# real boundary shows up as consecutive failing lines. Requiring a streak keeps
# one stray glyph row from ending the search.
BOUNDARY_STREAK_PX = 2

EDGES = ("left", "top", "right", "bottom")

BubbleStatus = Literal["complete", "clipped", "absent"]


@dataclass(frozen=True)
class BubbleAnalysis:
    status: BubbleStatus
    #: Image edges the bubble fill ran into, for a clipped region.
    edges: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.status == "complete"


def _region_box(region: Any, width: int, height: int) -> tuple[int, int, int, int] | None:
    """Clamp a region to whole pixels inside the image."""
    try:
        left = int(np.floor(float(region["x"])))
        top = int(np.floor(float(region["y"])))
        right = int(np.ceil(float(region["x"]) + float(region["width"])))
        bottom = int(np.ceil(float(region["y"]) + float(region["height"])))
    except (KeyError, TypeError, ValueError):
        return None

    left = max(0, min(left, width))
    top = max(0, min(top, height))
    right = max(left, min(right, width))
    bottom = max(top, min(bottom, height))
    if right - left < 2 or bottom - top < 2:
        return None
    return left, top, right, bottom


def _fill_value(gray: np.ndarray, box: tuple[int, int, int, int]) -> float:
    """Estimate the dominant fill brightness from inside the text box.

    OCR boxes contain both glyphs and their background.  Taking only the upper
    quartile works for black text on white, but mistakes dense white lettering
    for a white background.  The most populated 16-level luminance band instead
    selects the local background for both normal and inverse-colour captions.
    Sampling a ring outside the box would be fooled by a loose grouping box that
    already reaches the bubble outline.
    """
    left, top, right, bottom = box
    sample = gray[top:bottom, left:right]
    if not sample.size:
        return 0.0
    bands = sample.astype(np.uint8).reshape(-1) // 16
    dominant_band = int(np.argmax(np.bincount(bands, minlength=16)))
    return float(np.median(sample[(sample // 16) == dominant_band]))


def _ink_ratios(gray: np.ndarray, box: tuple[int, int, int, int], fill_value: float) -> tuple[float, float]:
    """How much darker/lighter text-like ink is inside a candidate fill.

    OCR boxes are tight around glyphs, making them a more reliable contrast
    sample than the illustrated background outside them.  The low threshold
    accepts antialiased or coloured lettering without promoting a few JPEG
    speckles into text.
    """
    left, top, right, bottom = box
    sample = gray[top:bottom, left:right].astype(np.int16)
    if not sample.size:
        return 0.0, 0.0
    dark = np.count_nonzero(sample <= fill_value - MIN_INK_CONTRAST) / sample.size
    light = np.count_nonzero(sample >= fill_value + MIN_INK_CONTRAST) / sample.size
    return float(dark), float(light)


def _outward_run(ratios: np.ndarray) -> int:
    """How many lines, counting outward from the text, are still bubble fill.

    Stops at the first run of ``BOUNDARY_STREAK_PX`` consecutive non-fill lines,
    ignoring a small gutter of glyph spill next to the box.
    """
    if ratios.size == 0:
        return 0
    if ratios.size <= EDGE_GUTTER_PX:
        return int(ratios.size)

    failing = ratios[EDGE_GUTTER_PX:] < FILL_LINE_RATIO
    if failing.size < BOUNDARY_STREAK_PX:
        return int(ratios.size)

    streak = failing.copy()
    for offset in range(1, BOUNDARY_STREAK_PX):
        streak[: streak.size - offset] &= failing[offset:]
    streak[streak.size - BOUNDARY_STREAK_PX + 1 :] = False

    boundary = np.nonzero(streak)[0]
    return int(EDGE_GUTTER_PX + boundary[0]) if boundary.size else int(ratios.size)


def _expand(fill: np.ndarray, box: tuple[int, int, int, int], budget: int) -> dict[str, str]:
    """Grow the box outward along each axis while the pixels stay bubble fill.

    Returns, per edge, whether the search was ``blocked`` by the bubble outline,
    ran into the image ``border``, or stayed ``open`` until the budget ran out.
    """
    left, top, right, bottom = box
    height, width = fill.shape
    outcome: dict[str, str] = {}

    # Each axis is probed across the text box's own extent on the other axis, so
    # a rounded corner cannot cut the search short.
    windows = {
        # ratios are ordered outward from the box in every case
        "left": fill[top:bottom, max(0, left - budget):left].mean(axis=0)[::-1],
        "right": fill[top:bottom, right:min(width, right + budget)].mean(axis=0),
        "top": fill[max(0, top - budget):top, left:right].mean(axis=1)[::-1],
        "bottom": fill[bottom:min(height, bottom + budget), left:right].mean(axis=1),
    }
    available = {
        "left": left,
        "right": width - right,
        "top": top,
        "bottom": height - bottom,
    }

    for edge in EDGES:
        run = _outward_run(windows[edge])
        reachable = min(budget, available[edge])
        if run < reachable:
            outcome[edge] = "blocked"
        elif available[edge] <= budget:
            # The fill continued all the way to the edge of the capture, so the
            # rest of this bubble is outside the image.
            outcome[edge] = "border"
        else:
            outcome[edge] = "open"
    return outcome


def analyze_bubble(gray: np.ndarray, region: Any) -> BubbleAnalysis:
    """Classify one OCR region against the grayscale capture it came from."""
    height, width = gray.shape[:2]
    box = _region_box(region, width, height)
    if box is None:
        # Too small to judge; leave it to the confidence filter.
        return BubbleAnalysis("complete")

    left, top, right, bottom = box
    touching = tuple(
        edge
        for edge, hit in (("left", left == 0), ("top", top == 0), ("right", right == width), ("bottom", bottom == height))
        if hit
    )
    if touching:
        # The recognised text itself runs off the capture, so the words are cut.
        return BubbleAnalysis("clipped", touching)

    fill_value = _fill_value(gray, box)
    dark_ink_ratio, light_ink_ratio = _ink_ratios(gray, box, fill_value)
    if fill_value >= LIGHT_FILL_THRESHOLD:
        fill_kind = "light"
    elif fill_value <= DARK_FILL_THRESHOLD:
        if light_ink_ratio < MIN_INK_RATIO:
            # Dark surroundings with no bright glyph contrast remain artwork.
            return BubbleAnalysis("absent")
        fill_kind = "dark"
    elif dark_ink_ratio >= MIN_INK_RATIO:
        # Coloured/mid-tone caption boxes commonly have dark lettering. Keep
        # them when an enclosing border confirms that this is a bounded box.
        fill_kind = "mid"
    elif light_ink_ratio >= MIN_INK_RATIO:
        # A saturated dark colour can be brighter than black in luminance yet
        # still contain white lettering.
        fill_kind = "dark"
    else:
        return BubbleAnalysis("absent")

    fill = np.abs(gray.astype(np.int16) - fill_value) <= FILL_TOLERANCE
    budget = int(max(MIN_EXPAND_PX, min(MAX_EXPAND_PX, 0.9 * max(right - left, bottom - top))))
    outcome = _expand(fill, box, budget)

    edges = tuple(edge for edge in EDGES if outcome[edge] == "border")
    if fill_kind == "mid" and not any(value == "blocked" for value in outcome.values()):
        # A uniform medium-toned piece of artwork has no caption outline. Do
        # not accept it solely because it happened to contain dark pixels.
        return BubbleAnalysis("absent")
    if edges:
        return BubbleAnalysis("clipped", edges)
    return BubbleAnalysis("complete")


def partition_regions(
    gray: np.ndarray,
    regions: Sequence[Any],
) -> tuple[list[Any], list[dict[str, float | tuple[str, ...]]], int]:
    """Split regions into the ones worth translating and the ones to defer.

    Returns the usable regions, geometry for the clipped ones (so the caller can
    avoid marking that part of the page as read), and how many were dropped for
    having no bubble around them.
    """
    usable: list[Any] = []
    clipped: list[dict[str, float | tuple[str, ...]]] = []
    absent = 0

    for region in regions:
        analysis = analyze_bubble(gray, region)
        if analysis.usable:
            usable.append(region)
            continue
        if analysis.status == "absent":
            absent += 1
            continue
        clipped.append(
            {
                "x": float(region["x"]),
                "y": float(region["y"]),
                "width": float(region["width"]),
                "height": float(region["height"]),
                "edges": analysis.edges,
            }
        )
    return usable, clipped, absent


def to_gray(image: Iterable[Any]) -> np.ndarray:
    """Luminance view of a PIL RGB image, without a second full decode."""
    return np.asarray(image.convert("L"), dtype=np.uint8)  # type: ignore[attr-defined]
