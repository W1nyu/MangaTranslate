from __future__ import annotations

import json
import logging
import math
import os
import re
import unicodedata
from collections import Counter
from threading import Lock
from typing import Any

import numpy as np
from PIL import Image, ImageDraw


logger = logging.getLogger("uvicorn.error")


PADDLE_LANGUAGE_CODES = {
    "en": "en",
    "ko": "korean",
    "ja": "japan",
    "zh": "ch",
    "es": "es",
    "fr": "fr",
    "de": "de",
    "pt": "pt",
    "ru": "ru",
    "vi": "vi",
}

# Mobile PP-OCRv5 models deliberately favor visible-screen responsiveness over
# server-model accuracy. Webtoon use scans a new viewport repeatedly.
PADDLE_RECOGNITION_MODELS = {
    "en": "en_PP-OCRv5_mobile_rec",
    "ko": "korean_PP-OCRv5_mobile_rec",
    "ja": "PP-OCRv5_mobile_rec",
    "zh": "PP-OCRv5_mobile_rec",
    "es": "latin_PP-OCRv5_mobile_rec",
    "fr": "latin_PP-OCRv5_mobile_rec",
    "de": "latin_PP-OCRv5_mobile_rec",
    "pt": "latin_PP-OCRv5_mobile_rec",
    "ru": "eslav_PP-OCRv5_mobile_rec",
    "vi": "latin_PP-OCRv5_mobile_rec",
}

# The grouping heuristic follows top-to-bottom, left-to-right text flow.
LTR_LINE_GROUPING_LANGUAGES = frozenset({"en", "es", "fr", "de", "pt", "ru", "vi"})
# Japanese and Chinese pages may place one character (or short run) per OCR
# box in a vertical column. Korean remains ungrouped here: vertical Korean is
# uncommon and its square glyphs are too easy to mistake for separate labels.
VERTICAL_CJK_GROUPING_LANGUAGES = frozenset({"ja", "zh"})

# A visible webtoon viewport is often much larger than the mobile detector
# needs. PaddleOCR 3's base pipeline otherwise retains its conservative
# 64-pixel minimum-side setting, which can pass the whole high-DPI screenshot
# through to the detector. A 1280px ceiling keeps fine inverse-colour lettering
# legible while still bounding CPU work on a high-DPI viewport.
# Restrict only horizontal scripts: CJK readers retain their existing
# full-resolution/orientation behavior because rotated and vertical text is
# more common there.
FAST_HORIZONTAL_OCR_LANGUAGES = LTR_LINE_GROUPING_LANGUAGES
FAST_TEXT_DETECTION_LIMIT_SIDE_LEN = 1280
# CJK retains more detector detail for small vertical glyphs, but must not send
# an unbounded high-DPI browser capture through the detector.
CJK_TEXT_DETECTION_LIMIT_SIDE_LEN = 1536
# PaddleOCR's defaults (0.3 / 0.6) are tuned for high-contrast document scans.
# Webtoon text often has outlines, gradients, glows, and reverse colours, so
# retain weaker detector boxes and let the source-language, confidence, and
# bubble-context stages reject unsuitable ones later in the pipeline.
TEXT_DETECTION_THRESHOLD = 0.24
TEXT_DETECTION_BOX_THRESHOLD = 0.48
# A detector sometimes returns a tight word/line polygon in addition to the
# already-grouped box for its complete speech bubble.  Suppress the smaller
# target only when its text and geometry both prove that it is contained in the
# larger one; close but distinct bubbles must remain independent.
CONTAINED_REGION_OVERLAP_RATIO = 0.85
# Geometry-only suppression is a last resort for OCR fragments whose glyphs
# differ from the complete line. Keep it stricter than text+geometry matching.
FUZZY_CONTAINED_REGION_OVERLAP_RATIO = 0.95
GEOMETRY_ONLY_MAX_AREA_RATIO = 0.9

# Script checks are intentionally conservative.  OCR can return names, sound
# effects, or a one-letter answer, so those ambiguous fragments are kept.  The
# filter only removes text when its script (or a strong Latin-language hint)
# makes it very unlikely to be the source language selected by the user.
LATIN_SOURCE_LANGUAGES = frozenset({"en", "es", "fr", "de", "pt", "vi"})

_LATIN_LANGUAGE_HINTS = {
    "en": frozenset({"the", "and", "you", "your", "this", "that", "what", "with", "hello", "thanks", "please", "don't", "can't", "i'm", "you're"}),
    "es": frozenset({"hola", "gracias", "cómo", "como", "estás", "estoy", "quiero", "porque", "pero", "para", "usted", "ustedes", "qué"}),
    "fr": frozenset({"bonjour", "merci", "je", "suis", "pas", "avec", "pourquoi", "comment", "vous", "nous", "dans", "j'ai"}),
    "de": frozenset({"hallo", "danke", "nicht", "ich", "und", "ist", "das", "wie", "was", "aber", "bitte", "mein"}),
    "pt": frozenset({"olá", "obrigado", "obrigada", "não", "você", "voce", "estou", "porque", "quero", "para", "com", "fazer"}),
    "vi": frozenset({"xin", "chào", "tôi", "bạn", "không", "được", "của", "với", "một", "này", "là", "biết"}),
}

_LATIN_LANGUAGE_MARKERS = {
    "es": frozenset({"ñ", "¿", "¡"}),
    "fr": frozenset({"à", "œ", "ë", "ï", "ÿ"}),
    "de": frozenset({"ä", "ö", "ü", "ß"}),
    "pt": frozenset({"ã", "õ"}),
    "vi": frozenset({"ă", "đ", "ê", "ơ", "ư"}),
}


class UnsupportedOcrLanguage(ValueError):
    pass


