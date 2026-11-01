from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.glossary import (
    GLOSSARY_MARKUP_MSTRANS,
    GLOSSARY_MARKUP_XML,
    entries_for_language_pair,
    find_matches,
    translate_with_glossary,
    unwrap_glossary_terms,
    wrap_glossary_terms,
)


def entry(source: str, target: str, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "source": source,
        "target": target,
        "sourceLanguage": "en",
        "targetLanguage": "ko",
        "enabled": True,
        "caseSensitive": True,
        "wholeWord": True,
        "priority": 100,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class GlossaryTests(unittest.TestCase):
    def test_case_sensitive_entry_forces_user_translation(self) -> None:
        output = translate_with_glossary(
            "SEOUL is beautiful.",
            [entry("SEOUL", "세울")],
            lambda text: text,
        )
        self.assertEqual(output, "세울 is beautiful.")

    def test_case_sensitive_rule_does_not_match_lowercase_word(self) -> None:
        matches = find_matches("Seoul and SEOUL", [entry("SEOUL", "세울")])
        self.assertEqual([(match.start, match.end) for match in matches], [(10, 15)])

    def test_whole_word_rule_does_not_change_longer_word(self) -> None:
        matches = find_matches("SEOUL SEOULITE", [entry("SEOUL", "세울")])
        self.assertEqual(len(matches), 1)

    def test_cjk_name_matches_when_followed_by_a_particle(self) -> None:
        matches = find_matches("철수야, 여기 와.", [entry("철수", "철수", sourceLanguage="ko")])
        self.assertEqual(len(matches), 1)

    def test_fallback_keeps_forced_term_when_model_changes_marker(self) -> None:
        output = translate_with_glossary(
            "Visit SEOUL now",
            [entry("SEOUL", "세울")],
            lambda _text: "번역문",
        )
        self.assertIn("세울", output)

    def test_higher_priority_overlapping_rule_wins(self) -> None:
        matches = find_matches(
            "New York",
            [entry("York", "요크", priority=10), entry("New York", "뉴욕", priority=100)],
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].target, "뉴욕")

    def test_entry_only_applies_to_its_selected_language_pair(self) -> None:
        entries = [entry("SEOUL", "세울"), entry("SEOUL", "ソウル", targetLanguage="ja")]
        active = entries_for_language_pair(entries, "en", "ko")
        self.assertEqual([item.target for item in active], ["세울"])

    def test_xml_markup_round_trips_targets_and_escaped_text(self) -> None:
        text = "SEOUL & JINWOO <3"
        matches = find_matches(text, [entry("SEOUL", "세울"), entry("JINWOO", "진우")])
        wrapped = wrap_glossary_terms(text, matches, GLOSSARY_MARKUP_XML)
        self.assertEqual(wrapped, "<wtterm>세울</wtterm> &amp; <wtterm>진우</wtterm> &lt;3")
        restored = unwrap_glossary_terms("<wtterm>진우</wtterm>와 <wtterm>세울</wtterm> &lt;3", matches, GLOSSARY_MARKUP_XML)
        self.assertEqual(restored, "진우와 세울 <3")

    def test_xml_markup_rejects_a_translation_that_lost_a_term(self) -> None:
        matches = find_matches("SEOUL and JINWOO", [entry("SEOUL", "세울"), entry("JINWOO", "진우")])
        self.assertIsNone(unwrap_glossary_terms("<wtterm>세울</wtterm>과 진수", matches, GLOSSARY_MARKUP_XML))

    def test_dynamic_dictionary_markup_keeps_the_matched_source(self) -> None:
        text = "Back to Seoul."
        matches = find_matches(text, [entry("SEOUL", "세울", caseSensitive=False)])
        wrapped = wrap_glossary_terms(text, matches, GLOSSARY_MARKUP_MSTRANS)
        self.assertEqual(wrapped, 'Back to <mstrans:dictionary translation="세울">Seoul</mstrans:dictionary>.')
        self.assertEqual(unwrap_glossary_terms("세울로 돌아가.", matches, GLOSSARY_MARKUP_MSTRANS), "세울로 돌아가.")
        self.assertIsNone(unwrap_glossary_terms("서울로 돌아가.", matches, GLOSSARY_MARKUP_MSTRANS))

    def test_dynamic_dictionary_markup_refuses_unsafe_characters(self) -> None:
        matches = find_matches("SEOUL <3", [entry("SEOUL", "세울")])
        self.assertIsNone(wrap_glossary_terms("SEOUL <3", matches, GLOSSARY_MARKUP_MSTRANS))
        quoted = find_matches("SEOUL", [entry("SEOUL", 'the "city"')])
        self.assertIsNone(wrap_glossary_terms("SEOUL", quoted, GLOSSARY_MARKUP_MSTRANS))


if __name__ == "__main__":
    unittest.main()
