from __future__ import annotations

import base64
import binascii
import logging
from io import BytesIO
from threading import BoundedSemaphore
from time import perf_counter

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, UnidentifiedImageError

from .bubble import partition_regions, to_gray
from .models import (
    IncompleteRegion,
    PaidUsageSnapshot,
    ProcessingTimings,
    TranslateImageRequest,
    TranslateImageResponse,
    TranslationRegion,
    WarmupRequest,
)
from .ocr import (
    LTR_LINE_GROUPING_LANGUAGES,
    VERTICAL_CJK_GROUPING_LANGUAGES,
    PADDLE_LANGUAGE_CODES,
    PaddleOcrEngine,
    UnsupportedOcrLanguage,
    discard_contained_ocr_regions,
    filter_regions_by_source_language,
    group_ocr_regions,
    group_vertical_cjk_regions,
    normalize_ocr_text_for_translation,
)
from .translation import TranslationRuntimeError, TranslationService


MAX_IMAGE_BYTES = 20_000_000
MAX_IMAGE_EDGE = 8_000
MAX_IMAGE_PIXELS = 24_000_000
APP_VERSION = "0.7.0"
# We inspect image dimensions before decoding pixels, rather than relying on
# Pillow's warning-only decompression-bomb threshold.
Image.MAX_IMAGE_PIXELS = None
ocr_engine = PaddleOcrEngine()
translation_service = TranslationService()
# OCR is serialized to protect Paddle's model state and peak memory. Cloud
# translation is I/O-bound, so it gets a separate small pool once OCR has
# finished; a slow provider must not stop the next viewport from being read.
ocr_slot = BoundedSemaphore(value=1)
translation_slot = BoundedSemaphore(value=3)
logger = logging.getLogger("uvicorn.error")

app = FastAPI(
    title="말풍선 번역 레이어 로컬 서버",
    version=APP_VERSION,
    description="Browser screenshots are OCRed and translated on localhost only.",
)

# The server is intended to be bound to 127.0.0.1. Only extension origins receive CORS access.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"chrome-extension://[a-p]{32}",
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def decode_image(data_url: str) -> Image.Image:
    encoded = data_url.split(",", 1)[1] if "," in data_url else data_url
    try:
        binary = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise HTTPException(status_code=400, detail="이미지 데이터가 올바른 base64 형식이 아닙니다.") from error
    if len(binary) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="화면 이미지가 너무 큽니다. 브라우저 확대 비율을 낮춘 뒤 다시 시도하세요.")
    try:
        with Image.open(BytesIO(binary)) as source:
            width, height = source.size
            if (
                width <= 0
                or height <= 0
                or width > MAX_IMAGE_EDGE
                or height > MAX_IMAGE_EDGE
                or width * height > MAX_IMAGE_PIXELS
            ):
                raise HTTPException(
                    status_code=413,
                    detail="화면 해상도가 너무 큽니다. 브라우저 확대 비율을 낮춘 뒤 다시 시도하세요.",
                )
            source.load()
            return source.convert("RGB")
    except Image.DecompressionBombError as error:
        raise HTTPException(status_code=413, detail="화면 이미지의 압축 해제 크기가 너무 큽니다.") from error
    except (UnidentifiedImageError, OSError) as error:
        # The extension currently sends JPEG screenshots, but any Pillow-readable
        # raster the caller supplies is accepted here.
        raise HTTPException(status_code=400, detail="화면 이미지를 읽지 못했습니다.") from error


def _elapsed_ms(started_at: float) -> float:
    """Return a compact, non-negative duration suitable for API diagnostics."""
    return round(max(0.0, (perf_counter() - started_at) * 1_000), 1)


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "mode": "local-ocr",
        "ocr": "PaddleOCR (lazy-loaded)",
        "ocrLoadedLanguages": ocr_engine.loaded_languages(),
        # "onnxruntime" normally; "paddle" means the ONNX models could not be
        # loaded and OCR runs on the slower fallback.
        "ocrEngines": ocr_engine.loaded_engines(),
        "translation": "DeepL API / Azure Translator",
        "supportedOcrLanguages": sorted(PADDLE_LANGUAGE_CODES),
        "translationProviders": translation_service.provider_metadata(),
        "defaultTranslationProvider": "auto",
        "paidFallbackAvailable": translation_service.paid_fallback_available(),
        "paidUsage": translation_service.paid_usage_snapshot(),
        "ocrConcurrency": 1,
        "translationConcurrency": 3,
    }