def _letter_script(character: str) -> str | None:
    """Return a broad Unicode script bucket for a letter-like character."""
    codepoint = ord(character)
    if not unicodedata.category(character).startswith("L"):
        return None
    if (
        0x1100 <= codepoint <= 0x11FF
        or 0x3130 <= codepoint <= 0x318F
        or 0xA960 <= codepoint <= 0xA97F
        or 0xAC00 <= codepoint <= 0xD7FF
        or 0xFFA0 <= codepoint <= 0xFFDC
    ):
        return "hangul"
    if 0x3040 <= codepoint <= 0x309F:
        return "hiragana"
    if 0x30A0 <= codepoint <= 0x30FF or 0x31F0 <= codepoint <= 0x31FF or 0xFF66 <= codepoint <= 0xFF9D:
        return "katakana"
    if (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2EBEF
        or codepoint in {0x3005, 0x3007}
    ):
        return "han"
    if 0x3100 <= codepoint <= 0x312F or 0x31A0 <= codepoint <= 0x31BF:
        return "bopomofo"
    if 0x0400 <= codepoint <= 0x052F or 0x2DE0 <= codepoint <= 0x2DFF or 0xA640 <= codepoint <= 0xA69F:
        return "cyrillic"
    if (
        0x0041 <= codepoint <= 0x024F
        or 0x1E00 <= codepoint <= 0x1EFF
        or 0x2C60 <= codepoint <= 0x2C7F
        or 0xA720 <= codepoint <= 0xA7FF
        or 0xAB30 <= codepoint <= 0xAB6F
        or 0xFF21 <= codepoint <= 0xFF5A
    ):
        return "latin"
    return "other"


