from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import http.client
import json
import os
import unicodedata
from threading import Lock
from typing import Any, Iterable
from urllib.parse import urlencode, urlsplit

from .glossary import entries_for_language_pair, find_matches, translate_with_glossary
from .paid_usage import PaidUsageLedger
from .runtime import configure_local_runtime

# Configure local state before Argos is ever imported. API keys from .env are
# loaded here as well, so they never need to be stored in the browser extension.
configure_local_runtime()
# Webtoon speech bubbles are short. Force the offline OpenNMT/MiniSBD path before
# Argos is imported: it avoids a first-translation Stanza resource download.
os.environ["ARGOS_CHUNK_TYPE"] = "MINISBD"
os.environ["ARGOS_DEVICE_TYPE"] = "cpu"
os.environ["ARGOS_MODEL_PROVIDER"] = "OPENNMT"


# Argos remains installed for existing local model data and maintenance tools,
# but the webtoon translation service deliberately never selects it. Cloud
# providers are the only supported runtime engines.
VALID_TRANSLATION_PROVIDERS = frozenset({"auto", "azure", "deepl"})

# The paid plans are a fallback tier, never something the reader picks from the
# engine list. Selecting "DeepL" means "the DeepL wording I chose"; whether that
# ends up costing money is decided by the free quota and the opt-in, not by the
# dropdown.
PAID_TRANSLATION_PROVIDERS = frozenset({"deepl_pro", "azure_paid"})
RESOLVED_TRANSLATION_PROVIDERS = VALID_TRANSLATION_PROVIDERS | PAID_TRANSLATION_PROVIDERS

#: The paid plan that continues a given free engine's wording.
PAID_EQUIVALENT_PROVIDERS = {"deepl": "deepl_pro", "azure": "azure_paid"}


def is_paid_provider(code: str | None) -> bool:
    return str(code or "") in PAID_TRANSLATION_PROVIDERS


