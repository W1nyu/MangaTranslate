from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


#: What the reader can pick in the extension. The paid plans are never chosen
#: here - they are only reached by falling back past an exhausted free quota.
TranslationProvider = Literal["auto", "azure", "deepl"]
#: What the server may report back as the engine that actually answered.
ResolvedTranslationProvider = Literal["auto", "azure", "deepl", "deepl_pro", "azure_paid"]


class GlossaryEntry(BaseModel):
    """A user rule whose target text must survive machine translation."""

    id: str = ""
    source: str = Field(min_length=1, max_length=160)
    target: str = Field(min_length=1, max_length=160)
    sourceLanguage: str = Field(default="en", min_length=2, max_length=8)
    targetLanguage: str = Field(default="ko", min_length=2, max_length=8)
    enabled: bool = True
    caseSensitive: bool = False
    wholeWord: bool = True
    priority: int = Field(default=0, ge=-100, le=1000)

    @field_validator("source", "target")
    @classmethod
    def strip_term(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("빈 표현은 사전에 넣을 수 없습니다.")
        return value


class TranslateImageRequest(BaseModel):
    image: str = Field(min_length=32, max_length=32_000_000)
    # The extension derives this from origin + pathname + search. It keeps the
    # in-memory cache bounded to the current webtoon episode rather than making
    # old chapter dialogue available to a later page.
    cacheScope: str = Field(default="legacy", min_length=1, max_length=2048)
    sourceLanguage: str = Field(min_length=2, max_length=8)
    targetLanguage: str = Field(min_length=2, max_length=8)
    translationProvider: TranslationProvider = "auto"
    minConfidence: float = Field(default=0.55, ge=0.0, le=1.0)
    # Only translate text enclosed by a fully visible speech bubble. A bubble cut
    # off by the edge of the capture is left for a later one that contains all of
    # it, rather than translated as half a sentence.
    requireCompleteBubble: bool = True
    # Off unless the reader turned it on. Once every free engine has failed, the
    # server may continue on a billed plan instead of stopping the scan; leaving
    # this at False means an exhausted free quota simply stops translating.
    allowPaidFallback: bool = False
    glossary: list[GlossaryEntry] = Field(default_factory=list, max_length=500)


class WarmupRequest(BaseModel):
    sourceLanguage: str = Field(min_length=2, max_length=8)
    targetLanguage: str = Field(min_length=2, max_length=8)
    translationProvider: TranslationProvider = "auto"
    allowPaidFallback: bool = False


class TranslationRegion(BaseModel):
    x: float
    y: float
    width: float
    height: float
    text: str
    translation: str
    confidence: float


class IncompleteRegion(BaseModel):
    """Where a bubble ran off the capture, so the caller can re-read it later.

    Geometry only: no recognised text is reported for a region that was not
    translated.
    """

    x: float
    y: float
    width: float
    height: float
    edges: list[str] = Field(default_factory=list)


class ProcessingTimings(BaseModel):
    """Per-request server timings, in milliseconds.

    These values cover the local-server work after FastAPI has parsed the
    request.  Browser capture, image upload, and overlay rendering happen in
    the extension and are intentionally not folded into ``totalMs``.
    """

    decodeMs: float
    ocrMs: float
    filterMs: float
    groupingMs: float
    confidenceFilterMs: float
    translationMs: float
    responseBuildMs: float
    totalMs: float
    slowestStage: str
    slowestStageMs: float
    ocrModelWasLoaded: bool
    recognizedRegionCount: int
    sourceMatchedRegionCount: int
    groupedRegionCount: int
    translatedRegionCount: int
    bubbleFilterMs: float = 0.0
    #: Regions held back because their bubble was cut off by the capture edge.
    clippedRegionCount: int = 0
    #: Regions dropped because no speech bubble encloses them.
    bubblelessRegionCount: int = 0


class PaidUsageSnapshot(BaseModel):
    """This month's paid-engine spend. Never includes key material."""

    month: str = ""
    usedCharacters: int = 0
    monthlyLimit: int = 0
    limited: bool = True
    #: ``None`` when the reader removed the cap with PAID_MONTHLY_CHAR_LIMIT=0.
    remainingCharacters: int | None = None
    byProvider: dict[str, int] = Field(default_factory=dict)
    #: Whether any paid key is configured at all.
    available: bool = False


class TranslateImageResponse(BaseModel):
    imageWidth: int
    imageHeight: int
    regions: list[TranslationRegion]
    incompleteRegions: list[IncompleteRegion] = Field(default_factory=list)
    translationProvider: ResolvedTranslationProvider = "auto"
    requestedTranslationProvider: TranslationProvider = "auto"
    providerFallbackReason: str | None = None
    #: True when this screen was translated by a billed plan.
    usesPaidApi: bool = False
    paidUsage: PaidUsageSnapshot | None = None
    # Added after the initial extension protocol. Keeping it optional makes
    # direct callers that construct the old response shape remain compatible.
    processingTimings: ProcessingTimings | None = None