def _script_counts(text: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for character in unicodedata.normalize("NFC", text):
        script = _letter_script(character)
        if script:
            counts[script] += 1
    return counts


def _clearly_detected_latin_language(text: str) -> str | None:
    """Return a Latin language only for sufficiently strong local evidence.

    This is deliberately not a general language detector: among English,
    Spanish, French, German, Portuguese, and Vietnamese, short dialogue and
    names are too ambiguous.  A result is returned only when multiple words or
    distinctive glyphs agree, which lets callers reject clear mismatches without
    dropping valid comic text.
    """
    normalized = unicodedata.normalize("NFC", text).casefold()
    words = re.findall(r"[^\W\d_]+(?:['’][^\W\d_]+)*", normalized, flags=re.UNICODE)
    latin_letters = sum(1 for character in normalized if _letter_script(character) == "latin")
    if latin_letters < 6 or len(words) < 2:
        return None

    scores: Counter[str] = Counter()
    for language, hints in _LATIN_LANGUAGE_HINTS.items():
        scores[language] += sum(3 for word in words if word in hints)
    for language, markers in _LATIN_LANGUAGE_MARKERS.items():
        scores[language] += sum(4 for character in normalized if character in markers)

    best_language, best_score = max(scores.items(), key=lambda item: item[1])
    runner_up_score = max(score for language, score in scores.items() if language != best_language)
    if best_score >= 6 and best_score >= runner_up_score + 3:
        return best_language
    return None


def _foreign_script_dominates(expected_letters: int, all_letters: int) -> bool:
    foreign_letters = all_letters - expected_letters
    # Keep ordinary code-switching, but reject a long foreign block with only a
    # stray expected character (often OCR noise or a decorative initial).
    return foreign_letters >= 3 and (expected_letters == 0 or foreign_letters > expected_letters * 2)


def is_probably_source_language(text: str, source_language: str) -> bool:
    """Whether OCR text is safe to translate for ``source_language``.

    An inconclusive result is treated as a match.  This prevents short answers
    such as "No", names, numbers, or sound effects from disappearing, while
    unmistakable foreign scripts and strong Latin-language mismatches are kept
    out of the translation request.
    """
    source = source_language.strip().lower().split("-", 1)[0]
    counts = _script_counts(text)
    all_letters = sum(counts.values())
    if all_letters <= 2 or source not in PADDLE_LANGUAGE_CODES:
        return True

    latin_language = _clearly_detected_latin_language(text)

    if source in LATIN_SOURCE_LANGUAGES:
        latin_letters = counts["latin"]
        if latin_letters == 0:
            return False
        if _foreign_script_dominates(latin_letters, all_letters):
            return False
        return latin_language in {None, source}

    if source == "ru":
        expected_letters = counts["cyrillic"]
        if expected_letters:
            return not _foreign_script_dominates(expected_letters, all_letters)
        return False

    if source == "ko":
        expected_letters = counts["hangul"]
        if expected_letters:
            return not _foreign_script_dominates(expected_letters, all_letters)
        # Keep one- or two-letter labels (handled above), but a longer Latin
        # block is not Korean source text even if it is only a name.
        if counts["latin"] == all_letters:
            return False
        return False

    if source == "ja":
        expected_letters = counts["han"] + counts["hiragana"] + counts["katakana"]
        if expected_letters:
            return not _foreign_script_dominates(expected_letters, all_letters)
        if counts["latin"] == all_letters:
            return False
        return False

    if source == "zh":
        # A sequence of kana is strong evidence of Japanese even when it also
        # contains kanji.  A single kana is left alone as an ambiguous OCR token.
        if counts["hiragana"] + counts["katakana"] >= 2:
            return False
        expected_letters = counts["han"] + counts["bopomofo"]
        if expected_letters:
            return not _foreign_script_dominates(expected_letters, all_letters)
        if counts["latin"] == all_letters:
            return False
        return False

    return True


def filter_regions_by_source_language(
    regions: list[dict[str, float | str]], source_language: str
) -> list[dict[str, float | str]]:
    """Drop only OCR regions that clearly do not match the selected source."""
    return [
        region
        for region in regions
        if is_probably_source_language(str(region.get("text", "")), source_language)
    ]


# Comic lettering makes the recognizer read O as 0 and I as 1 or |. A cloud
# translator then sees an unknown token: Azure turned "YOU WERE G0NE FOR SO
# L0NG" into "you took so long". Only these two glyph pairs are repaired, only
# inside a word that is otherwise letters, and never in an ordinal or a
# number-with-unit such as 1ST, 10K or 1990S.
_OCR_WORD_PATTERN = re.compile(r"[A-Za-z0-9|]+(?:['’][A-Za-z]+)*")
_NUMBER_WITH_SUFFIX_PATTERN = re.compile(r"^\d+(?:st|nd|rd|th|s|k|m|x|v\d*|am|pm|d|h)$", re.IGNORECASE)
# A short label followed by a number (LV10, PS1, B1, HP100) is not a misread.
_LABEL_WITH_NUMBER_PATTERN = re.compile(r"^[A-Za-z]{1,3}\d+$")
_STANDALONE_I_CONTRACTION_PATTERN = re.compile(r"^[1|](['’](?:m|ll|ve|d))$", re.IGNORECASE)
# Two-character dialogue words whose single letter would otherwise be too
# little evidence for the general rule below.
_MISREAD_SHORT_WORDS = {
    "n0": "no", "g0": "go", "s0": "so", "t0": "to", "d0": "do",
    "0f": "of", "0n": "on", "0r": "or", "0h": "oh", "0k": "ok",
    "1f": "if", "1n": "in", "1t": "it", "1s": "is",
}
# Words that commonly follow a standalone "I". A lone "1" before one of them is
# far more likely a misread pronoun than the number one.
_FOLLOWS_PRONOUN_I = frozenset({
    "am", "was", "will", "would", "could", "should", "can", "can't", "cannot", "do", "don't",
    "did", "didn't", "won't", "wouldn't", "couldn't", "have", "haven't", "had", "think",
    "thought", "know", "knew", "need", "needed", "want", "wanted", "said", "say", "see",
    "saw", "mean", "meant", "guess", "just", "really", "never", "always", "already",
    "love", "hate", "told", "got", "swear", "promise", "bet", "feel", "felt", "must",
    "might", "hope", "wish", "came", "went", "remember", "forgot", "understand",
})


def _repair_ocr_word(word: str) -> str:
    contraction = _STANDALONE_I_CONTRACTION_PATTERN.match(word)
    if contraction:
        return "I" + contraction.group(1)
    if not any(character in "01|" for character in word):
        return word
    if any(character.isdigit() and character not in "01" for character in word):
        return word
    letters = [character for character in word if character.isalpha()]
    uppercase = sum(1 for character in letters if character.isupper()) >= len(letters) / 2
    short_word = _MISREAD_SHORT_WORDS.get(word.casefold())
    if short_word:
        return short_word.upper() if uppercase else short_word
    stem = re.split(r"['’]", word, maxsplit=1)[0]
    if len(letters) < 2 or _NUMBER_WITH_SUFFIX_PATTERN.match(stem) or _LABEL_WITH_NUMBER_PATTERN.match(stem):
        return word
    return word.translate(
        str.maketrans({"0": "O" if uppercase else "o", "1": "I" if uppercase else "l", "|": "I" if uppercase else "l"})
    )


def normalize_ocr_text_for_translation(text: str, source_language: str) -> str:
    """Repair unambiguous OCR glyph confusions before text goes to a translator.

    Only Latin-script sources are touched; the displayed OCR text is unchanged.
    """
    if source_language.strip().lower().split("-", 1)[0] not in LATIN_SOURCE_LANGUAGES:
        return text
    words = list(_OCR_WORD_PATTERN.finditer(text))
    if not words:
        return text
    pieces: list[str] = []
    cursor = 0
    for index, match in enumerate(words):
        word = match.group(0)
        repaired = _repair_ocr_word(word)
        if repaired == word and word in {"1", "|"}:
            following = words[index + 1].group(0).casefold().replace("’", "'") if index + 1 < len(words) else ""
            # "|" is never dialogue punctuation; "1" needs a pronoun-shaped
            # next word so "1 MORE" or "TAKE 1" survive.
            if word == "|" or following in _FOLLOWS_PRONOUN_I:
                repaired = "I"
        pieces.append(text[cursor : match.start()])
        pieces.append(repaired)
        cursor = match.end()
    pieces.append(text[cursor:])
    return "".join(pieces)


def _normalized_region_text(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _comparison_region_text(value: object) -> str:
    """Return an OCR-only comparison key without changing displayed text."""
    normalized = _normalized_region_text(value)
    compact = "".join(character for character in normalized if character.isalnum())
    return compact.translate(str.maketrans({
        "0": "o", "o": "o",
        "1": "i", "l": "i", "i": "i",
        "5": "s", "s": "s",
        "8": "b", "b": "b",
        "2": "z", "z": "z",
    }))


def _text_contains_fragment(full_text: str, fragment: str) -> bool:
    """Whether a grouped OCR result includes a whole detected fragment.

    Latin fragments need word boundaries so ``he`` does not accidentally match
    ``the``.  CJK text has no equivalent whitespace boundary, so its normalized
    substring is deliberately retained as a valid containment signal.
    """
    if not full_text or not fragment:
        return False
    start = full_text.find(fragment)
    if start < 0:
        return False
    if not any(character.isascii() and character.isalnum() for character in fragment):
        return True
    before = full_text[start - 1] if start else ""
    end = start + len(fragment)
    after = full_text[end] if end < len(full_text) else ""
    return (not before or not before.isalnum()) and (not after or not after.isalnum())


def _comparison_text_contains(full_text: str, fragment: str) -> bool:
    if not full_text or not fragment or len(full_text) < len(fragment):
        return False
    # Compact Latin comparison keys no longer have word boundaries. Keep the
    # existing protection against tiny fragments such as "he" matching "the".
    if any(character.isascii() and character.isalnum() for character in fragment) and len(fragment) < 3:
        return False
    return fragment in full_text


def _region_box(region: dict[str, float | str]) -> tuple[float, float, float, float] | None:
    try:
        left = float(region["x"])
        top = float(region["y"])
        width = float(region["width"])
        height = float(region["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0 or not all(math.isfinite(value) for value in (left, top, width, height)):
        return None
    return left, top, width, height


def _contained_area_ratio(inner: tuple[float, float, float, float], outer: tuple[float, float, float, float]) -> float:
    inner_left, inner_top, inner_width, inner_height = inner
    outer_left, outer_top, outer_width, outer_height = outer
    overlap_width = max(0.0, min(inner_left + inner_width, outer_left + outer_width) - max(inner_left, outer_left))
    overlap_height = max(0.0, min(inner_top + inner_height, outer_top + outer_height) - max(inner_top, outer_top))
    return (overlap_width * overlap_height) / (inner_width * inner_height)


def _is_contained_duplicate(
    inner_box: tuple[float, float, float, float],
    inner_text: str,
    outer_box: tuple[float, float, float, float],
    outer_text: str,
) -> bool:
    containment = _contained_area_ratio(inner_box, outer_box)
    if containment < CONTAINED_REGION_OVERLAP_RATIO:
        return False
    exact_text_match = inner_text == outer_text or _text_contains_fragment(outer_text, inner_text)
    inner_comparison = _comparison_region_text(inner_text)
    outer_comparison = _comparison_region_text(outer_text)
    fuzzy_text_match = (
        inner_comparison == outer_comparison
        or _comparison_text_contains(outer_comparison, inner_comparison)
    )
    if exact_text_match or fuzzy_text_match:
        return True
    inner_area = inner_box[2] * inner_box[3]
    outer_area = outer_box[2] * outer_box[3]
    return (
        containment >= FUZZY_CONTAINED_REGION_OVERLAP_RATIO
        and inner_area <= outer_area * GEOMETRY_ONLY_MAX_AREA_RATIO
        # A one/two-character box can be a legitimate nearby caption. Without
        # a reliable text match, geometry alone is not strong enough to erase it.
        and len(inner_comparison) >= 3
    )


def discard_contained_ocr_regions(regions: list[dict[str, float | str]]) -> list[dict[str, float | str]]:
    """Keep only the complete target when an OCR result duplicates part of it.

    Grouping can leave both ``A complete sentence`` and a tight box containing
    ``complete sentence``.  The latter must not be sent to the translation
    provider or rendered as a second patch.  Text containment *and* at least
    85% geometric containment are required, preventing repeated dialogue in a
    nearby bubble from being removed merely because it uses the same phrase.
    """
    kept: list[dict[str, float | str]] = []
    normalized = [_normalized_region_text(region.get("text", "")) for region in regions]
    boxes = [_region_box(region) for region in regions]

    for index, region in enumerate(regions):
        inner_text = normalized[index]
        inner_box = boxes[index]
        if not inner_text or inner_box is None:
            kept.append(region)
            continue

        inner_area = inner_box[2] * inner_box[3]
        duplicate = False
        for other_index, other in enumerate(regions):
            if other_index == index:
                continue
            outer_text = normalized[other_index]
            outer_box = boxes[other_index]
            if not outer_text or outer_box is None:
                continue
            outer_area = outer_box[2] * outer_box[3]
            inner_comparison = _comparison_region_text(inner_text)
            outer_comparison = _comparison_region_text(outer_text)
            if inner_text == outer_text or inner_comparison == outer_comparison:
                # Equal text is only a duplicate when the competing box is at
                # least as large. On an exact area tie, keep the earliest OCR
                # result deterministically instead of allowing both to remove
                # one another.
                if outer_area < inner_area or (outer_area == inner_area and other_index > index):
                    continue
            elif not _is_contained_duplicate(inner_box, inner_text, outer_box, outer_text):
                continue
            if _is_contained_duplicate(inner_box, inner_text, outer_box, outer_text):
                duplicate = True
                break
        if not duplicate:
            kept.append(region)
    return kept


def _result_payload(result: Any) -> dict[str, Any] | None:
    """Normalize PaddleOCR 3 Result objects, dictionaries, and JSON payloads."""
    candidate = getattr(result, "json", result)
    if callable(candidate):
        candidate = candidate()
    if isinstance(candidate, str):
        try:
            candidate = json.loads(candidate)
        except json.JSONDecodeError:
            return None
    if not isinstance(candidate, dict):
        return None
    payload = candidate.get("res", candidate)
    return payload if isinstance(payload, dict) else None


def _region_from_points(points: Any, text: Any, score: Any) -> dict[str, float | str] | None:
    try:
        text = " ".join(str(text).split())
        confidence = float(score)
        # PaddleOCR 3 returns polygons shaped (4, 2). Some result variants return
        # a flat [x_min, y_min, x_max, y_max] box instead.
        if isinstance(points, (list, tuple, np.ndarray)) and len(points) == 4 and all(
            isinstance(value, (int, float, np.number)) for value in points
        ):
            xs = [float(points[0]), float(points[2])]
            ys = [float(points[1]), float(points[3])]
        else:
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
    except (IndexError, TypeError, ValueError):
        return None
    if not text or not xs or not ys:
        return None
    return {
        "x": min(xs),
        "y": min(ys),
        "width": max(1.0, max(xs) - min(xs)),
        "height": max(1.0, max(ys) - min(ys)),
        "text": text,
        "confidence": confidence,
    }


def _same_visual_line_score(
    first: dict[str, float | str], second: dict[str, float | str]
) -> float | None:
    """Return a score when Paddle split one visible line into adjacent boxes.

    Paddle's detector normally returns one region per text line, but comic
    lettering can make it split the final word (or a bold word) into a second
    region on the *same* baseline.  Treating those pieces as separate stacked
    lines leaves an orphan such as ``THE`` outside of its speech bubble.  This
    helper is intentionally much stricter horizontally than the vertical
    bubble grouping below, so two side-by-side bubbles are still separate.
    """
    first_x = float(first["x"])
    first_y = float(first["y"])
    first_width = float(first["width"])
    first_height = float(first["height"])
    second_x = float(second["x"])
    second_y = float(second["y"])
    second_width = float(second["width"])
    second_height = float(second["height"])

    first_center_y = first_y + first_height / 2
    second_center_y = second_y + second_height / 2
    line_height = max(first_height, second_height)
    center_distance = abs(second_center_y - first_center_y)
    # OCR polygons from the same printed line can be a few pixels taller or
    # lower than one another.  Do not let vertically neighbouring lines pass
    # this test, however.
    if center_distance > max(3.0, 0.52 * min(first_height, second_height)):
        return None

    if first_x <= second_x:
        left_x, left_width = first_x, first_width
        right_x = second_x
    else:
        left_x, left_width = second_x, second_width
        right_x = first_x
    horizontal_gap = right_x - (left_x + left_width)
    narrow_width = min(first_width, second_width)
    # A small polygon overlap is normal.  A broad overlap is almost always a
    # duplicate/decorative region rather than the next words in reading order.
    if horizontal_gap < -0.22 * narrow_width:
        return None
    max_horizontal_gap = max(12.0, 1.5 * line_height)
    if horizontal_gap > max_horizontal_gap:
        return None

    return max(0.0, horizontal_gap) / max_horizontal_gap + center_distance / max(3.0, line_height)


def _merge_same_visual_line_fragments(
    lines: list[dict[str, float | str | int]],
) -> list[dict[str, float | str | int]]:
    """Join adjacent OCR fragments which belong to one printed line.

    This is done before vertical grouping because a split line otherwise gives
    the greedy stacker two competing "last" lines.  Components are used rather
    than Paddle's result order: detector results are not guaranteed to be
    emitted left-to-right.
    """
    if len(lines) < 2:
        return lines

    parents = list(range(len(lines)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first_index: int, second_index: int) -> None:
        first_root = find(first_index)
        second_root = find(second_index)
        if first_root != second_root:
            parents[second_root] = first_root

    for first_index, first in enumerate(lines):
        for second_index in range(first_index + 1, len(lines)):
            if _same_visual_line_score(first, lines[second_index]) is not None:
                union(first_index, second_index)

    components: dict[int, list[dict[str, float | str | int]]] = {}
    for index, line in enumerate(lines):
        components.setdefault(find(index), []).append(line)

    merged: list[dict[str, float | str | int]] = []
    for members in components.values():
        if len(members) == 1:
            merged.append(members[0])
            continue
        members.sort(
            key=lambda member: (
                float(member["x"]),
                float(member["y"]),
                int(member["original_index"]),
            )
        )
        left = min(float(member["x"]) for member in members)
        top = min(float(member["y"]) for member in members)
        right = max(float(member["x"]) + float(member["width"]) for member in members)
        bottom = max(float(member["y"]) + float(member["height"]) for member in members)
        total_area = sum(float(member["width"]) * float(member["height"]) for member in members)
        confidence = sum(
            float(member["confidence"]) * float(member["width"]) * float(member["height"])
            for member in members
        ) / total_area
        merged.append(
            {
                "x": left,
                "y": top,
                "width": right - left,
                "height": bottom - top,
                "text": " ".join(str(member["text"]) for member in members),
                "confidence": confidence,
                "original_index": min(int(member["original_index"]) for member in members),
            }
        )

    return merged


def _stacked_line_score(previous: dict[str, float | str], current: dict[str, float | str]) -> float | None:
    """Return a smaller-is-better score when two OCR boxes look like stacked LTR lines.

    PaddleOCR detects text *lines*, not speech-bubble contours. This deliberately
    conservative geometric test joins only lines that are vertically adjacent and
    horizontally related, which handles wrapped English dialogue without making a
    full page of nearby captions into one hover target.
    """
    previous_x = float(previous["x"])
    previous_y = float(previous["y"])
    previous_width = float(previous["width"])
    previous_height = float(previous["height"])
    current_x = float(current["x"])
    current_y = float(current["y"])
    current_width = float(current["width"])
    current_height = float(current["height"])

    previous_bottom = previous_y + previous_height
    vertical_gap = current_y - previous_bottom
    line_height = max(previous_height, current_height)
    # Text lines in one English bubble normally have a much smaller separation
    # than their height. The 8px floor keeps small-font viewport captures usable.
    # Comic bubbles can use generous leading, especially around a short
    # centered line. A 1.9x limit keeps deliberately spaced dialogue together;
    # horizontal alignment still prevents a nearby caption/bubble from being
    # absorbed into the same translation target.
    max_vertical_gap = max(8.0, 1.9 * line_height)
    allowed_overlap = 0.25 * min(previous_height, current_height)
    if vertical_gap < -allowed_overlap or vertical_gap > max_vertical_gap:
        return None

    previous_right = previous_x + previous_width
    current_right = current_x + current_width
    horizontal_overlap = max(0.0, min(previous_right, current_right) - max(previous_x, current_x))
    narrow_width = min(previous_width, current_width)
    overlap_ratio = horizontal_overlap / narrow_width
    previous_center = previous_x + previous_width / 2
    current_center = current_x + current_width / 2
    center_distance = abs(current_center - previous_center)
    # A shorter wrapped line may be left-aligned or centred beneath a long line,
    # so permit either useful horizontal overlap or a modest centre displacement.
    max_center_distance = max(1.75 * line_height, 0.65 * max(previous_width, current_width))
    if overlap_ratio < 0.22 and center_distance > max_center_distance:
        return None

    # Prefer the closest prior line if more than one bubble is geometrically
    # plausible. The group index is used as a deterministic tie-breaker below.
    vertical_score = max(0.0, vertical_gap) / max_vertical_gap
    alignment_score = center_distance / max_center_distance
    return vertical_score + alignment_score - min(1.0, overlap_ratio) * 0.18


def _horizontal_gap(first: dict[str, float | str | int], second: dict[str, float | str | int]) -> float:
    """Distance between two boxes on the x-axis; negative means overlap."""
    first_left = float(first["x"])
    first_right = first_left + float(first["width"])
    second_left = float(second["x"])
    second_right = second_left + float(second["width"])
    return max(first_left, second_left) - min(first_right, second_right)


def _horizontally_related(
    first: dict[str, float | str | int], second: dict[str, float | str | int]
) -> float | None:
    """Return a score only for two boxes plausibly in the same bubble flow."""
    first_x = float(first["x"])
    first_width = float(first["width"])
    first_height = float(first["height"])
    second_x = float(second["x"])
    second_width = float(second["width"])
    second_height = float(second["height"])

    first_right = first_x + first_width
    second_right = second_x + second_width
    overlap = max(0.0, min(first_right, second_right) - max(first_x, second_x))
    overlap_ratio = overlap / min(first_width, second_width)
    gap = _horizontal_gap(first, second)
    center_distance = abs((first_x + first_width / 2) - (second_x + second_width / 2))
    max_width = max(first_width, second_width)
    near_touch = max(4.0, 0.35 * max(first_height, second_height))

    # Meaningful overlap is the normal wrapped-line case.  A short, offset
    # line can also touch its neighbour at an edge (common in hand-lettered
    # bubbles), but then its centre must still be within roughly one line's
    # width.  This rejects a caption/bubble sitting to the side.
    if overlap_ratio >= 0.08:
        return center_distance / max_width - min(1.0, overlap_ratio) * 0.1
    if gap <= near_touch and center_distance <= 1.05 * max_width:
        return gap / near_touch + center_distance / (1.05 * max_width)
    return None


def _interleaved_row_score(group: dict[str, Any], candidate: dict[str, float | str | int]) -> float | None:
    """Score an isolated row skipped between two rows of an existing group.

    A greedy top-to-bottom stack can legitimately add the first and third rows
    of one bubble while rejecting a very offset middle row.  Once both
    enclosing rows exist, their geometry is stronger evidence than either
    direct greedy comparison.  We only repair a *single* skipped visual row,
    require an enclosing row above and below, and retain both a horizontal
    group guard and a neighbouring-row guard to avoid absorbing nearby
    bubbles.
    """
    members: list[dict[str, float | str | int]] = group["lines"]
    candidate_center_y = float(candidate["y"]) + float(candidate["height"]) / 2
    above = [
        member
        for member in members
        if float(member["y"]) + float(member["height"]) / 2 < candidate_center_y
    ]
    below = [
        member
        for member in members
        if float(member["y"]) + float(member["height"]) / 2 > candidate_center_y
    ]
    if not above or not below:
        return None

    nearest_above = max(above, key=lambda member: float(member["y"]) + float(member["height"]) / 2)
    nearest_below = min(below, key=lambda member: float(member["y"]) + float(member["height"]) / 2)
    above_center_y = float(nearest_above["y"]) + float(nearest_above["height"]) / 2
    below_center_y = float(nearest_below["y"]) + float(nearest_below["height"]) / 2
    max_step = max(
        10.0,
        2.5
        * max(
            float(candidate["height"]),
            float(nearest_above["height"]),
            float(nearest_below["height"]),
        ),
    )
    above_distance = candidate_center_y - above_center_y
    below_distance = below_center_y - candidate_center_y
    if above_distance > max_step or below_distance > max_step:
        return None

    group_left = min(float(member["x"]) for member in members)
    group_right = max(float(member["x"]) + float(member["width"]) for member in members)
    group_height = max(float(member["height"]) for member in members)
    group_box: dict[str, float | str | int] = {
        "x": group_left,
        "y": min(float(member["y"]) for member in members),
        "width": group_right - group_left,
        "height": group_height,
        "text": "",
        "confidence": 1.0,
        "original_index": -1,
    }
    group_alignment = _horizontally_related(group_box, candidate)
    neighbour_alignment = min(
        (
            score
            for score in (
                _horizontally_related(nearest_above, candidate),
                _horizontally_related(nearest_below, candidate),
            )
            if score is not None
        ),
        default=None,
    )
    if group_alignment is None or neighbour_alignment is None:
        return None

    return (
        above_distance / max_step
        + below_distance / max_step
        + group_alignment
        + neighbour_alignment
    )


def _merge_interleaved_rows(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repair a single visual row that greedy line stacking skipped.

    The loop is intentionally narrow: it only moves a one-row group into a
    multi-row group that encloses it vertically.  That preserves separate
    speech bubbles/captions even when their boxes are nearby.
    """
    while True:
        candidates: list[tuple[float, int, int]] = []
        for candidate_index, candidate_group in enumerate(groups):
            candidate_lines: list[dict[str, float | str | int]] = candidate_group["lines"]
            if len(candidate_lines) != 1:
                continue
            candidate = candidate_lines[0]
            for target_index, target_group in enumerate(groups):
                if target_index == candidate_index or len(target_group["lines"]) < 2:
                    continue
                score = _interleaved_row_score(target_group, candidate)
                if score is not None:
                    candidates.append((score, target_index, candidate_index))
        if not candidates:
            return groups

        _, target_index, candidate_index = min(candidates, key=lambda candidate: candidate)
        target = groups[target_index]
        target["lines"].extend(groups[candidate_index]["lines"])
        target["last"] = max(
            target["lines"],
            key=lambda line: (
                float(line["y"]),
                float(line["x"]),
                int(line["original_index"]),
            ),
        )
        del groups[candidate_index]


# The lowered detector thresholds also pick up strokes of artwork, which the
# recognizer reads as a stray "-" or "Y" at low confidence. Such a fragment
# must not take part in grouping: placed between two bubbles it bridged them
# into one region, which then looked clipped and dropped *both* bubbles. A real
# wrapped line has more characters or a confident read, so it still joins.
NOISE_FRAGMENT_MAX_CHARACTERS = 2
NOISE_FRAGMENT_MAX_CONFIDENCE = 0.5


def _is_noise_fragment(text: str, confidence: float) -> bool:
    characters = sum(1 for character in text if character.isalnum())
    return characters <= NOISE_FRAGMENT_MAX_CHARACTERS and confidence < NOISE_FRAGMENT_MAX_CONFIDENCE


def group_ocr_regions(regions: list[dict[str, float | str]]) -> list[dict[str, float | str]]:
    """Merge nearby OCR text lines into conservative speech-bubble-sized regions.

    Returned regions retain the existing API shape. Their text is ordered top to
    bottom (and then left to right) and joined with spaces so the translation
    model receives one continuous wrapped utterance. The bounding box encloses every
    member line; confidence is an area-weighted mean, rather than discarding a
    slightly lower-confidence line before it can join its bubble.
    """
    lines: list[dict[str, float | str | int]] = []
    for original_index, region in enumerate(regions):
        try:
            x = float(region["x"])
            y = float(region["y"])
            width = float(region["width"])
            height = float(region["height"])
            confidence = float(region["confidence"])
        except (KeyError, TypeError, ValueError):
            continue
        text = " ".join(str(region.get("text", "")).split())
        if (
            not text
            or width <= 0
            or height <= 0
            or not all(math.isfinite(value) for value in (x, y, width, height, confidence))
        ):
            continue
        if _is_noise_fragment(text, confidence):
            continue
        lines.append(
            {
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "text": text,
                "confidence": confidence,
                "original_index": original_index,
            }
        )

    # First reconstruct one visual line if detector geometry split it into
    # pieces.  This is crucial for a wrapped sentence such as
    # "THE ENTRANCE TO" + "THE": the two pieces share a baseline, not a
    # vertical relationship, and therefore cannot be fixed by the stacker.
    lines = _merge_same_visual_line_fragments(lines)
    lines.sort(key=lambda line: (float(line["y"]), float(line["x"]), int(line["original_index"])))
    groups: list[dict[str, Any]] = []
    for line in lines:
        candidates: list[tuple[float, int]] = []
        for group_index, group in enumerate(groups):
            score = _stacked_line_score(group["last"], line)
            if score is not None:
                candidates.append((score, group_index))
        if candidates:
            _, group_index = min(candidates, key=lambda candidate: (candidate[0], candidate[1]))
            group = groups[group_index]
            group["lines"].append(line)
            group["last"] = line
        else:
            groups.append({"lines": [line], "last": line})

    groups = _merge_interleaved_rows(groups)
    grouped: list[dict[str, float | str]] = []
    for group in groups:
        members: list[dict[str, float | str | int]] = sorted(
            group["lines"],
            key=lambda member: (
                float(member["y"]),
                float(member["x"]),
                int(member["original_index"]),
            ),
        )
        left = min(float(member["x"]) for member in members)
        top = min(float(member["y"]) for member in members)
        right = max(float(member["x"]) + float(member["width"]) for member in members)
        bottom = max(float(member["y"]) + float(member["height"]) for member in members)
        total_area = sum(float(member["width"]) * float(member["height"]) for member in members)
        confidence = (
            sum(
                float(member["confidence"]) * float(member["width"]) * float(member["height"])
                for member in members
            )
            / total_area
        )
        grouped.append(
            {
                "x": left,
                "y": top,
                "width": right - left,
                "height": bottom - top,
                "text": " ".join(str(member["text"]) for member in members),
                "confidence": confidence,
            }
        )
    return grouped


def group_vertical_cjk_regions(regions: list[dict[str, float | str]]) -> list[dict[str, float | str]]:
    """Merge tightly stacked Japanese/Chinese OCR boxes in one vertical column.

    Horizontal CJK labels deliberately pass through unchanged. A candidate has
    to be clearly taller than it is wide, align with the previous column box,
    and be separated by only a small gap; this avoids joining two independent
    bubbles that happen to be above/below one another.
    """
    vertical: list[dict[str, float | str | int]] = []
    passthrough: list[dict[str, float | str]] = []
    for original_index, region in enumerate(regions):
        try:
            x = float(region["x"])
            y = float(region["y"])
            width = float(region["width"])
            height = float(region["height"])
            confidence = float(region["confidence"])
        except (KeyError, TypeError, ValueError):
            continue
        text = "".join(str(region.get("text", "")).split())
        if (
            not text
            or width <= 0
            or height <= 0
            or not all(math.isfinite(value) for value in (x, y, width, height, confidence))
        ):
            continue
        line: dict[str, float | str | int] = {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "text": text,
            "confidence": confidence,
            "original_index": original_index,
        }
        if height >= width * 1.15:
            vertical.append(line)
        else:
            passthrough.append({key: value for key, value in line.items() if key != "original_index"})

    vertical.sort(key=lambda line: (-float(line["x"]) - float(line["width"]) / 2, float(line["y"]), int(line["original_index"])))
    columns: list[list[dict[str, float | str | int]]] = []
    for line in vertical:
        line_center = float(line["x"]) + float(line["width"]) / 2
        matches: list[tuple[float, list[dict[str, float | str | int]]]] = []
        for column in columns:
            last = column[-1]
            last_center = float(last["x"]) + float(last["width"]) / 2
            center_delta = abs(line_center - last_center)
            width_scale = max(float(line["width"]), float(last["width"]))
            gap = float(line["y"]) - (float(last["y"]) + float(last["height"]))
            if center_delta > max(6.0, width_scale * 0.65):
                continue
            if gap < -min(float(line["height"]), float(last["height"])) * 0.35:
                continue
            if gap > max(36.0, width_scale * 3.0, float(last["height"]) * 1.15):
                continue
            matches.append((center_delta + max(0.0, gap), column))
        if matches:
            _, column = min(matches, key=lambda match: match[0])
            column.append(line)
        else:
            columns.append([line])

    grouped = list(passthrough)
    for column in columns:
        members = sorted(column, key=lambda member: (float(member["y"]), int(member["original_index"])))
        left = min(float(member["x"]) for member in members)
        top = min(float(member["y"]) for member in members)
        right = max(float(member["x"]) + float(member["width"]) for member in members)
        bottom = max(float(member["y"]) + float(member["height"]) for member in members)
        total_area = sum(float(member["width"]) * float(member["height"]) for member in members)
        confidence = sum(
            float(member["confidence"]) * float(member["width"]) * float(member["height"])
            for member in members
        ) / total_area
        grouped.append({
            "x": left,
            "y": top,
            "width": right - left,
            "height": bottom - top,
            "text": "".join(str(member["text"]) for member in members),
            "confidence": confidence,
        })
    return sorted(grouped, key=lambda item: (float(item["y"]), float(item["x"])))


#: Inference engines, fastest first. ONNX Runtime runs the very same official
#: PP-OCRv5 weights (PaddleX publishes an ``_onnx`` export of each model) about
#: 8x faster than Paddle Inference on this project's target CPU - measured
#: 3.6 s -> 0.44 s for a 1440x1300 synthetic comic viewport, with identical
#: recognized text. Paddle Inference cannot close that gap itself: its oneDNN
#: path still fails on the detector under Paddle 3.3's new IR. Paddle stays as
#: the fallback whenever the ONNX models cannot be fetched or loaded.
OCR_ENGINE_ONNX = "onnxruntime"
OCR_ENGINE_PADDLE = "paddle"
#: ``OCR_ENGINE`` in local-server/.env: ``auto`` (default), ``onnxruntime`` or
#: ``paddle``. ``auto`` tries ONNX Runtime and silently falls back to Paddle.
OCR_ENGINE_ENV = "OCR_ENGINE"
# Eight threads measured fastest for both engines on the target PC; four is
# ~10% slower and twelve is no faster for the detector.
OCR_CPU_THREADS = 8


def _ocr_engine_preference() -> str:
    preference = os.environ.get(OCR_ENGINE_ENV, "auto").strip().lower()
    return preference if preference in {"auto", OCR_ENGINE_ONNX, OCR_ENGINE_PADDLE} else "auto"


def _engine_probe_image() -> np.ndarray:
    """A tiny image with real glyphs, so a probe exercises detection *and* recognition."""
    image = Image.new("RGB", (160, 48), "white")
    ImageDraw.Draw(image).text((12, 14), "OK 123", fill="black")
    return np.asarray(image)


class PaddleOcrEngine:
    """Lazy PaddleOCR 3 loader optimized for repeated visible-screen OCR."""

    def __init__(self) -> None:
        self._readers: dict[str, Any] = {}
        self._engines: dict[str, str] = {}
        self._lock = Lock()
        self._inference_lock = Lock()

    @staticmethod
    def _reader_options(source_language: str, paddle_language: str, recognition_model: str) -> dict[str, Any]:
        return {
            "lang": paddle_language,
            "text_detection_model_name": "PP-OCRv5_mobile_det",
            "text_recognition_model_name": recognition_model,
            "device": "cpu",
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": source_language not in FAST_HORIZONTAL_OCR_LANGUAGES,
            "text_det_thresh": TEXT_DETECTION_THRESHOLD,
            "text_det_box_thresh": TEXT_DETECTION_BOX_THRESHOLD,
            "text_det_limit_side_len": (
                FAST_TEXT_DETECTION_LIMIT_SIDE_LEN
                if source_language in FAST_HORIZONTAL_OCR_LANGUAGES
                else CJK_TEXT_DETECTION_LIMIT_SIDE_LEN
            ),
            "text_det_limit_type": "max",
        }

    @staticmethod
    def _onnx_engine_options() -> dict[str, Any]:
        return {
            "engine": OCR_ENGINE_ONNX,
            # Severity 3 keeps ONNX Runtime's per-initializer load warnings out
            # of the server console; real errors still raise.
            "engine_config": {"intra_op_num_threads": OCR_CPU_THREADS, "log_severity_level": 3},
        }

    @staticmethod
    def _paddle_engine_options() -> dict[str, Any]:
        return {
            # Paddle 3.3's new-IR path currently fails for this detector with
            # MKLDNN enabled on the supported Intel runtime, so leave it
            # explicitly off.
            "enable_mkldnn": False,
            "cpu_threads": OCR_CPU_THREADS,
        }

    def _build_reader(self, source_language: str, paddle_language: str, recognition_model: str) -> tuple[Any, str]:
        from paddleocr import PaddleOCR

        options = self._reader_options(source_language, paddle_language, recognition_model)
        preference = _ocr_engine_preference()
        if preference != OCR_ENGINE_PADDLE:
            try:
                reader = PaddleOCR(**options, **self._onnx_engine_options())
                # A reader can construct fine and still fail on its first real
                # crop, so prove both models run before trusting it.
                list(reader.predict(_engine_probe_image()))
                return reader, OCR_ENGINE_ONNX
            except Exception as error:
                if preference == OCR_ENGINE_ONNX:
                    raise
                logger.warning(
                    "ONNX Runtime OCR is unavailable for %s (%s); using Paddle Inference instead.",
                    source_language,
                    type(error).__name__,
                )
        return PaddleOCR(**options, **self._paddle_engine_options()), OCR_ENGINE_PADDLE

    def _reader_for(self, source_language: str) -> Any:
        paddle_language = PADDLE_LANGUAGE_CODES.get(source_language)
        recognition_model = PADDLE_RECOGNITION_MODELS.get(source_language)
        if not paddle_language or not recognition_model:
            raise UnsupportedOcrLanguage(f"{source_language} OCR은 아직 지원하지 않습니다.")
        with self._lock:
            if paddle_language not in self._readers:
                try:
                    reader, engine = self._build_reader(source_language, paddle_language, recognition_model)
                except Exception as error:
                    raise RuntimeError(
                        "PaddleOCR 모델을 준비하지 못했습니다. 인터넷 연결과 Python/PaddlePaddle 설치를 확인하세요."
                    ) from error
                self._readers[paddle_language] = reader
                self._engines[paddle_language] = engine
            return self._readers[paddle_language]

    def _fall_back_to_paddle(self, source_language: str) -> Any:
        """Replace a failing ONNX reader for this language for the rest of the process."""
        paddle_language = PADDLE_LANGUAGE_CODES[source_language]
        recognition_model = PADDLE_RECOGNITION_MODELS[source_language]
        from paddleocr import PaddleOCR

        reader = PaddleOCR(
            **self._reader_options(source_language, paddle_language, recognition_model),
            **self._paddle_engine_options(),
        )
        with self._lock:
            self._readers[paddle_language] = reader
            self._engines[paddle_language] = OCR_ENGINE_PADDLE
        return reader

    def warmup(self, source_language: str) -> None:
        """Download, load, and run a tiny inference before the first visible-tab request."""
        self.recognize(Image.new("RGB", (64, 64), "white"), source_language)

    def loaded_languages(self) -> list[str]:
        reverse_codes = {paddle_code: app_code for app_code, paddle_code in PADDLE_LANGUAGE_CODES.items()}
        with self._lock:
            loaded = list(self._readers)
        return sorted(reverse_codes.get(language, language) for language in loaded)

    def loaded_engines(self) -> dict[str, str]:
        """Which inference engine each loaded OCR language actually runs on."""
        reverse_codes = {paddle_code: app_code for app_code, paddle_code in PADDLE_LANGUAGE_CODES.items()}
        with self._lock:
            return {reverse_codes.get(language, language): engine for language, engine in self._engines.items()}

    def _recognize_v3(self, reader: Any, image_array: np.ndarray) -> list[dict[str, float | str]]:
        regions: list[dict[str, float | str]] = []
        for result in reader.predict(image_array):
            payload = _result_payload(result)
            if not payload:
                continue
            texts = payload.get("rec_texts")
            scores = payload.get("rec_scores")
            polygons = payload.get("rec_polys")
            if texts is None:
                texts = []
            if scores is None:
                scores = []
            if polygons is None:
                polygons = payload.get("dt_polys")
            if polygons is None:
                polygons = []
            for points, text, score in zip(polygons, texts, scores, strict=False):
                region = _region_from_points(points, text, score)
                if region:
                    regions.append(region)
        return regions

    def recognize(self, image: Image.Image, source_language: str) -> list[dict[str, float | str]]:
        reader = self._reader_for(source_language)
        image_array = np.asarray(image.convert("RGB"))
        # PaddleOCR reader objects are shared between requests; serialize inference to
        # avoid model-state races and keep memory use predictable on a personal PC.
        with self._inference_lock:
            try:
                return self._recognize_v3(reader, image_array)
            except Exception as error:
                with self._lock:
                    engine = self._engines.get(PADDLE_LANGUAGE_CODES[source_language])
                if engine != OCR_ENGINE_ONNX or _ocr_engine_preference() == OCR_ENGINE_ONNX:
                    raise
                # The probe passed, so this is an input-specific ONNX failure.
                # Re-read this screen on Paddle rather than losing it.
                logger.warning(
                    "ONNX Runtime OCR failed for %s (%s); switching to Paddle Inference.",
                    source_language,
                    type(error).__name__,
                )
                try:
                    paddle_reader = self._fall_back_to_paddle(source_language)
                except Exception as fallback_error:
                    raise RuntimeError(
                        "PaddleOCR 모델을 준비하지 못했습니다. 인터넷 연결과 Python/PaddlePaddle 설치를 확인하세요."
                    ) from fallback_error
                return self._recognize_v3(paddle_reader, image_array)