@app.get("/v1/paid-usage")
def paid_usage() -> PaidUsageSnapshot:
    """This month's billed-engine spend, for the extension's usage readout."""
    return PaidUsageSnapshot(**translation_service.paid_usage_snapshot())


@app.get("/v1/languages")
def languages() -> dict[str, object]:
    return {"ocrLanguages": sorted(PADDLE_LANGUAGE_CODES)}


@app.post("/v1/warmup")
def warmup_ocr(request: WarmupRequest) -> dict[str, object]:
    """Preload the selected OCR language so the first webtoon capture is responsive."""
    if not ocr_slot.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="다른 화면을 처리 중입니다. 잠시 후 다시 시도하세요.")
    try:
        try:
            selection = translation_service.warmup_for_provider(
                request.sourceLanguage,
                request.targetLanguage,
                request.translationProvider,
                allow_paid_fallback=request.allowPaidFallback,
            )
            ocr_engine.warmup(request.sourceLanguage)
        except (UnsupportedOcrLanguage, TranslationRuntimeError) as error:
            status = 422 if isinstance(error, UnsupportedOcrLanguage) else 503
            raise HTTPException(status_code=status, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {
            "status": "ready",
            "sourceLanguage": request.sourceLanguage,
            "targetLanguage": request.targetLanguage,
            "translationProvider": selection.translation_provider,
            "requestedTranslationProvider": selection.requested_provider,
            "providerFallbackReason": selection.provider_fallback_reason,
            "usesPaidApi": selection.uses_paid_api,
            "paidUsage": translation_service.paid_usage_snapshot(),
        }
    finally:
        ocr_slot.release()


@app.post("/v1/translate-image", response_model=TranslateImageResponse)
def translate_image(request: TranslateImageRequest) -> TranslateImageResponse:
    if not ocr_slot.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="다른 화면을 처리 중입니다. 잠시 후 다시 시도하세요.")
    ocr_acquired = True
    translation_acquired = False
    request_started_at = perf_counter()
    try:
        decode_started_at = perf_counter()
        image = decode_image(request.image)
        decode_ms = _elapsed_ms(decode_started_at)

        # A cold Paddle model load can dominate one request by several seconds.
        # Record it separately so it is not mistaken for normal OCR inference.
        ocr_model_was_loaded = request.sourceLanguage in ocr_engine.loaded_languages()
        ocr_started_at = perf_counter()
        try:
            recognized = ocr_engine.recognize(image, request.sourceLanguage)
        except UnsupportedOcrLanguage as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        ocr_ms = _elapsed_ms(ocr_started_at)

        # Filter individual lines before grouping: a foreign-script caption must
        # not become part of an otherwise valid wrapped speech bubble.  The
        # helper is intentionally conservative and retains short/ambiguous text.
        filter_started_at = perf_counter()
        source_matched = filter_regions_by_source_language(recognized, request.sourceLanguage)
        filter_ms = _elapsed_ms(filter_started_at)
        # PaddleOCR emits a region per text line. Group first so a wrapped bubble
        # is translated as one utterance and a slightly weaker line is not silently
        # removed before it can join the surrounding dialogue.
        grouping_started_at = perf_counter()
        grouped = (
            group_ocr_regions(source_matched)
            if request.sourceLanguage in LTR_LINE_GROUPING_LANGUAGES
            else group_vertical_cjk_regions(source_matched)
            if request.sourceLanguage in VERTICAL_CJK_GROUPING_LANGUAGES
            else source_matched
        )
        # Grouping can legitimately retain a complete bubble and a tighter OCR
        # fragment from inside it. Keep the complete utterance only before a
        # cloud provider sees the text, so it cannot become two translated
        # patches or spend quota twice.
        grouped = discard_contained_ocr_regions(grouped)
        grouping_ms = _elapsed_ms(grouping_started_at)

        confidence_filter_started_at = perf_counter()
        confident = [region for region in grouped if float(region["confidence"]) >= request.minConfidence]
        confidence_filter_ms = _elapsed_ms(confidence_filter_started_at)

        # Reading half a speech bubble produces a truncated sentence that then
        # gets translated into confident nonsense, and text recognised on the
        # artwork produces a patch where the reader can see there is no bubble.
        # Both are decided from the pixels, before paying for translation.
        bubble_filter_started_at = perf_counter()
        if request.requireCompleteBubble:
            readable, clipped, bubbleless = partition_regions(to_gray(image), confident)
        else:
            readable, clipped, bubbleless = confident, [], 0
        bubble_filter_ms = _elapsed_ms(bubble_filter_started_at)
        # The translator gets the repaired text (G0NE -> GONE); the response
        # keeps what OCR actually read.
        texts = [normalize_ocr_text_for_translation(str(region["text"]), request.sourceLanguage) for region in readable]

        translation_started_at = perf_counter()
        # Release the CPU/model slot before waiting for a network provider.
        # A later screenshot can now run OCR while this one is in flight.
        ocr_slot.release()
        ocr_acquired = False
        translation_slot.acquire()
        translation_acquired = True
        try:
            translation_result = translation_service.translate_many_with_provider(
                texts,
                request.sourceLanguage,
                request.targetLanguage,
                request.glossary,
                request.translationProvider,
                cache_scope=request.cacheScope,
                allow_paid_fallback=request.allowPaidFallback,
            )
        except TranslationRuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        translation_ms = _elapsed_ms(translation_started_at)

        response_build_started_at = perf_counter()
        regions = [
            TranslationRegion(
                x=float(region["x"]),
                y=float(region["y"]),
                width=float(region["width"]),
                height=float(region["height"]),
                text=str(region["text"]),
                translation=translation,
                confidence=float(region["confidence"]),
            )
            for region, translation in zip(readable, translation_result.translations, strict=True)
        ]
        response_build_ms = _elapsed_ms(response_build_started_at)
        stage_timings = {
            "decode": decode_ms,
            "ocr": ocr_ms,
            "filter": filter_ms,
            "grouping": grouping_ms,
            "confidenceFilter": confidence_filter_ms,
            "bubbleFilter": bubble_filter_ms,
            "translation": translation_ms,
            "responseBuild": response_build_ms,
        }
        slowest_stage, slowest_stage_ms = max(stage_timings.items(), key=lambda item: item[1])
        timings = ProcessingTimings(
            decodeMs=decode_ms,
            ocrMs=ocr_ms,
            filterMs=filter_ms,
            groupingMs=grouping_ms,
            confidenceFilterMs=confidence_filter_ms,
            translationMs=translation_ms,
            responseBuildMs=response_build_ms,
            totalMs=_elapsed_ms(request_started_at),
            slowestStage=slowest_stage,
            slowestStageMs=slowest_stage_ms,
            ocrModelWasLoaded=ocr_model_was_loaded,
            recognizedRegionCount=len(recognized),
            sourceMatchedRegionCount=len(source_matched),
            groupedRegionCount=len(grouped),
            translatedRegionCount=len(regions),
            bubbleFilterMs=bubble_filter_ms,
            clippedRegionCount=len(clipped),
            bubblelessRegionCount=bubbleless,
        )
        # No image pixels or OCR text are emitted.  The one-line record is safe
        # to keep in the normal local-server log for performance diagnosis.
        logger.info(
            "translate-image timings total_ms=%.1f decode_ms=%.1f ocr_ms=%.1f "
            "filter_ms=%.1f grouping_ms=%.1f confidence_filter_ms=%.1f "
            "translation_ms=%.1f response_build_ms=%.1f bottleneck=%s:%.1f "
            "ocr_model_was_loaded=%s regions=%d/%d/%d/%d clipped=%d bubbleless=%d provider=%s paid=%s",
            timings.totalMs,
            timings.decodeMs,
            timings.ocrMs,
            timings.filterMs,
            timings.groupingMs,
            timings.confidenceFilterMs,
            timings.translationMs,
            timings.responseBuildMs,
            timings.slowestStage,
            timings.slowestStageMs,
            timings.ocrModelWasLoaded,
            timings.recognizedRegionCount,
            timings.sourceMatchedRegionCount,
            timings.groupedRegionCount,
            timings.translatedRegionCount,
            timings.clippedRegionCount,
            timings.bubblelessRegionCount,
            translation_result.selection.translation_provider,
            translation_result.selection.uses_paid_api,
        )
        return TranslateImageResponse(
            imageWidth=image.width,
            imageHeight=image.height,
            regions=regions,
            incompleteRegions=[IncompleteRegion(**region) for region in clipped],
            translationProvider=translation_result.selection.translation_provider,
            requestedTranslationProvider=translation_result.selection.requested_provider,
            providerFallbackReason=translation_result.selection.provider_fallback_reason,
            usesPaidApi=translation_result.selection.uses_paid_api,
            paidUsage=PaidUsageSnapshot(**translation_service.paid_usage_snapshot()),
            processingTimings=timings,
        )
    finally:
        if translation_acquired:
            translation_slot.release()
        if ocr_acquired:
            ocr_slot.release()