def _entry_value(entry: object, name: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _normalized_cache_text(text: str) -> str:
    """Make harmless OCR whitespace variants reuse the same translated result."""
    return " ".join(unicodedata.normalize("NFKC", str(text)).split())


def _glossary_cache_key(entries: Iterable[object], source_language: str, target_language: str) -> str:
    """Fingerprint every glossary attribute that can change translation output."""
    active_entries = entries_for_language_pair(entries, source_language, target_language)
    payload = [
        {
            "source": str(_entry_value(entry, "source", "")),
            "target": str(_entry_value(entry, "target", "")),
            "enabled": bool(_entry_value(entry, "enabled", True)),
            "caseSensitive": bool(_entry_value(entry, "caseSensitive", False)),
            "wholeWord": bool(_entry_value(entry, "wholeWord", True)),
            "priority": int(_entry_value(entry, "priority", 0)),
        }
        for entry in active_entries
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class MissingLanguagePair(RuntimeError):
    pass


class TranslationRuntimeError(RuntimeError):
    pass


class ProviderRequestError(TranslationRuntimeError):
    """A safe, user-facing cloud provider failure without credentials or raw bodies."""

    def __init__(self, provider: str, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


@dataclass(frozen=True)
class ProviderSelection:
    requested_provider: str
    translation_provider: str
    provider_fallback_reason: str | None = None
    #: True once the request is being served by a billed plan, so the extension
    #: can say so on screen instead of spending quietly.
    uses_paid_api: bool = False


@dataclass(frozen=True)
class ProviderTranslationResult:
    translations: list[str]
    selection: ProviderSelection


def _provider_label(provider: str) -> str:
    return {
        "azure": "Azure Translator",
        "deepl": "DeepL",
        "deepl_pro": "DeepL API Pro (유료)",
        "azure_paid": "Azure Translator (유료)",
    }.get(provider, provider)


def _normalized_provider(value: str | None) -> str:
    provider = str(value or "auto").strip().lower()
    if provider not in VALID_TRANSLATION_PROVIDERS:
        raise TranslationRuntimeError("지원하지 않는 번역 엔진입니다.")
    return provider


def _api_message(payload: bytes) -> str:
    """Read only a concise provider message; never expose request headers or keys."""
    if not payload:
        return ""
    try:
        parsed = json.loads(payload.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            value = error.get("message") or error.get("code")
            return str(value) if value else ""
        value = parsed.get("message") or parsed.get("detail")
        return str(value) if value else ""
    return ""


def _provider_http_error(provider: str, status: int, payload: bytes) -> ProviderRequestError:
    detail = _api_message(payload)
    status = int(status)
    label = _provider_label(provider)
    if provider == "deepl" and status == 456:
        message = "DeepL API Free의 월간 번역 한도를 초과했습니다. 다른 엔진으로 전환합니다."
    elif status == 429:
        message = f"{label}의 요청 한도에 도달했습니다. 잠시 후 다시 시도하거나 다른 엔진을 사용하세요."
    elif status == 401:
        message = f"{label} API 키를 확인하세요."
    elif status == 403:
        message = f"{label}을 사용할 수 없습니다. 무료 할당량, 리전 또는 API 권한을 확인하세요."
    elif status == 400:
        message = f"{label}이 이 언어 조합 또는 요청을 지원하지 않습니다."
    else:
        message = f"{label} 번역 요청에 실패했습니다."
    if detail and status not in {400, 401, 403, 429, 456}:
        message = f"{message} ({detail[:160]})"
    return ProviderRequestError(provider, message, status)


# A preferred cloud provider must fail quickly enough for the other provider to
# be useful during a visible-screen scan.
PROVIDER_REQUEST_TIMEOUT_SECONDS = 10


class ProviderConnectionError(OSError):
    """The request never reached the provider, so retrying it is safe."""


class KeepAliveHttpClient:
    """A tiny pooled HTTPS client for the translation providers.

    Opening a fresh connection per request costs a TCP round trip plus a full
    TLS handshake before any text is translated - measured at roughly 0.6 s to
    DeepL from here, paid on *every* screen the reader scrolls to. Holding one
    connection per host removes that from all but the first request.

    Providers drop idle keep-alive connections without warning, so a pooled
    connection that fails before a response arrives is retried once on a fresh
    one. A connection that already produced a response is never retried.
    """

    def __init__(self, connection_factory=None, timeout: float = PROVIDER_REQUEST_TIMEOUT_SECONDS) -> None:
        self._connection_factory = connection_factory or self._default_connection
        self._timeout = timeout
        self._lock = Lock()
        self._connections: dict[tuple[str, str, int | None], Any] = {}

    @staticmethod
    def _default_connection(scheme: str, host: str, port: int | None, timeout: float):
        if scheme == "http":
            return http.client.HTTPConnection(host, port, timeout=timeout)
        return http.client.HTTPSConnection(host, port, timeout=timeout)

    def _discard(self, key: tuple[str, str, int | None]) -> None:
        connection = self._connections.pop(key, None)
        if connection is None:
            return
        try:
            connection.close()
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            for key in list(self._connections):
                self._discard(key)

    def post(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        parts = urlsplit(url)
        if not parts.hostname:
            raise ProviderConnectionError(f"invalid provider url: {url}")
        key = (parts.scheme or "https", parts.hostname, parts.port)
        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"
        request_headers = {**headers, "Connection": "keep-alive"}

        with self._lock:
            last_error: Exception | None = None
            for _ in range(2):
                pooled = self._connections.get(key)
                connection = pooled or self._connection_factory(*key, self._timeout)
                try:
                    connection.request("POST", target, body=body, headers=request_headers)
                    response = connection.getresponse()
                    # The body must be drained before the connection can serve
                    # another request.
                    payload = response.read()
                except (http.client.HTTPException, OSError) as error:
                    self._discard(key)
                    try:
                        connection.close()
                    except OSError:
                        pass
                    last_error = error
                    if pooled is None:
                        # A brand new connection failed: the provider really is
                        # unreachable, so there is nothing to retry.
                        break
                    continue

                if getattr(response, "will_close", True):
                    self._discard(key)
                    try:
                        connection.close()
                    except OSError:
                        pass
                else:
                    self._connections[key] = connection
                return int(response.status), payload

        raise ProviderConnectionError(str(last_error) if last_error else "provider request failed")


_http_client = KeepAliveHttpClient()


def _post_json(provider: str, url: str, headers: dict[str, str], payload: object) -> object:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        status, response_body = _http_client.post(url, headers, body)
    except (TimeoutError, OSError) as error:
        raise ProviderRequestError(provider, f"{_provider_label(provider)}에 연결하지 못했습니다.") from error
    if status >= 400:
        raise _provider_http_error(provider, status, response_body)
    try:
        return json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderRequestError(provider, f"{_provider_label(provider)}의 응답을 읽지 못했습니다.") from error


class AzureTranslationProvider:
    code = "azure"
    unconfigured_message = "Azure Translator API 키와 리전을 local-server/.env에 설정하세요."

    def __init__(self, key: str | None = None, region: str | None = None, endpoint: str | None = None) -> None:
        self.key = str(key if key is not None else os.environ.get("AZURE_TRANSLATOR_KEY", "")).strip()
        self.region = str(region if region is not None else os.environ.get("AZURE_TRANSLATOR_REGION", "")).strip()
        self.endpoint = str(
            endpoint if endpoint is not None else os.environ.get("AZURE_TRANSLATOR_ENDPOINT", "https://api.cognitive.microsofttranslator.com")
        ).strip().rstrip("/")

    @property
    def configured(self) -> bool:
        # A free Translator resource is regional, so request both values instead
        # of letting a confusing authorization error reach the extension.
        return bool(self.key and self.region and self.endpoint)

    @property
    def available(self) -> bool:
        return self.configured

    @staticmethod
    def _language(code: str) -> str:
        return {"zh": "zh-Hans"}.get(code.lower(), code.lower())

    def translate_many(self, texts: Iterable[str], source_language: str, target_language: str) -> list[str]:
        values = list(texts)
        if not values or source_language == target_language:
            return values
        if not self.configured:
            raise ProviderRequestError(self.code, self.unconfigured_message)
        base_url = self.endpoint if self.endpoint.endswith("/translate") else f"{self.endpoint}/translate"
        query = urlencode(
            {
                "api-version": "3.0",
                "from": self._language(source_language),
                "to": self._language(target_language),
            }
        )
        headers = {
            "Ocp-Apim-Subscription-Key": self.key,
            "Ocp-Apim-Subscription-Region": self.region,
            "Content-Type": "application/json; charset=UTF-8",
        }
        response = _post_json(self.code, f"{base_url}?{query}", headers, [{"Text": text} for text in values])
        if not isinstance(response, list) or len(response) != len(values):
            raise ProviderRequestError(self.code, "Azure Translator의 번역 응답 형식이 올바르지 않습니다.")
        translated: list[str] = []
        for item in response:
            if not isinstance(item, dict):
                raise ProviderRequestError(self.code, "Azure Translator의 번역 응답 형식이 올바르지 않습니다.")
            translations = item.get("translations")
            if not isinstance(translations, list) or not translations or not isinstance(translations[0], dict):
                raise ProviderRequestError(self.code, "Azure Translator의 번역 결과가 비어 있습니다.")
            text = translations[0].get("text")
            if not isinstance(text, str):
                raise ProviderRequestError(self.code, "Azure Translator의 번역 결과가 올바르지 않습니다.")
            translated.append(text)
        return translated


class DeepLTranslationProvider:
    code = "deepl"
    unconfigured_message = "DeepL API 키를 local-server/.env에 설정하세요."

    def __init__(self, key: str | None = None, endpoint: str | None = None) -> None:
        self.key = str(key if key is not None else os.environ.get("DEEPL_AUTH_KEY", "")).strip()
        self.endpoint = str(
            endpoint if endpoint is not None else os.environ.get("DEEPL_API_ENDPOINT", "https://api-free.deepl.com/v2")
        ).strip().rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.key and self.endpoint)

    @property
    def available(self) -> bool:
        return self.configured

    @staticmethod
    def _source_language(code: str) -> str:
        return {"zh": "ZH", "pt": "PT"}.get(code.lower(), code.upper())

    @staticmethod
    def _target_language(code: str) -> str:
        return {"zh": "ZH", "pt": "PT-PT"}.get(code.lower(), code.upper())

    def translate_many(self, texts: Iterable[str], source_language: str, target_language: str) -> list[str]:
        values = list(texts)
        if not values or source_language == target_language:
            return values
        if not self.configured:
            raise ProviderRequestError(self.code, self.unconfigured_message)
        url = self.endpoint if self.endpoint.endswith("/translate") else f"{self.endpoint}/translate"
        response = _post_json(
            self.code,
            url,
            {
                "Authorization": f"DeepL-Auth-Key {self.key}",
                "Content-Type": "application/json",
            },
            {
                "text": values,
                "source_lang": self._source_language(source_language),
                "target_lang": self._target_language(target_language),
            },
        )
        if not isinstance(response, dict):
            raise ProviderRequestError(self.code, "DeepL의 번역 응답 형식이 올바르지 않습니다.")
        translations = response.get("translations")
        if not isinstance(translations, list) or len(translations) != len(values):
            raise ProviderRequestError(self.code, "DeepL의 번역 결과가 올바르지 않습니다.")
        translated: list[str] = []
        for item in translations:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise ProviderRequestError(self.code, "DeepL의 번역 결과가 올바르지 않습니다.")
            translated.append(item["text"])
        return translated


class DeepLProTranslationProvider(DeepLTranslationProvider):
    """The billed DeepL plan: same protocol, different host and key."""

    code = "deepl_pro"
    unconfigured_message = (
        "DeepL API Pro 키를 local-server/.env의 DEEPL_PRO_AUTH_KEY에 설정하세요. "
        "Free 키(:fx로 끝나는 키)는 유료 엔드포인트에서 사용할 수 없습니다."
    )

    def __init__(self, key: str | None = None, endpoint: str | None = None) -> None:
        super().__init__(
            key if key is not None else os.environ.get("DEEPL_PRO_AUTH_KEY", ""),
            endpoint if endpoint is not None else os.environ.get("DEEPL_PRO_API_ENDPOINT", "https://api.deepl.com/v2"),
        )

    @property
    def configured(self) -> bool:
        # A Free key ends in ":fx" and the Pro host rejects it. Reporting it as
        # unconfigured surfaces the real problem - "that is your Free key" -
        # instead of an authorization error arriving only once the free quota is
        # already gone and the reader expects the paid plan to take over.
        return bool(self.key) and not self.key.endswith(":fx") and bool(self.endpoint)


class AzurePaidTranslationProvider(AzureTranslationProvider):
    """A Translator resource on a billed tier, kept separate from the F0 one."""

    code = "azure_paid"
    unconfigured_message = (
        "유료 Azure Translator 키와 리전을 local-server/.env의 "
        "AZURE_TRANSLATOR_PAID_KEY, AZURE_TRANSLATOR_PAID_REGION에 설정하세요."
    )

    def __init__(self, key: str | None = None, region: str | None = None, endpoint: str | None = None) -> None:
        super().__init__(
            key if key is not None else os.environ.get("AZURE_TRANSLATOR_PAID_KEY", ""),
            region if region is not None else os.environ.get("AZURE_TRANSLATOR_PAID_REGION", ""),
            endpoint
            if endpoint is not None
            else os.environ.get("AZURE_TRANSLATOR_PAID_ENDPOINT", "https://api.cognitive.microsofttranslator.com"),
        )


class MeteredPaidProvider:
    """A paid provider that may only spend what the monthly budget still allows.

    The budget check sits here, immediately around the network call, so it
    counts exactly the characters that leave this machine: the episode cache and
    the duplicate-bubble collapse have already removed everything they can by
    the time ``translate_many`` is reached.
    """

    def __init__(self, provider: AzureTranslationProvider | DeepLTranslationProvider, ledger: PaidUsageLedger) -> None:
        self._provider = provider
        self._ledger = ledger

    @property
    def code(self) -> str:
        return self._provider.code

    @property
    def configured(self) -> bool:
        return self._provider.configured

    @property
    def available(self) -> bool:
        return self._provider.available

    def translate_many(self, texts: Iterable[str], source_language: str, target_language: str) -> list[str]:
        values = list(texts)
        characters = sum(len(text) for text in values)
        if not self._ledger.can_spend(characters):
            raise ProviderRequestError(
                self.code,
                f"{_provider_label(self.code)}의 이번 달 사용 한도({self._ledger.monthly_limit:,}자)에 도달했습니다. "
                "local-server/.env의 PAID_MONTHLY_CHAR_LIMIT를 올리거나 다음 달까지 기다려 주세요.",
            )
        translated = self._provider.translate_many(values, source_language, target_language)
        # Only a successful call is billed, so only a successful call is counted.
        self._ledger.record(self.code, characters)
        return translated


class TranslationService:
    """User-configured DeepL/Azure translators with cloud-only failover."""

    def __init__(
        self,
        azure_provider: AzureTranslationProvider | None = None,
        deepl_provider: DeepLTranslationProvider | None = None,
        paid_deepl_provider: DeepLTranslationProvider | None = None,
        paid_azure_provider: AzureTranslationProvider | None = None,
        paid_usage: PaidUsageLedger | None = None,
    ) -> None:
        # This is deliberately an in-memory, one-episode cache. A new cache scope
        # empties it rather than retaining dialogue from a previous chapter.
        self._cache: OrderedDict[tuple[str, str, str, str, str, str], str] = OrderedDict()
        self._cache_limit = 1200
        self._active_cache_scope: str | None = None
        self._lock = Lock()
        self._azure = azure_provider or AzureTranslationProvider()
        self._deepl = deepl_provider or DeepLTranslationProvider()
        self._paid_usage = paid_usage or PaidUsageLedger()
        self._paid_deepl = paid_deepl_provider or DeepLProTranslationProvider()
        self._paid_azure = paid_azure_provider or AzurePaidTranslationProvider()
        # Every paid call goes through the budget wrapper. There is no path to a
        # billed provider that skips it.
        self._providers: dict[str, object] = {
            "azure": self._azure,
            "deepl": self._deepl,
            "deepl_pro": MeteredPaidProvider(self._paid_deepl, self._paid_usage),
            "azure_paid": MeteredPaidProvider(self._paid_azure, self._paid_usage),
        }

    def _activate_cache_scope(self, cache_scope: str | None) -> str:
        scope = str(cache_scope or "legacy").strip() or "legacy"
        with self._lock:
            if scope != self._active_cache_scope:
                self._cache.clear()
                self._active_cache_scope = scope
        return scope

    @staticmethod
    def _cache_key(
        cache_scope: str,
        provider: str,
        source_language: str,
        target_language: str,
        glossary_key: str,
        text: str,
    ) -> tuple[str, str, str, str, str, str]:
        return (
            cache_scope,
            provider,
            source_language.strip().lower(),
            target_language.strip().lower(),
            glossary_key,
            _normalized_cache_text(text),
        )

    @staticmethod
    def _load_translation(source_language: str, target_language: str):
        """Get a direct or installed-pivot Argos translation without hiding runtime failures."""
        try:
            import argostranslate.translate
        except ImportError as error:
            raise TranslationRuntimeError("Argos Translate 패키지가 설치되어 있지 않습니다.") from error

        try:
            installed_languages = argostranslate.translate.get_installed_languages()
        except Exception as error:
            raise TranslationRuntimeError("설치된 Argos 번역 모델 목록을 읽지 못했습니다.") from error

        source = next((language for language in installed_languages if language.code == source_language), None)
        target = next((language for language in installed_languages if language.code == target_language), None)
        if source is None or target is None:
            raise MissingLanguagePair(
                f"{source_language} → {target_language} 번역 모델을 찾지 못했습니다. "
                f"local-server에서 python scripts/install_models.py --from {source_language} --to {target_language}를 실행하세요."
            )
        try:
            translation = source.get_translation(target)
        except Exception as error:
            raise MissingLanguagePair(
                f"{source_language} → {target_language} 번역 모델을 찾지 못했습니다. "
                f"local-server에서 python scripts/install_models.py --from {source_language} --to {target_language}를 실행하세요."
            ) from error
        if translation is None:
            raise MissingLanguagePair(
                f"{source_language} → {target_language} 번역 모델을 찾지 못했습니다. "
                f"local-server에서 python scripts/install_models.py --from {source_language} --to {target_language}를 실행하세요."
            )
        return translation

    @classmethod
    def ensure_language_pair(cls, source_language: str, target_language: str) -> None:
        if source_language != target_language:
            cls._load_translation(source_language, target_language)

    def warmup(self, source_language: str, target_language: str) -> None:
        """Initialize CTranslate2 and sentence splitting with a harmless short input."""
        if source_language == target_language:
            return
        with self._lock:
            translation = self._load_translation(source_language, target_language)
            try:
                translation.translate(".")
            except Exception as error:
                raise TranslationRuntimeError("Argos Translate 모델 초기화 중 오류가 발생했습니다.") from error

    @classmethod
    def has_language_pair(cls, source_language: str, target_language: str) -> bool:
        if source_language == target_language:
            return True
        try:
            cls._load_translation(source_language, target_language)
        except (MissingLanguagePair, TranslationRuntimeError):
            return False
        return True

    @classmethod
    def installed_language_pairs(cls) -> list[str]:
        """Expose only actually runnable pairs so the popup does not promise unavailable translation."""
        try:
            import argostranslate.translate

            languages = argostranslate.translate.get_installed_languages()
        except Exception:
            return []
        pairs: list[str] = []
        for source in languages:
            for target in languages:
                if source.code == target.code:
                    continue
                try:
                    if source.get_translation(target) is not None:
                        pairs.append(f"{source.code}->{target.code}")
                except Exception:
                    continue
        return sorted(set(pairs))

    def provider_metadata(self) -> dict[str, dict[str, bool]]:
        """Configuration state only; this endpoint never returns an API key."""
        return {
            code: {"configured": provider.configured, "available": provider.available}
            for code, provider in self._providers.items()
        }

    def paid_fallback_available(self) -> bool:
        """True when at least one paid plan could actually take over."""
        return any(self._providers[code].configured for code in PAID_TRANSLATION_PROVIDERS)

    def paid_usage_snapshot(self) -> dict[str, object]:
        return {"available": self.paid_fallback_available(), **self._paid_usage.snapshot()}

    def _provider(self, code: str):
        provider = self._providers.get(code)
        if provider is None:
            raise TranslationRuntimeError("지원하지 않는 번역 엔진입니다.")
        return provider

    @staticmethod
    def _candidate_names(requested_provider: str, allow_paid_fallback: bool = False) -> list[str]:
        requested = _normalized_provider(requested_provider)
        if requested == "auto":
            # DeepL is preferred for dialogue quality; Azure remains the cloud
            # fallback so an exhausted free quota does not stop the current scan.
            free = ["deepl", "azure"]
        else:
            # An explicit cloud selection may fall back only to the *other* cloud
            # provider. It can never route text through local Argos.
            free = [requested, "azure" if requested == "deepl" else "deepl"]
        if not allow_paid_fallback:
            return free
        # Every free option is exhausted before any money is spent, and the paid
        # tier repeats the same preference order so a reader who prefers DeepL
        # keeps DeepL's wording after the free quota is gone.
        return free + [PAID_EQUIVALENT_PROVIDERS[code] for code in free]

    @staticmethod
    def _selection(requested: str, used: str, failures: list[str]) -> ProviderSelection:
        paid = is_paid_provider(used)
        if not paid and requested == "auto" and used == "deepl" and not failures:
            reason = None
        elif not paid and requested == used and not failures:
            reason = None
        elif paid:
            prefix = " ".join(failures).strip()
            suffix = (
                f"무료 한도를 모두 사용해 {_provider_label(used)}로 전환했습니다. "
                "지금부터는 유료 API 사용량이 발생합니다."
            )
            reason = f"{prefix} {suffix}".strip()
        else:
            prefix = " ".join(failures).strip()
            suffix = f"{_provider_label(used)}로 전환했습니다."
            reason = f"{prefix} {suffix}".strip()
        return ProviderSelection(requested, used, reason, paid)

    def _raw_translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
        cache_scope: str,
        glossary_key: str,
    ) -> str:
        if not text.strip() or source_language == target_language:
            return text
        cache_key = self._cache_key(cache_scope, "argos", source_language, target_language, glossary_key, text)
        with self._lock:
            if cache_key in self._cache:
                self._cache.move_to_end(cache_key)
                return self._cache[cache_key]
            translation = self._load_translation(source_language, target_language)
            try:
                translated = translation.translate(text)
            except Exception as error:
                raise TranslationRuntimeError("Argos Translate 모델 실행 중 오류가 발생했습니다.") from error
            self._cache[cache_key] = translated
            if len(self._cache) > self._cache_limit:
                self._cache.popitem(last=False)
            return translated

    def _cached_cloud_translate_many(
        self,
        provider: AzureTranslationProvider | DeepLTranslationProvider | MeteredPaidProvider,
        texts: Iterable[str],
        source_language: str,
        target_language: str,
        cache_scope: str,
        glossary_key: str,
    ) -> list[str]:
        values = list(texts)
        if not values or source_language == target_language:
            return values
        result: list[str | None] = [None] * len(values)
        # A screen can contain repeated OCR strings (for example "..." or a
        # speaker label). Collapse equal cache keys before the first cloud call:
        # the cache is populated only after that call, so a plain missing list
        # would otherwise spend quota and wait for the same text repeatedly.
        missing_by_key: OrderedDict[tuple[str, str, str, str, str, str], tuple[str, list[int]]] = OrderedDict()
        with self._lock:
            for index, text in enumerate(values):
                key = self._cache_key(
                    cache_scope, provider.code, source_language, target_language, glossary_key, text
                )
                cached = self._cache.get(key)
                if cached is None:
                    pending = missing_by_key.get(key)
                    if pending is None:
                        missing_by_key[key] = (text, [index])
                    else:
                        pending[1].append(index)
                else:
                    self._cache.move_to_end(key)
                    result[index] = cached
        if missing_by_key:
            missing_items = list(missing_by_key.items())
            translated = provider.translate_many(
                [source_text for _key, (source_text, _indices) in missing_items], source_language, target_language
            )
            if len(translated) != len(missing_items):
                raise ProviderRequestError(provider.code, f"{_provider_label(provider.code)}의 번역 결과 수가 맞지 않습니다.")
            with self._lock:
                for (key, (_source_text, indices)), translation in zip(missing_items, translated, strict=True):
                    self._cache[key] = translation
                    self._cache.move_to_end(key)
                    for index in indices:
                        result[index] = translation
                while len(self._cache) > self._cache_limit:
                    self._cache.popitem(last=False)
        return [str(item) for item in result]

    def _translate_argos_many(
        self,
        texts: Iterable[str],
        source_language: str,
        target_language: str,
        glossary: Iterable[object],
        cache_scope: str,
        glossary_key: str,
    ) -> list[str]:
        return self.translate_many(texts, source_language, target_language, glossary, cache_scope, glossary_key)

    def _translate_cloud_many(
        self,
        provider: AzureTranslationProvider | DeepLTranslationProvider | MeteredPaidProvider,
        texts: Iterable[str],
        source_language: str,
        target_language: str,
        glossary: Iterable[object],
        cache_scope: str,
        glossary_key: str,
    ) -> list[str]:
        values = list(texts)
        active_entries = entries_for_language_pair(glossary, source_language, target_language)
        if not active_entries:
            return self._cached_cloud_translate_many(
                provider, values, source_language, target_language, cache_scope, glossary_key
            )
        # Most bubbles do not contain a protected term even when a glossary is
        # active. Keep those in one cloud batch; only the bubbles whose markers
        # need restoring take the per-bubble fallback-safe path.
        results: list[str | None] = [None] * len(values)
        plain_indices: list[int] = []
        plain_texts: list[str] = []
        protected_indices: list[int] = []
        for index, text in enumerate(values):
            if find_matches(text, active_entries):
                protected_indices.append(index)
            else:
                plain_indices.append(index)
                plain_texts.append(text)

        if plain_texts:
            plain_translations = self._cached_cloud_translate_many(
                provider, plain_texts, source_language, target_language, cache_scope, glossary_key
            )
            for index, translation in zip(plain_indices, plain_translations, strict=True):
                results[index] = translation

        for index in protected_indices:
            text = values[index]
            results[index] = translate_with_glossary(
                text,
                active_entries,
                lambda fragment: self._cached_cloud_translate_many(
                    provider, [fragment], source_language, target_language, cache_scope, glossary_key
                )[0],
            )
        return [str(result) for result in results]

    def _unavailable_detail(self, failures: list[str], allow_paid_fallback: bool) -> str:
        detail = " ".join(failures) or "사용 가능한 번역 엔진이 없습니다."
        if not allow_paid_fallback and self.paid_fallback_available():
            # The reader has paid keys ready but has not opted in, so tell them
            # the switch exists rather than leaving translation simply stopped.
            detail = f"{detail} 확장 프로그램 설정에서 유료 API 전환을 켜면 계속 번역할 수 있습니다."
        return detail

    def warmup_for_provider(
        self,
        source_language: str,
        target_language: str,
        requested_provider: str | None,
        allow_paid_fallback: bool = False,
    ) -> ProviderSelection:
        requested = _normalized_provider(requested_provider)
        if source_language == target_language:
            return ProviderSelection(requested, requested)
        failures: list[str] = []
        for code in self._candidate_names(requested, allow_paid_fallback):
            provider = self._provider(code)
            if not provider.configured:
                failures.append(f"{_provider_label(code)} API 키가 설정되지 않았습니다.")
                continue
            # Do not spend a free quota character for a readiness check. The first
            # actual translation still falls back safely if the key is invalid.
            return self._selection(requested, code, failures)
        raise TranslationRuntimeError(self._unavailable_detail(failures, allow_paid_fallback))

    def translate_many_with_provider(
        self,
        texts: Iterable[str],
        source_language: str,
        target_language: str,
        glossary: Iterable[object],
        requested_provider: str | None,
        cache_scope: str | None = None,
        allow_paid_fallback: bool = False,
    ) -> ProviderTranslationResult:
        values = list(texts)
        glossary_values = list(glossary)
        active_cache_scope = self._activate_cache_scope(cache_scope)
        glossary_key = _glossary_cache_key(glossary_values, source_language, target_language)
        requested = _normalized_provider(requested_provider)
        if source_language == target_language:
            return ProviderTranslationResult(values, ProviderSelection(requested, requested))

        failures: list[str] = []
        for code in self._candidate_names(requested, allow_paid_fallback):
            try:
                provider = self._provider(code)
                if not provider.configured:
                    failures.append(f"{_provider_label(code)} API 키가 설정되지 않았습니다.")
                    continue
                translated = self._translate_cloud_many(
                    provider,
                    values,
                    source_language,
                    target_language,
                    glossary_values,
                    active_cache_scope,
                    glossary_key,
                )
                return ProviderTranslationResult(translated, self._selection(requested, code, failures))
            except (MissingLanguagePair, ProviderRequestError, TranslationRuntimeError) as error:
                failures.append(str(error))
        raise TranslationRuntimeError(self._unavailable_detail(failures, allow_paid_fallback))

    def translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
        glossary: Iterable[object],
        cache_scope: str | None = None,
        glossary_key: str | None = None,
    ) -> str:
        glossary_values = list(glossary)
        active_cache_scope = self._activate_cache_scope(cache_scope)
        active_glossary_key = glossary_key or _glossary_cache_key(glossary_values, source_language, target_language)
        active_entries = entries_for_language_pair(glossary_values, source_language, target_language)
        return translate_with_glossary(
            text,
            active_entries,
            lambda fragment: self._raw_translate(
                fragment, source_language, target_language, active_cache_scope, active_glossary_key
            ),
        )

    def translate_many(
        self,
        texts: Iterable[str],
        source_language: str,
        target_language: str,
        glossary: Iterable[object],
        cache_scope: str | None = None,
        glossary_key: str | None = None,
    ) -> list[str]:
        glossary_values = list(glossary)
        active_cache_scope = self._activate_cache_scope(cache_scope)
        active_glossary_key = glossary_key or _glossary_cache_key(glossary_values, source_language, target_language)
        return [
            self.translate(
                text,
                source_language,
                target_language,
                glossary_values,
                active_cache_scope,
                active_glossary_key,
            )
            for text in texts
        ]
