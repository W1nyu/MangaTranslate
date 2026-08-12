# LLM Handoff - Webtoon Translation Layer

## Purpose

This project is a Chrome/Edge Manifest V3 extension plus a localhost FastAPI
server. It captures the currently visible portion of a webtoon page, performs
OCR locally, sends only recognized text to a configured cloud translation
provider, and renders translations as browser overlays.

The user-facing documentation and UI are Korean. Source code comments and this
handoff use English so another coding LLM can take over easily.

## Current state (checked 2026-08-12)

- Python unit suite passes: **96 tests, 0 failures**.
- Extension unit suite passes: **62 tests, 0 failures**
  (`node --test "extension/tests/**/*.test.mjs"`, no dependencies to install).
- The local virtual environment, Argos data directory, and a local `.env` are
  present under `local-server/`. Do not read, print, commit, or share `.env`.
- The project is now a real Git repository with an `origin` at
  `https://github.com/W1nyu/MangaTranslate`. `.env` and `.paid-usage.json` are
  ignored; verify with `git check-ignore -v local-server/.env` before any commit
  that touches configuration.
- The overlay was reworked from "one screenshot, one result" into a persistent
  per-episode region store. See "Reading model" below before touching
  `content.js`.
- Verified in a real browser against a synthetic lazy-loading episode by running
  the real `content.js` with a stubbed `chrome` API: off-screen culling,
  scroll-back restore with zero server calls, incremental strip captures, hover
  reveal, and session hydration all behave as described.
- Not yet validated end to end with the packed extension on a live webtoon site.
  That still needs a manual pass (load unpacked, reload the tab, read an
  episode).

## What changed most recently

**Paid fallback tier.** The free plans stop by themselves (DeepL Free answers
456, Azure F0 answers 403) and translation used to simply stop with them. The
server can now continue on a billed plan instead. Four rules hold the design
together; do not relax any of them casually:

1. **Opt-in.** Requests carry `allowPaidFallback`, default `false`, set from a
   checkbox in the options page that is off by default. Without it, no code path
   reaches a paid provider.
2. **Last resort.** `_candidate_names()` appends the paid engines *after* every
   free engine, mirroring the free preference order, so a paid call only happens
   once each free option has actually been tried and failed.
3. **Metered.** Every paid provider is wrapped in `MeteredPaidProvider`, which
   checks the monthly character budget immediately before the network call and
   records characters only after it succeeds. There is no unwrapped path to a
   billed provider. The ledger (`app/paid_usage.py`) persists to
   `local-server/.paid-usage.json`, so restarting the server does not reset the
   budget; `PAID_MONTHLY_CHAR_LIMIT=0` removes the cap.
4. **Visible.** `usesPaidApi` rides along on the warmup and translate responses;
   the overlay status line says `유료 사용 중` and the popup/options pages show
   the month's spend. The reader never has to go looking to find out that a
   screen cost money.

Paid engines (`deepl_pro`, `azure_paid`) are deliberately **not** selectable in
the engine dropdown: choosing "DeepL" means choosing DeepL's wording, not
choosing to pay. `TranslationProvider` (request) and
`ResolvedTranslationProvider` (response) in `models.py` encode exactly that
split — the request Literal rejects a paid code with HTTP 422.

Earlier: performance work, measured rather than assumed:

| Change | Effect |
| --- | --- |
| Keep-alive connections to DeepL/Azure (`translation.py`) | Removed a full TCP+TLS handshake per scan. Measured 625 ms to DeepL from this machine; translation time per request fell from ~1070 ms to ~450 ms. **Largest single win.** |
| Incremental strip capture | A quarter-screen scroll reads a ~310 px strip instead of a 731 px screen. OCR is roughly proportional to area; a 35% strip measured 69% less OCR time. |
| Coverage ranges + region store | Returning to already-read page costs no capture, no OCR, and no network at all. |
| Unchanged-frame signature skip | A pixel-identical screen skips the whole round trip. |
| JPEG capture instead of PNG | OCR text verified identical on shaded/screentoned artwork. Payload ~22% smaller and decode ~2x faster; the larger benefit is that Chrome no longer PNG-encodes every screenshot. Server time alone barely moves - do not expect much from this one. |

End to end, a typical scroll step measured **3179 ms -> 1116 ms**.

## Architecture

```text
Webtoon page (Chrome/Edge tab)
  -> MV3 extension captures/crops the visible viewport
  -> http://127.0.0.1:8765/v1/translate-image
  -> FastAPI server: image validation -> PaddleOCR -> filtering/grouping
  -> DeepL API and/or Azure Translator (OCR text only)
  -> regions with translated text and timing data
  -> content script maps regions back to the page and draws overlays
```

