from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import WarmupRequest
from app.translation import (
    AzureTranslationProvider,
    DeepLTranslationProvider,
    ProviderRequestError,
    TranslationRuntimeError,
    TranslationService,
    azure_translate_url,
)


class FakeHttpClient:
    """Stands in for the pooled client and records what was sent."""

    def __init__(self, payload: object = None, status: int = 200, body: bytes | None = None) -> None:
        self._status = status
        self._body = body if body is not None else json.dumps(payload).encode("utf-8")
        self.calls: list[tuple[str, dict[str, str], bytes]] = []

    def post(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        self.calls.append((url, headers, body))
        return self._status, self._body

    @property
    def last(self) -> tuple[str, dict[str, str], bytes]:
        return self.calls[-1]


class ScriptedHttpClient:
    """Answers each request with the next scripted (status, payload)."""

    def __init__(self, responses: list[tuple[int, object]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], bytes]] = []

    def post(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        self.calls.append((url, headers, body))
        status, payload = self._responses.pop(0)
        return status, json.dumps(payload, ensure_ascii=False).encode("utf-8")


class FakeCloudProvider:
    def __init__(self, code: str, *, configured: bool = True, failure: Exception | None = None) -> None:
        self.code = code
        self.configured = configured
        self.available = configured
        self.failure = failure
        self.calls: list[list[str]] = []

    def translate_many(self, texts: list[str], _source: str, _target: str) -> list[str]:
        self.calls.append(list(texts))
        if self.failure is not None:
            raise self.failure
        return [f"{self.code}:{text}" for text in texts]


class TranslationProviderTests(unittest.TestCase):
    def test_azure_posts_a_batched_v3_request(self) -> None:
        provider = AzureTranslationProvider("azure-key", "koreacentral", "https://translator.example")
        client = FakeHttpClient([
            {"translations": [{"text": "안녕"}]},
            {"translations": [{"text": "잘 가"}]},
        ])
        with patch("app.translation._http_client", client):
            translated = provider.translate_many(["Hello", "Bye"], "en", "ko")

        url, headers, body = client.last
        self.assertEqual(translated, ["안녕", "잘 가"])
        self.assertEqual(url, "https://translator.example/translate?api-version=3.0&from=en&to=ko")
        self.assertEqual(json.loads(body.decode("utf-8")), [{"Text": "Hello"}, {"Text": "Bye"}])
        self.assertEqual(headers["Ocp-Apim-Subscription-Key"], "azure-key")
        self.assertEqual(headers["Ocp-Apim-Subscription-Region"], "koreacentral")

    def test_deepl_uses_the_free_endpoint_and_authorization_header(self) -> None:
        provider = DeepLTranslationProvider("deepl-key", "https://api-free.deepl.com/v2", webtoon_style=False)
        client = FakeHttpClient({"translations": [{"text": "안녕하세요"}]})
        with patch("app.translation._http_client", client):
            translated = provider.translate_many(["Hello"], "en", "ko")

        url, headers, body = client.last
        self.assertEqual(translated, ["안녕하세요"])
        self.assertEqual(url, "https://api-free.deepl.com/v2/translate")
        self.assertEqual(headers["Authorization"], "DeepL-Auth-Key deepl-key")
        self.assertEqual(
            json.loads(body.decode("utf-8")),
            {"text": ["Hello"], "source_lang": "EN", "target_lang": "KO"},
        )

    def test_deepl_quota_error_is_safe_and_actionable(self) -> None:
        provider = DeepLTranslationProvider("deepl-key")
        client = FakeHttpClient(status=456, body=b'{"message":"Quota exceeded"}')
        with patch("app.translation._http_client", client):
            with self.assertRaisesRegex(ProviderRequestError, "월간 번역 한도"):
                provider.translate_many(["Hello"], "en", "ko")

    def test_provider_error_never_leaks_the_request(self) -> None:
        provider = DeepLTranslationProvider("deepl-key")
        client = FakeHttpClient(status=401, body=b'{"message":"Wrong endpoint deepl-key"}')
        with patch("app.translation._http_client", client):
            with self.assertRaises(ProviderRequestError) as raised:
                provider.translate_many(["Hello"], "en", "ko")

        # 401/403/429/456 deliberately drop the provider's own text, which can
        # echo back the request that carried the key.
        self.assertNotIn("deepl-key", str(raised.exception))

    def test_unreachable_provider_becomes_a_connection_error(self) -> None:
        provider = DeepLTranslationProvider("deepl-key")

        class UnreachableClient:
            def post(self, *_args: object) -> tuple[int, bytes]:
                raise OSError("no route to host")

        with patch("app.translation._http_client", UnreachableClient()):
            with self.assertRaisesRegex(ProviderRequestError, "연결하지 못했습니다"):
                provider.translate_many(["Hello"], "en", "ko")

    def test_unreadable_response_becomes_a_provider_error(self) -> None:
        provider = DeepLTranslationProvider("deepl-key")
        client = FakeHttpClient(status=200, body=b"<html>not json</html>")
        with patch("app.translation._http_client", client):
            with self.assertRaisesRegex(ProviderRequestError, "응답을 읽지 못했습니다"):
                provider.translate_many(["Hello"], "en", "ko")

    def test_auto_prefers_deepl_before_azure(self) -> None:
        azure = FakeCloudProvider("azure")
        deepl = FakeCloudProvider("deepl")
        service = TranslationService(azure_provider=azure, deepl_provider=deepl)

        result = service.translate_many_with_provider(["Hello"], "en", "ko", [], "auto")

        self.assertEqual(result.translations, ["deepl:Hello"])
        self.assertEqual(result.selection.translation_provider, "deepl")
        self.assertIsNone(result.selection.provider_fallback_reason)
        self.assertEqual(azure.calls, [])
        self.assertEqual(deepl.calls, [["Hello"]])

    def test_auto_falls_back_to_azure_after_deepl_failure(self) -> None:
        azure = FakeCloudProvider("azure")
        deepl = FakeCloudProvider("deepl", failure=ProviderRequestError("deepl", "DeepL quota"))
        service = TranslationService(azure_provider=azure, deepl_provider=deepl)

        result = service.translate_many_with_provider(["Hello"], "en", "ko", [], "auto")

        self.assertEqual(result.translations, ["azure:Hello"])
        self.assertEqual(result.selection.translation_provider, "azure")
        self.assertIn("DeepL quota", result.selection.provider_fallback_reason or "")
        self.assertEqual(deepl.calls, [["Hello"]])
        self.assertEqual(azure.calls, [["Hello"]])

    def test_cloud_translation_cache_reuses_text_only_inside_the_same_episode_scope(self) -> None:
        azure = FakeCloudProvider("azure")
        service = TranslationService(azure_provider=azure, deepl_provider=FakeCloudProvider("deepl", configured=False))

        first = service.translate_many_with_provider(
            ["Same bubble"], "en", "ko", [], "azure", cache_scope="https://comic.test/chapter/1"
        )
        repeated = service.translate_many_with_provider(
            ["Same   bubble"], "en", "ko", [], "azure", cache_scope="https://comic.test/chapter/1"
        )
        next_episode = service.translate_many_with_provider(
            ["Same bubble"], "en", "ko", [], "azure", cache_scope="https://comic.test/chapter/2"
        )

        self.assertEqual(first.translations, ["azure:Same bubble"])
        self.assertEqual(repeated.translations, ["azure:Same bubble"])
        self.assertEqual(next_episode.translations, ["azure:Same bubble"])
        self.assertEqual(azure.calls, [["Same bubble"], ["Same bubble"]])

    def test_cloud_batch_deduplicates_equal_ocr_texts_before_the_first_request(self) -> None:
        azure = FakeCloudProvider("azure")
        service = TranslationService(azure_provider=azure, deepl_provider=FakeCloudProvider("deepl", configured=False))

        result = service.translate_many_with_provider(
            ["Same bubble", "Same   bubble", "Another bubble", "Same bubble"],
            "en",
            "ko",
            [],
            "azure",
            cache_scope="https://comic.test/chapter/1",
        )

        self.assertEqual(result.translations, ["azure:Same bubble", "azure:Same bubble", "azure:Another bubble", "azure:Same bubble"])
        self.assertEqual(azure.calls, [["Same bubble", "Another bubble"]])

    def test_glossary_only_sends_matching_bubbles_individually(self) -> None:
        azure = FakeCloudProvider("azure")
        service = TranslationService(azure_provider=azure, deepl_provider=FakeCloudProvider("deepl", configured=False))
        glossary = [
            {
                "source": "Hero",
                "target": "HeroName",
                "sourceLanguage": "en",
                "targetLanguage": "ko",
            }
        ]

        result = service.translate_many_with_provider(
            ["First plain bubble", "Hero arrives", "Second plain bubble"],
            "en",
            "ko",
            glossary,
            "azure",
            cache_scope="https://comic.test/chapter/1",
        )

        self.assertEqual(
            result.translations,
            ["azure:First plain bubble", "azure:HeroName arrives", "azure:Second plain bubble"],
        )
        self.assertEqual(
            azure.calls,
            [["First plain bubble", "Second plain bubble"], ["[[WTTERM_0000_X]] arrives"]],
        )

    def test_cloud_translation_cache_separates_glossary_variants(self) -> None:
        azure = FakeCloudProvider("azure")
        service = TranslationService(azure_provider=azure, deepl_provider=FakeCloudProvider("deepl", configured=False))
        first_glossary = [
            {
                "source": "Hero",
                "target": "철수",
                "sourceLanguage": "en",
                "targetLanguage": "ko",
            }
        ]
        changed_glossary = [{**first_glossary[0], "target": "영웅"}]

        first = service.translate_many_with_provider(
            ["Hero arrives"], "en", "ko", first_glossary, "azure", cache_scope="https://comic.test/chapter/1"
        )
        changed = service.translate_many_with_provider(
            ["Hero arrives"], "en", "ko", changed_glossary, "azure", cache_scope="https://comic.test/chapter/1"
        )

        self.assertEqual(first.translations, ["azure:철수 arrives"])
        self.assertEqual(changed.translations, ["azure:영웅 arrives"])
        self.assertEqual(len(azure.calls), 2)

    def test_explicit_azure_falls_back_to_deepl_only(self) -> None:
        azure = FakeCloudProvider("azure", failure=ProviderRequestError("azure", "Azure 한도 초과"))
        deepl = FakeCloudProvider("deepl")
        service = TranslationService(azure_provider=azure, deepl_provider=deepl)
        result = service.translate_many_with_provider(["Hello"], "en", "ko", [], "azure")

        self.assertEqual(result.translations, ["deepl:Hello"])
        self.assertEqual(result.selection.translation_provider, "deepl")
        self.assertEqual(azure.calls, [["Hello"]])
        self.assertEqual(deepl.calls, [["Hello"]])

    def test_explicit_deepl_falls_back_to_azure_only(self) -> None:
        azure = FakeCloudProvider("azure")
        deepl = FakeCloudProvider("deepl", failure=ProviderRequestError("deepl", "DeepL quota"))
        service = TranslationService(azure_provider=azure, deepl_provider=deepl)

        result = service.translate_many_with_provider(["Hello"], "en", "ko", [], "deepl")

        self.assertEqual(result.translations, ["azure:Hello"])
        self.assertEqual(result.selection.translation_provider, "azure")
        self.assertEqual(deepl.calls, [["Hello"]])
        self.assertEqual(azure.calls, [["Hello"]])

    def test_argos_is_rejected_before_any_translation_attempt(self) -> None:
        service = TranslationService(azure_provider=FakeCloudProvider("azure"), deepl_provider=FakeCloudProvider("deepl"))

        with self.assertRaises(TranslationRuntimeError):
            service.translate_many_with_provider(["Hello"], "en", "ko", [], "argos")

    def test_api_model_rejects_argos_provider(self) -> None:
        with self.assertRaises(ValidationError):
            WarmupRequest(sourceLanguage="en", targetLanguage="ko", translationProvider="argos")

    def test_deepl_adds_webtoon_style_only_for_checked_target_languages(self) -> None:
        provider = DeepLTranslationProvider("deepl-key", webtoon_style=True)
        client = FakeHttpClient({"translations": [{"text": "번역"}]})
        with patch("app.translation._http_client", client):
            provider.translate_many(["Hello"], "en", "ko")
            korean = json.loads(client.last[2].decode("utf-8"))
            provider.translate_many(["Hello"], "en", "ja")
            japanese = json.loads(client.last[2].decode("utf-8"))

        self.assertIn("반말", " ".join(korean["custom_instructions"]))
        self.assertTrue(all(len(item) <= 300 for item in korean["custom_instructions"]))
        self.assertNotIn("custom_instructions", japanese)

    def test_deepl_receives_earlier_bubbles_of_the_episode_as_context(self) -> None:
        deepl = DeepLTranslationProvider("deepl-key", webtoon_style=False)
        service = TranslationService(azure_provider=FakeCloudProvider("azure", configured=False), deepl_provider=deepl)
        client = ScriptedHttpClient(
            [
                (200, {"translations": [{"text": "괜찮아?"}]}),
                (200, {"translations": [{"text": "괜찮다고 했잖아!"}, {"text": "쳇."}]}),
                (200, {"translations": [{"text": "다음 화"}]}),
            ]
        )
        with patch("app.translation._http_client", client):
            service.translate_many_with_provider(["ARE YOU OKAY?"], "en", "ko", [], "deepl", cache_scope="ep1")
            service.translate_many_with_provider(["I SAID I'M FINE!", "TCH."], "en", "ko", [], "deepl", cache_scope="ep1")
            service.translate_many_with_provider(["NEXT"], "en", "ko", [], "deepl", cache_scope="ep2")

        first, second, next_episode = (json.loads(body.decode("utf-8")) for _url, _headers, body in client.calls)
        # A lone bubble is no context for itself.
        self.assertNotIn("context", first)
        self.assertEqual(second["context"], "ARE YOU OKAY?\nI SAID I'M FINE!\nTCH.")
        self.assertEqual(second["text"], ["I SAID I'M FINE!", "TCH."])
        # A new episode never sees the previous one's dialogue.
        self.assertNotIn("context", next_episode)

    def test_deepl_drops_optional_parameters_once_they_are_rejected(self) -> None:
        provider = DeepLTranslationProvider("deepl-key", webtoon_style=True)
        client = ScriptedHttpClient(
            [
                (400, {"message": "custom_instructions not supported"}),
                (200, {"translations": [{"text": "안녕"}]}),
                (200, {"translations": [{"text": "잘 가"}]}),
            ]
        )
        with patch("app.translation._http_client", client):
            first = provider.translate_many(["Hello"], "en", "ko", context="Hello\nBye")
            second = provider.translate_many(["Bye"], "en", "ko", context="Hello\nBye")

        bodies = [json.loads(body.decode("utf-8")) for _url, _headers, body in client.calls]
        self.assertEqual((first, second), (["안녕"], ["잘 가"]))
        self.assertIn("custom_instructions", bodies[0])
        self.assertEqual(bodies[1], {"text": ["Hello"], "source_lang": "EN", "target_lang": "KO"})
        self.assertEqual(bodies[2], {"text": ["Bye"], "source_lang": "EN", "target_lang": "KO"})

    def test_deepl_pins_glossary_terms_with_ignored_xml_tags(self) -> None:
        deepl = DeepLTranslationProvider("deepl-key", webtoon_style=False)
        service = TranslationService(azure_provider=FakeCloudProvider("azure", configured=False), deepl_provider=deepl)
        glossary = [{"source": "SEOUL", "target": "세울", "sourceLanguage": "en", "targetLanguage": "ko"}]
        client = FakeHttpClient(
            {"translations": [{"text": "<wtterm>세울</wtterm>이 공격받고 있다! &lt;경보&gt;"}, {"text": "도망쳐 &amp; 숨어!"}]}
        )
        with patch("app.translation._http_client", client):
            result = service.translate_many_with_provider(
                ["SEOUL IS UNDER ATTACK! <ALERT>", "RUN & HIDE!"], "en", "ko", glossary, "deepl", cache_scope="ep1"
            )

        body = json.loads(client.last[2].decode("utf-8"))
        self.assertEqual(result.translations, ["세울이 공격받고 있다! <경보>", "도망쳐 & 숨어!"])
        # Plain and protected bubbles share one request.
        self.assertEqual(body["text"], ["<wtterm>세울</wtterm> IS UNDER ATTACK! &lt;ALERT&gt;", "RUN &amp; HIDE!"])
        self.assertEqual(body["tag_handling"], "xml")
        self.assertEqual(body["ignore_tags"], ["wtterm"])
        self.assertEqual(body["context"], "SEOUL IS UNDER ATTACK! &lt;ALERT&gt;\nRUN &amp; HIDE!")
        self.assertEqual(len(client.calls), 1)

    def test_glossary_falls_back_to_markers_when_a_pinned_term_is_lost(self) -> None:
        deepl = DeepLTranslationProvider("deepl-key", webtoon_style=False)
        service = TranslationService(azure_provider=FakeCloudProvider("azure", configured=False), deepl_provider=deepl)
        glossary = [{"source": "SEOUL", "target": "세울", "sourceLanguage": "en", "targetLanguage": "ko"}]
        client = ScriptedHttpClient(
            [
                (200, {"translations": [{"text": "서울이 공격받고 있다!"}]}),
                (200, {"translations": [{"text": "[[WTTERM_0000_X]]이 공격받고 있다!"}]}),
            ]
        )
        with patch("app.translation._http_client", client):
            result = service.translate_many_with_provider(
                ["SEOUL IS UNDER ATTACK!"], "en", "ko", glossary, "deepl", cache_scope="ep1"
            )

        self.assertEqual(result.translations, ["세울이 공격받고 있다!"])
        self.assertEqual(len(client.calls), 2)

    def test_azure_pins_glossary_terms_with_the_dynamic_dictionary(self) -> None:
        azure = AzureTranslationProvider("azure-key", "koreacentral", "https://api.cognitive.microsofttranslator.com")
        service = TranslationService(azure_provider=azure, deepl_provider=FakeCloudProvider("deepl", configured=False))
        glossary = [{"source": "SEOUL", "target": "세울", "sourceLanguage": "en", "targetLanguage": "ko"}]
        client = FakeHttpClient([{"translations": [{"text": "내일 세울로 돌아갈 거야."}]}])
        with patch("app.translation._http_client", client):
            result = service.translate_many_with_provider(
                ["I'M GOING BACK TO SEOUL TOMORROW."], "en", "ko", glossary, "azure", cache_scope="ep1"
            )

        body = json.loads(client.last[2].decode("utf-8"))
        self.assertEqual(result.translations, ["내일 세울로 돌아갈 거야."])
        self.assertEqual(
            body,
            [{"Text": 'I\'M GOING BACK TO <mstrans:dictionary translation="세울">SEOUL</mstrans:dictionary> TOMORROW.'}],
        )

    def test_azure_resource_domain_endpoints_use_the_translator_path(self) -> None:
        cases = {
            "https://api.cognitive.microsofttranslator.com": "https://api.cognitive.microsofttranslator.com/translate",
            "https://my-res.cognitiveservices.azure.com/": "https://my-res.cognitiveservices.azure.com/translator/text/v3.0/translate",
            "https://my-res.cognitiveservices.azure.com/translator/text/v3.0": "https://my-res.cognitiveservices.azure.com/translator/text/v3.0/translate",
            "https://my-res.cognitiveservices.azure.com/translator/text/v3.0/translate?api-version=3.0": "https://my-res.cognitiveservices.azure.com/translator/text/v3.0/translate",
            "https://translator.example": "https://translator.example/translate",
        }
        for endpoint, expected in cases.items():
            with self.subTest(endpoint=endpoint):
                self.assertEqual(azure_translate_url(endpoint), expected)

    def test_azure_free_quota_exhaustion_is_named(self) -> None:
        provider = AzureTranslationProvider("azure-key", "koreacentral", "https://translator.example")
        client = FakeHttpClient(
            status=403, body=b'{"error":{"code":403001,"message":"The operation isn\'t allowed because the subscription has exceeded its free quota."}}'
        )
        with patch("app.translation._http_client", client):
            with self.assertRaisesRegex(ProviderRequestError, "200만 자"):
                provider.translate_many(["Hello"], "en", "ko")

    def test_warmup_requires_a_configured_cloud_provider(self) -> None:
        azure = FakeCloudProvider("azure", configured=False)
        deepl = FakeCloudProvider("deepl", configured=False)
        service = TranslationService(azure_provider=azure, deepl_provider=deepl)
        with self.assertRaises(TranslationRuntimeError):
            service.warmup_for_provider("en", "ko", "auto")

        self.assertEqual(azure.calls, [])
        self.assertEqual(deepl.calls, [])


if __name__ == "__main__":
    unittest.main()
