from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class GlossaryMatch:
    start: int
    end: int
    target: str


@dataclass(frozen=True)
class ProtectedTerm:
    marker: str
    target: str


def _value(entry: Any, name: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def entries_for_language_pair(entries: Iterable[Any], source_language: str, target_language: str) -> list[Any]:
    """Keep enabled entries for the active pair, in conflict-resolution order."""
    selected = [
        entry
        for entry in entries
        if _value(entry, "enabled", True)
        and _value(entry, "sourceLanguage") == source_language
        and _value(entry, "targetLanguage") == target_language
        and str(_value(entry, "source", "")).strip()
    ]
    return sorted(
        selected,
        key=lambda entry: (int(_value(entry, "priority", 0)), len(str(_value(entry, "source", "")))),
        reverse=True,
    )


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", text))


def find_matches(text: str, entries: Iterable[Any]) -> list[GlossaryMatch]:
    """Find non-overlapping glossary hits; high priority and longer terms win."""
    selected: list[GlossaryMatch] = []
    ordered_entries = sorted(
        entries,
        key=lambda entry: (int(_value(entry, "priority", 0)), len(str(_value(entry, "source", "")))),
        reverse=True,
    )
    for entry in ordered_entries:
        source = str(_value(entry, "source", "")).strip()
        if not source:
            continue
        flags = 0 if _value(entry, "caseSensitive", False) else re.IGNORECASE
        pattern = re.escape(source)
        # Korean, Japanese, and Chinese do not have reliable whitespace word
        # boundaries. A named entity such as "철수" should still match "철수야".
        if _value(entry, "wholeWord", True) and not _contains_cjk(source):
            pattern = rf"(?<!\w){pattern}(?!\w)"
        for hit in re.finditer(pattern, text, flags):
            if any(hit.start() < existing.end and hit.end() > existing.start for existing in selected):
                continue
            selected.append(GlossaryMatch(hit.start(), hit.end(), str(_value(entry, "target", ""))))
    return sorted(selected, key=lambda item: item.start)


def protect_terms(text: str, entries: Iterable[Any]) -> tuple[str, list[ProtectedTerm], list[tuple[bool, str]]]:
    """Replace glossary matches with markers and retain a no-loss fallback split."""
    matches = find_matches(text, entries)
    if not matches:
        return text, [], [(False, text)]

    cursor = 0
    pieces: list[str] = []
    protected: list[ProtectedTerm] = []
    fallback_segments: list[tuple[bool, str]] = []
    for index, match in enumerate(matches):
        before = text[cursor : match.start]
        if before:
            pieces.append(before)
            fallback_segments.append((False, before))
        marker = f"[[WTTERM_{index:04d}_X]]"
        pieces.append(marker)
        protected.append(ProtectedTerm(marker, match.target))
        fallback_segments.append((True, match.target))
        cursor = match.end
    after = text[cursor:]
    if after:
        pieces.append(after)
        fallback_segments.append((False, after))
    return "".join(pieces), protected, fallback_segments


def restore_markers(translated: str, protected: Iterable[ProtectedTerm]) -> str | None:
    """Return None if the model altered any marker, so callers can use the safe fallback."""
    result = translated
    for item in protected:
        if item.marker not in result:
            return None
        result = result.replace(item.marker, item.target)
    return result


def translate_with_glossary(
    text: str,
    entries: Iterable[Any],
    translate_segment: Callable[[str], str],
) -> str:
    """Translate text while making user-defined names take precedence over the model."""
    protected_text, protected, fallback_segments = protect_terms(text, entries)
    if not protected:
        return translate_segment(text)

    restored = restore_markers(translate_segment(protected_text), protected)
    if restored is not None:
        return restored

    # Some sequence-to-sequence models mutate even deliberately unusual markers.
    # Translate only the surrounding fragments in that rare case; target terms stay exact.
    return "".join(target if literal else translate_segment(target) for literal, target in fallback_segments)