Important privacy boundary: image pixels remain local. Only OCR text and
language/provider metadata are sent to DeepL/Azure. The extension rejects any
server URL whose hostname is not `127.0.0.1` or `localhost`.

## Repository map

| Path | Responsibility |
| --- | --- |
| `extension/manifest.json` | MV3 declaration, permissions, service worker, popup/options pages. |
| `extension/background.js` | Viewport capture/cropping, JPEG encoding, unchanged-frame signatures, localhost requests, per-tab state, request throttling, translation logs, region memory. |
| `extension/content.js` / `content.css` | Region selection, the region store and its overlay lifecycle, artwork anchoring, hover reveal, incremental capture planning. |
| `extension/popup.*` | Start/stop control, status, copy/log controls. |
| `extension/options.*` | Settings and glossary editor. |
| `extension/shared/defaults.js` | Settings defaults, language/provider options, `scanDelay` migration. |
| `extension/shared/glossary.js` | Browser-side glossary helpers. |
| `extension/shared/regions.js` | Pure geometry: anchors, region merging, coverage ranges, incremental band planning. No DOM, no `chrome.*`. |
| `extension/tests/` | Node (`node:test`) unit tests for `shared/regions.js` and `shared/defaults.js`. |
| `local-server/app/main.py` | FastAPI routes, image limits, single-request semaphore, processing pipeline and timings. |
| `local-server/app/ocr.py` | PaddleOCR adapter, script/language filtering, LTR speech-bubble line grouping. |
| `local-server/app/translation.py` | DeepL/Azure clients (free and paid), provider fallback chain, paid metering wrapper, request/cache behavior. |
| `local-server/app/paid_usage.py` | Monthly character ledger for the paid tier. Persisted, month-rolling, fail-open on a corrupt file. |
| `local-server/app/bubble.py` | Decides whether a region sits in a fully visible speech bubble. Pixel analysis only, no OCR. |
| `local-server/app/glossary.py` | Protects/reinserts glossary terms around machine translation. |
| `local-server/app/models.py` | Pydantic request/response models. |
| `local-server/scripts/` | Bootstrap, model installation, server launch, doctor, smoke test. |
| `local-server/tests/` | Unit tests for OCR, glossary, runtime, installation and cloud-provider behavior. |

## Local server contract

The server binds only to `127.0.0.1:8765`; CORS is restricted to Chrome
extension origins.

- `GET /health` returns readiness, loaded OCR languages, supported OCR
  languages, configured translation-provider metadata (now including
  `deepl_pro`/`azure_paid`), `paidFallbackAvailable`, and `paidUsage`.
- `GET /v1/languages` returns supported PaddleOCR languages.
- `GET /v1/paid-usage` returns this month's paid spend for the settings page.
  Configuration state and counters only; it never returns key material.
- `POST /v1/warmup` receives `sourceLanguage`, `targetLanguage`,
  `translationProvider`, and `allowPaidFallback`; it validates/preloads the
  provider and OCR model, and reports `usesPaidApi`.
- `POST /v1/translate-image` receives a base64 image, `cacheScope`, language
  pair, `translationProvider`, `allowPaidFallback`, `minConfidence`,
  `requireCompleteBubble`, and a glossary. It returns image dimensions,
  translated OCR regions, the geometry of bubbles held back for being cut off
  (`incompleteRegions`), selected/requested provider, fallback reason,
  `usesPaidApi`, `paidUsage`, and stage timings.

`TranslateImageRequest` is defined in `local-server/app/models.py`. Preserve
camelCase field names when changing extension/server communication.

The request pipeline is deliberately ordered as:

1. Validate/decode the image (20 MB / 8,000 px edge / 24 MP limits).
2. OCR it with PaddleOCR.
3. Remove clear source-language/script mismatches.
4. Group LTR text lines into speech-bubble utterances.
5. Apply the confidence threshold.
6. Keep only regions inside a fully visible speech bubble (`requireCompleteBubble`).
7. Translate, with glossary protection and per-episode cache scope.
8. Return region geometry in the cropped image's pixel space, plus the geometry
   of any bubble held back for being cut off.

The server permits only one OCR/translation request at a time; overlapping
requests return HTTP 429. The extension also throttles captures.

## Translation and OCR behavior

- Supported UI language codes: `en`, `ko`, `ja`, `zh`, `es`, `fr`, `de`, `pt`,
  `ru`, `vi` (verify PaddleOCR/model availability before adding another one).
- Selectable providers are `auto`, `deepl`, and `azure`. `auto` prefers DeepL
  then Azure; explicit choices only fall back to the other cloud provider. Argos
  is used for model/runtime preparation, not as a request-time translation
  fallback.
- With `allowPaidFallback`, the chain continues into `deepl_pro` then
  `azure_paid` (or the reverse when Azure was chosen). Never insert a paid
  engine ahead of a working free one.
