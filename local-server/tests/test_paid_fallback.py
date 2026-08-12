from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import TranslateImageRequest, WarmupRequest
from app.paid_usage import PaidUsageLedger, configured_monthly_limit
from app.translation import (
    AzurePaidTranslationProvider,
    DeepLProTranslationProvider,
    MeteredPaidProvider,
    ProviderRequestError,
    TranslationRuntimeError,
    TranslationService,
)

from test_translation_providers import FakeCloudProvider


class MutableClock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class PaidUsageLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "usage.json"

    def test_spending_is_refused_once_the_monthly_limit_is_reached(self) -> None:
        ledger = PaidUsageLedger(self.path, monthly_limit=100)

        self.assertTrue(ledger.can_spend(60))
        ledger.record("deepl_pro", 60)
        self.assertTrue(ledger.can_spend(40))
        self.assertFalse(ledger.can_spend(41))
        self.assertEqual(ledger.remaining(), 40)

    def test_usage_survives_a_server_restart(self) -> None:
        first = PaidUsageLedger(self.path, monthly_limit=100)
        first.record("deepl_pro", 70)

        # A restart must not hand the reader a fresh budget.
        restarted = PaidUsageLedger(self.path, monthly_limit=100)
        self.assertEqual(restarted.remaining(), 30)
        self.assertFalse(restarted.can_spend(31))

    def test_a_new_calendar_month_resets_the_count(self) -> None:
        clock = MutableClock(datetime(2026, 8, 12, 10, 0, 0))
        ledger = PaidUsageLedger(self.path, monthly_limit=100, clock=clock)
        ledger.record("deepl_pro", 90)
        self.assertEqual(ledger.remaining(), 10)

        clock.moment = datetime(2026, 9, 1, 0, 5, 0)
        self.assertEqual(ledger.remaining(), 100)
        self.assertEqual(ledger.snapshot()["month"], "2026-09")

    def test_zero_limit_means_uncapped(self) -> None:
        ledger = PaidUsageLedger(self.path, monthly_limit=0)
        ledger.record("azure_paid", 10_000_000)

        self.assertTrue(ledger.can_spend(10_000_000))
        self.assertIsNone(ledger.remaining())
        self.assertFalse(ledger.snapshot()["limited"])

    def test_a_corrupt_ledger_file_does_not_block_translation(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        ledger = PaidUsageLedger(self.path, monthly_limit=100)

        self.assertTrue(ledger.can_spend(50))

    def test_a_typo_in_the_limit_falls_back_to_the_default_not_unlimited(self) -> None:
        with patch.dict("os.environ", {"PAID_MONTHLY_CHAR_LIMIT": "50만"}):
            self.assertEqual(configured_monthly_limit(), 500_000)


class PaidProviderConfigurationTests(unittest.TestCase):
    def test_a_free_deepl_key_is_not_accepted_as_a_pro_key(self) -> None:
        provider = DeepLProTranslationProvider("abcd-1234:fx")

        self.assertFalse(provider.configured)

    def test_a_pro_key_uses_the_paid_host(self) -> None:
        provider = DeepLProTranslationProvider("abcd-1234")

        self.assertTrue(provider.configured)
        self.assertEqual(provider.endpoint, "https://api.deepl.com/v2")
        self.assertEqual(provider.code, "deepl_pro")

    def test_the_paid_azure_resource_needs_its_own_key_and_region(self) -> None:
        self.assertFalse(AzurePaidTranslationProvider("key", "").configured)
        self.assertTrue(AzurePaidTranslationProvider("key", "koreacentral").configured)


class PaidFallbackChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.ledger = PaidUsageLedger(Path(self._directory.name) / "usage.json", monthly_limit=1_000)

    def _service(self, **overrides: object) -> tuple[TranslationService, dict[str, FakeCloudProvider]]:
        providers = {
            "azure": FakeCloudProvider("azure"),
            "deepl": FakeCloudProvider("deepl"),
            "deepl_pro": FakeCloudProvider("deepl_pro"),
            "azure_paid": FakeCloudProvider("azure_paid"),
        }
        providers.update(overrides)  # type: ignore[arg-type]
        service = TranslationService(
            azure_provider=providers["azure"],
            deepl_provider=providers["deepl"],
            paid_deepl_provider=providers["deepl_pro"],
            paid_azure_provider=providers["azure_paid"],
            paid_usage=self.ledger,
        )
        return service, providers

    def test_a_paid_engine_is_never_used_without_the_opt_in(self) -> None:
        service, providers = self._service(
            azure=FakeCloudProvider("azure", failure=ProviderRequestError("azure", "Azure 한도 초과")),
            deepl=FakeCloudProvider("deepl", failure=ProviderRequestError("deepl", "DeepL 한도 초과")),
        )

        with self.assertRaises(TranslationRuntimeError):
            service.translate_many_with_provider(["Hello"], "en", "ko", [], "auto")

        self.assertEqual(providers["deepl_pro"].calls, [])
        self.assertEqual(providers["azure_paid"].calls, [])

    def test_exhausting_both_free_engines_hands_over_to_the_paid_plan(self) -> None:
        service, providers = self._service(
            azure=FakeCloudProvider("azure", failure=ProviderRequestError("azure", "Azure 한도 초과")),
            deepl=FakeCloudProvider("deepl", failure=ProviderRequestError("deepl", "DeepL 월간 번역 한도 초과")),
        )

        result = service.translate_many_with_provider(
            ["Hello"], "en", "ko", [], "auto", allow_paid_fallback=True
        )

        self.assertEqual(result.translations, ["deepl_pro:Hello"])
        self.assertEqual(result.selection.translation_provider, "deepl_pro")
        self.assertTrue(result.selection.uses_paid_api)
        self.assertIn("유료 API 사용량이 발생합니다", result.selection.provider_fallback_reason or "")
        # The free engines were both really attempted before any money was spent.
        self.assertEqual(providers["deepl"].calls, [["Hello"]])
        self.assertEqual(providers["azure"].calls, [["Hello"]])
        self.assertEqual(providers["azure_paid"].calls, [])

    def test_a_working_free_engine_is_never_skipped_for_a_paid_one(self) -> None:
        service, providers = self._service()

        result = service.translate_many_with_provider(
            ["Hello"], "en", "ko", [], "auto", allow_paid_fallback=True
        )

        self.assertEqual(result.translations, ["deepl:Hello"])
        self.assertFalse(result.selection.uses_paid_api)
        self.assertIsNone(result.selection.provider_fallback_reason)
        self.assertEqual(providers["deepl_pro"].calls, [])
        self.assertEqual(self.ledger.snapshot()["usedCharacters"], 0)

    def test_the_paid_tier_keeps_the_engine_the_reader_chose(self) -> None:
        service, providers = self._service(
            azure=FakeCloudProvider("azure", failure=ProviderRequestError("azure", "Azure 한도 초과")),
            deepl=FakeCloudProvider("deepl", failure=ProviderRequestError("deepl", "DeepL 한도 초과")),
        )

        result = service.translate_many_with_provider(
            ["Hello"], "en", "ko", [], "azure", allow_paid_fallback=True
        )

        self.assertEqual(result.selection.translation_provider, "azure_paid")
        self.assertEqual(providers["deepl_pro"].calls, [])

    def test_paid_characters_are_counted_and_the_budget_stops_the_next_scan(self) -> None:
        service, providers = self._service(
            azure=FakeCloudProvider("azure", configured=False),
            deepl=FakeCloudProvider("deepl", configured=False),
            azure_paid=FakeCloudProvider("azure_paid", configured=False),
        )
        long_line = "A" * 600

        first = service.translate_many_with_provider(
            [long_line], "en", "ko", [], "auto", allow_paid_fallback=True, cache_scope="chapter/1"
        )
        self.assertEqual(first.selection.translation_provider, "deepl_pro")
        self.assertEqual(self.ledger.snapshot()["usedCharacters"], 600)

        # 600 + 600 crosses the 1,000-character monthly limit, so the second
        # screen must not reach the provider at all.
        with self.assertRaises(TranslationRuntimeError) as raised:
            service.translate_many_with_provider(
                ["B" * 600], "en", "ko", [], "auto", allow_paid_fallback=True, cache_scope="chapter/1"
            )

        self.assertIn("이번 달 사용 한도", str(raised.exception))
        self.assertEqual(len(providers["deepl_pro"].calls), 1)
        self.assertEqual(self.ledger.snapshot()["usedCharacters"], 600)

    def test_a_failed_paid_call_is_not_billed(self) -> None:
        provider = FakeCloudProvider("deepl_pro", failure=ProviderRequestError("deepl_pro", "일시 오류"))
        metered = MeteredPaidProvider(provider, self.ledger)

        with self.assertRaises(ProviderRequestError):
            metered.translate_many(["Hello"], "en", "ko")

        self.assertEqual(self.ledger.snapshot()["usedCharacters"], 0)

    def test_the_cache_keeps_a_rescanned_screen_off_the_paid_meter(self) -> None:
        service, providers = self._service(
            azure=FakeCloudProvider("azure", configured=False),
            deepl=FakeCloudProvider("deepl", configured=False),
        )

        for _ in range(3):
            service.translate_many_with_provider(
                ["Same bubble"], "en", "ko", [], "auto", allow_paid_fallback=True, cache_scope="chapter/1"
            )

        self.assertEqual(providers["deepl_pro"].calls, [["Same bubble"]])
        self.assertEqual(self.ledger.snapshot()["usedCharacters"], len("Same bubble"))

    def test_the_error_points_at_the_switch_when_paid_keys_are_ready_but_unused(self) -> None:
        service, _providers = self._service(
            azure=FakeCloudProvider("azure", failure=ProviderRequestError("azure", "Azure 한도 초과")),
            deepl=FakeCloudProvider("deepl", failure=ProviderRequestError("deepl", "DeepL 한도 초과")),
        )

        with self.assertRaises(TranslationRuntimeError) as raised:
            service.translate_many_with_provider(["Hello"], "en", "ko", [], "auto")

        self.assertIn("유료 API 전환을 켜면", str(raised.exception))

    def test_metadata_reports_paid_configuration_without_any_key(self) -> None:
        service, _providers = self._service()
        metadata = service.provider_metadata()

        self.assertEqual(sorted(metadata), ["azure", "azure_paid", "deepl", "deepl_pro"])
        self.assertTrue(service.paid_fallback_available())
        snapshot = service.paid_usage_snapshot()
        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["monthlyLimit"], 1_000)
        # Nothing in the snapshot may resemble credentials.
        self.assertEqual(
            sorted(snapshot),
            ["available", "byProvider", "limited", "month", "monthlyLimit", "remainingCharacters", "usedCharacters"],
        )

    def test_warmup_does_not_report_a_paid_engine_while_a_free_key_works(self) -> None:
        service, _providers = self._service()

        selection = service.warmup_for_provider("en", "ko", "auto", allow_paid_fallback=True)

        self.assertEqual(selection.translation_provider, "deepl")
        self.assertFalse(selection.uses_paid_api)


class PaidRequestModelTests(unittest.TestCase):
    def test_the_opt_in_defaults_to_off_on_both_endpoints(self) -> None:
        warmup = WarmupRequest(sourceLanguage="en", targetLanguage="ko")
        translate = TranslateImageRequest(
            image="x" * 64, sourceLanguage="en", targetLanguage="ko"
        )

        self.assertFalse(warmup.allowPaidFallback)
        self.assertFalse(translate.allowPaidFallback)

    def test_a_paid_engine_cannot_be_requested_directly(self) -> None:
        with self.assertRaises(Exception):
            WarmupRequest(sourceLanguage="en", targetLanguage="ko", translationProvider="deepl_pro")


if __name__ == "__main__":
    unittest.main()