- API keys live only in `local-server/.env`, loaded by `app/runtime.py`. The
  paid keys are `DEEPL_PRO_AUTH_KEY`, `AZURE_TRANSLATOR_PAID_KEY`, and
  `AZURE_TRANSLATOR_PAID_REGION`. A DeepL Free key (`:fx`) placed in the Pro
  slot reports as unconfigured rather than failing later with a 403.
- Glossary entries include language pair, enabled flag, case sensitivity,
  whole-word matching, and priority. The implementation protects matching
  source terms with markers and restores target terms after translation.
- Translation caching is in-memory and scoped to `origin + pathname + search`
  sent by the extension, which prevents a prior episode's text being reused in
  a later episode.
- OCR filtering is intentionally conservative: it removes obvious wrong-script
  text but retains short/ambiguous text. Do not make this aggressive without
  adding regression fixtures/tests.

## Reading model (read before editing `content.js`)

The overlay is no longer "render the latest response". It is a store of
translations for the episode, plus a projection of that store onto the screen.

- **`RegionStore`** holds one entry per speech bubble for the current episode
  scope. `mergeRegions` folds each capture into it: a bubble that is recognised
  again updates in place, and an empty re-read keeps the translation it already
  had. A capture never disturbs bubbles it did not cover.
- **Anchors.** Each entry is anchored to the `<img>` under its centre, as
  fractions of that image's box (`{kind:"element", key, relX, relY, relW, relH}`),
  with a document rect as fallback. The key is `currentSrc|index`, which survives
  a reload. This is why an overlay follows its panel when lazy loading reflows
  the page.
- **`syncOverlays`** resolves every anchor for the current scroll position,
  mounts a patch for what intersects the viewport, and *removes* the rest.
  Off-screen means no DOM node. Coming back mounts it again from the store with
  no server call. Patches are positioned with `transform`, so scrolling stays on
  the compositor; width/height are only written when they change.
- **Coverage** is a set of already-read document ranges, not a single band.
  `incrementalBand` returns `full` (read the whole region), `band` (read only the
  unread strip, widened by a seam overlap so a bubble crossing the boundary is
  captured whole), or `none` (everything on screen was already read - repaint
  from the store and make no request). Coverage is discarded whenever the layout
  can have moved: resize, nested scroll, a crop change, an image loading at or
  above the viewport, a manual refresh, or `MAX_CONSECUTIVE_BAND_SCANS` strips in
  a row.
- **Hover reveal** hit-tests the pointer against the mounted patches' `hit*`
  datasets. The overlay host stays `pointer-events: none`, so the page keeps
  every event it would normally receive. Revealing swaps in the *translation* -
  re-showing the original would just repeat what the artwork already says.
- **Region memory** is mirrored into `chrome.storage.session` per tab and
  episode scope, guarded by the same scope check as the translation log. It is
  session storage on purpose: nothing about what the user read reaches disk.
- **Store hygiene.** `mergeRegions` takes the document rect the capture actually
  read. A stored bubble lying wholly inside it that the capture did not find
  again earns a strike, and is removed after `DEFAULT_MAX_MISSES` of them.
  Without this, one spurious OCR region would leave a patch on screen forever -
  the store turns transient OCR noise into a permanent artefact.

## Complete-bubble filtering (`app/bubble.py`)

Only text inside a fully visible speech bubble is translated. Two failures
motivated it: lettering recognised on the artwork produced a patch where the
reader can plainly see there is no bubble, and a bubble half cut off by the
screen edge produced a truncated sentence that machine translation turned into
confident nonsense.

The decision is made from the captured pixels, per region:

1. The fill brightness is the 75th percentile *inside* the text box - the glyphs
   are dark and the fill between them is what the bubble looks like. Sampling
   outside the box instead is fooled by a loose grouping box that already touches
   the outline.
2. Below `LIGHT_FILL_THRESHOLD` the text is sitting on artwork: `absent`, dropped.
3. Otherwise the box grows outward along each axis while the lines stay fill. A
   small gutter of glyph spill is skipped and a real boundary must be several
   consecutive lines, because a single descender poking past the box would
   otherwise read as the bubble's edge. Getting this wrong makes clip detection
   silently do nothing, so `test_bubble_detection.py` pins it.
4. Fill reaching the image border means the bubble continues outside the
   capture: `clipped`. Those regions are returned as `incompleteRegions`
   (geometry only, no text) rather than translated.

**The client must not mark a clipped bubble's area as read.** `trimBandForClipped`
cuts the claimed band back to just above a bottom-clipped bubble, so the next
scroll re-reads it. Skipping that step makes the bubble vanish permanently: the
server refuses to translate it and the coverage logic then refuses to look again.

Everything here is conservative by design - an inconclusive region is kept.
`requireCompleteBubble` turns the whole filter off if it ever over-filters.

Two independent guards stop a stale translation being drawn over the wrong art:
an element anchor that cannot be resolved is simply not drawn, and a nested
scroll marks document-anchored entries stale until the next capture.

## Extension behavior worth preserving

- It injects scripts only after the user starts translation for the active tab;
  navigating/reloading requires a new start action.
- The user chooses a capture region (or full viewport) before OCR. That region
  persists while scanning the same episode page.
- Existing overlays are hidden only while the screenshot is captured, then
  restored immediately; OCR and network translation happen afterward.
- Capture sizing is optimized to balance OCR quality with a pixel/long-edge
  budget. Do not assume CSS pixels equal captured image pixels; use the
  `captureRegion` and `captureImage` mapping returned by `background.js`.
- Translation logs are stored locally and deduplicated. Settings use
  `chrome.storage.sync`; glossary, tab state, and logs use local extension
  storage.

## Run and verify

Initial setup (Python 3.10–3.13; do not use Python 3.14 for PaddleOCR):

```powershell
cd C:\Users\Winyu\Documents\Project\manga\local-server
.\scripts\bootstrap.ps1 -PythonPath 'C:\path\to\python.exe' -SourceLanguage en -TargetLanguage ko
```

Start the companion server:

```powershell
cd C:\Users\Winyu\Documents\Project\manga\local-server
.\.venv\Scripts\python.exe .\scripts\run_server.py
```

Load `C:\Users\Winyu\Documents\Project\manga\extension` as an unpacked
extension at `chrome://extensions`, then start translation from the popup.
After extension changes, reload it in `chrome://extensions` and reload the
target webtoon tab.

Run both unit suites:

```powershell
cd C:\Users\Winyu\Documents\Project\manga\local-server
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

cd C:\Users\Winyu\Documents\Project\manga
node --test "extension/tests/**/*.test.mjs"
```

Useful manual checks after a functional change:

1. `GET http://127.0.0.1:8765/health` succeeds.
2. Start on an ordinary non-DRM page and select a small visible region.
3. Confirm no old overlay appears in its own screenshot.
4. Scroll down: a bubble leaving the screen takes its patch with it, and only
   newly arrived bubbles are read. The status tooltip should say
   "새로 나온 부분만 읽음" for ordinary reading-pace scrolling. Nothing should be
   captured while the page is still moving.
5. Stop with a bubble half off the bottom of the screen: it stays untranslated
   and the tooltip says "잘린 말풍선 …개는 다음에". Scroll until it is fully
   visible and it must then be translated.
6. Confirm a sound effect drawn on the artwork gets no patch.
7. Scroll back up: translations reappear immediately and the server log shows
   **no** new `translate-image` line.
8. Press "원문", then point at a bubble: its translation alone comes back.
9. On a lazily loading episode, confirm patches follow their panel when a late
   image pushes the page down.
10. Verify glossary replacement and the chosen/fallback provider status.
11. Confirm invalid remote server URLs are rejected in options/background flow.
12. Move to another chapter and confirm no translation from the previous one
    survives.

The server log line for each request carries `ocr_ms` and `translation_ms`; the
toolbar tooltip carries the browser-side numbers. Use both when judging whether
a change actually made reading faster.

## Change guidance for the next LLM

- Read the narrowly relevant files before editing; the coordinate pipeline and
  OCR grouping rules have subtle regressions covered by tests.
- Change both sides of the API together. Update `models.py`, the background
  request/response handling, and tests when the contract changes.
- Keep secrets, screenshots, OCR text, and generated runtime artifacts out of
  commits and chat output. `.env`, `.venv/`, `.argos/`, and `tmp/` are local
  runtime state.
- Preserve loopback-only server use, strict image limits, CORS restriction, and
  the single processing semaphore unless there is an explicit security and
  resource-management design change.
- Add a targeted unit test in `local-server/tests/` for Python behavior, and in
  `extension/tests/` for anything you can express as pure geometry or planning.
  Keep such logic in `shared/regions.js` rather than inside `content.js`: the
  DOM-bound parts of the overlay still have no automated coverage, so anything
  left in there can only be checked by hand.
- Before claiming a change made things faster, measure it. The two obvious
  candidates in this codebase turned out to be misleading: JPEG capture barely
  moves server time, while the provider TLS handshake - which was not on any
  list - dominated every scan.
- The README is the user setup guide; update it alongside changes that affect
  install, configuration, usage, privacy, or limitations.

## Suggested first commands for a fresh agent

```powershell
cd C:\Users\Winyu\Documents\Project\manga
Get-Content HANDOFF.md
rg -n "relevant-symbol-or-endpoint" extension local-server\app local-server\tests
cd local-server
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
