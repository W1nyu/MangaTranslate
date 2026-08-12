(() => {
  if (globalThis.__webtoonTranslationLayerLoaded) return;
  globalThis.__webtoonTranslationLayerLoaded = true;

  const { getSettings, getGlossary, translationProviderLabel } = globalThis.WebtoonTranslator;
  const { entriesForLanguage } = globalThis.WebtoonGlossary;
  const {
    mergeRegions,
    resolveAnchorRect,
    documentAnchor,
    elementAnchor,
    rectsIntersect,
    incrementalBand,
    addCoverage,
    trimBandForClipped,
    normalizedCaptureRegion: normalizeCaptureRegion
  } = globalThis.WebtoonRegions;

  const nextFrame = () => new Promise((resolve) => requestAnimationFrame(resolve));
  const EXTENSION_CONTEXT_INVALIDATED = /extension context invalidated|context invalidated/i;
  const MIN_CAPTURE_REGION_PIXELS = 24;
  // Keep a patch mounted slightly beyond the viewport so a slow scroll does not
  // pop it in at the very edge of the screen.
  const VISIBLE_MARGIN_PX = 64;
  const MAX_STORE_ENTRIES = 600;
  const REGION_MEMORY_SAVE_DELAY_MS = 900;
  // Strip captures accumulate whatever small errors each one carries. Coming
  // back to a full capture periodically resynchronizes the page.
  const MAX_CONSECUTIVE_BAND_SCANS = 8;

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  // Document coordinates are derived from the layout viewport, the same frame
  // `getBoundingClientRect` and a `position: fixed` overlay both live in. Mixing
  // in visualViewport offsets here would misplace every patch under pinch zoom.
  function getViewportFrame() {
    return {
      scrollX: Number(window.scrollX) || 0,
      scrollY: Number(window.scrollY) || 0,
      width: Math.max(1, Number(window.innerWidth) || 1),
      height: Math.max(1, Number(window.innerHeight) || 1),
      devicePixelRatio: Number(window.devicePixelRatio) || 1
    };
  }

  function toViewportRect(documentRect, frame) {
    return {
      left: documentRect.left - frame.scrollX,
      top: documentRect.top - frame.scrollY,
      width: documentRect.width,
      height: documentRect.height
    };
  }

  function isRootScrollEvent(event) {
    const target = event?.target;
    return target === document || target === document.documentElement || target === document.body || target === window;
  }

  function getEpisodeCacheScope() {
    const url = new URL(window.location.href);
    // A chapter can be represented by query parameters, while the hash is
    // commonly just an in-page position. Keep origin/path/query as the scope.
    return `${url.origin}${url.pathname}${url.search}`;
  }

  // ---------------------------------------------------------------------------
  // Anchoring translations to the artwork underneath them.
  //
  // A webtoon lazily loads panels, so a bubble's document position can move
  // after it was read. Anchoring to the <img> that carries the bubble makes the
  // patch travel with the artwork instead of floating over whatever ends up at
  // that page offset.
  // ---------------------------------------------------------------------------

  const anchorKeyByElement = new WeakMap();
  const anchorElementByKey = new Map();

  function imageSource(image) {
    return image.currentSrc || image.getAttribute("src") || "";
  }

  function rememberAnchorElement(key, element) {
    anchorKeyByElement.set(element, key);
    anchorElementByKey.set(key, new WeakRef(element));
  }

  function anchorKeyForImage(element) {
    const cached = anchorKeyByElement.get(element);
    if (cached) return cached;
    const source = imageSource(element);
    if (!source) return null;

    // Nearly every webtoon panel has a unique URL; the index only disambiguates
    // the rare repeated asset, and keeps the key reproducible after a reload.
    let index = 0;
    for (const image of document.images) {
      if (image === element) break;
      if (imageSource(image) === source) index += 1;
    }
    const key = `${source}|${index}`;
    rememberAnchorElement(key, element);
    return key;
  }

  function documentRectOf(element, frame) {
    const rect = element.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    return {
      left: rect.left + frame.scrollX,
      top: rect.top + frame.scrollY,
      width: rect.width,
      height: rect.height
    };
  }

  // Resolve every anchor key the store needs in at most one pass over the
  // document's images. Cached elements cost nothing; only keys whose element was
  // replaced fall through to the scan.
  function elementRectsForKeys(keys, frame) {
    const rects = new Map();
    const pending = new Set();

    for (const key of keys) {
      const element = anchorElementByKey.get(key)?.deref();
      if (element?.isConnected) {
        const rect = documentRectOf(element, frame);
        if (rect) rects.set(key, rect);
        continue;
      }
      anchorElementByKey.delete(key);
      pending.add(key);
    }
    if (!pending.size) return rects;

    const seenPerSource = new Map();
    for (const image of document.images) {
      const source = imageSource(image);
      if (!source) continue;
      const index = seenPerSource.get(source) || 0;
      seenPerSource.set(source, index + 1);
      const key = `${source}|${index}`;
      if (!pending.has(key)) continue;

      rememberAnchorElement(key, image);
      const rect = documentRectOf(image, frame);
      if (rect) rects.set(key, rect);
      pending.delete(key);
      if (!pending.size) break;
    }
    return rects;
  }

  function imageAtPoint(x, y) {
    // The overlay host is pointer-events:none, so it never shadows this probe.
    for (const element of document.elementsFromPoint(x, y)) {
      if (element instanceof HTMLImageElement && element.naturalWidth > 0) return element;
    }
    return null;
  }

  function anchorForDocumentRect(documentRect, frame) {
    const viewportRect = toViewportRect(documentRect, frame);
    const centerX = viewportRect.left + viewportRect.width / 2;
    const centerY = viewportRect.top + viewportRect.height / 2;
    if (centerX < 0 || centerY < 0 || centerX > frame.width || centerY > frame.height) {
      return documentAnchor(documentRect);
    }

    const image = imageAtPoint(centerX, centerY);
    if (!image) return documentAnchor(documentRect);
    const key = anchorKeyForImage(image);
    const imageRect = key ? documentRectOf(image, frame) : null;
    return (imageRect && elementAnchor(key, imageRect, documentRect)) || documentAnchor(documentRect);
  }

  function isExtensionContextInvalidated(error) {
    return EXTENSION_CONTEXT_INVALIDATED.test(String(error?.message || error || ""));
  }

  function formatMilliseconds(value) {
    const milliseconds = Number(value);
    if (!Number.isFinite(milliseconds) || milliseconds < 0) return null;
    if (milliseconds < 1_000) return `${Math.round(milliseconds)}ms`;
    return `${(milliseconds / 1_000).toFixed(milliseconds >= 10_000 ? 0 : 1)}초`;
  }

  // The server only measures its own work, so a slow scan can never be diagnosed
  // from it alone. Showing the browser-side numbers next to it makes the actual
  // bottleneck — capture, localhost round trip, OCR, or the translation API —
  // visible from the toolbar tooltip.
  function processingTimingDetail(timings, clientTimings) {
    const entries = [
      ["전체", formatMilliseconds(clientTimings?.totalMs)],
      ["캡처·왕복", formatMilliseconds(clientTimings?.roundTripMs)],
      ["서버", formatMilliseconds(timings?.totalMs)],
      ["OCR", formatMilliseconds(timings?.ocrMs)],
      ["번역", formatMilliseconds(timings?.translationMs)]
    ].filter(([, value]) => value);
    return entries.map(([label, value]) => `${label} ${value}`).join(" · ");
  }

  async function writeTextToClipboard(value) {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        return;
      } catch {
        // A content script can lose Clipboard API access on some pages. Fall
        // back to the user-gesture copy command used by older Chromium builds.
      }
    }
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "");
    textarea.style.cssText = "position:fixed;opacity:0;pointer-events:none;";
    document.documentElement.append(textarea);
    textarea.select();
    const copied = document.execCommand("copy");
    textarea.remove();
    if (!copied) throw new Error("클립보드에 원문을 복사하지 못했습니다.");
  }

  // ---------------------------------------------------------------------------
  // The translation store for the episode currently being read.
  // ---------------------------------------------------------------------------

  class RegionStore {
    constructor() {
      this.entries = [];
      this.scope = null;
    }

    setScope(scope) {
      if (scope === this.scope) return false;
      this.scope = scope;
      this.entries = [];
      return true;
    }

    hydrate(entries) {
      this.entries = (Array.isArray(entries) ? entries : []).filter((entry) => entry?.id && entry?.documentRect);
    }

    merge(candidates, coveredRect) {
      const result = mergeRegions(this.entries, candidates, {
        maxEntries: MAX_STORE_ENTRIES,
        // "Same text, roughly here" has to mean something relative to how much
        // page the reader can see, not a fixed pixel count.
        textProximity: Math.max(1, window.innerHeight || 1) * 0.1,
        // Lets a patch the capture no longer finds be retired instead of
        // hanging over artwork that has no bubble.
        coveredRect
      });
      this.entries = result.entries;
      return result;
    }

    anchorKeys() {
      const keys = new Set();
      for (const entry of this.entries) {
        if (entry.anchor?.kind === "element") keys.add(entry.anchor.key);
      }
      return keys;
    }

    clear() {
      this.entries = [];
    }
  }

  class TranslationOverlay {
    constructor(controller) {
      this.controller = controller;
      this.host = document.createElement("webtoon-translation-overlay");
      this.host.setAttribute("aria-live", "polite");
      this.host.style.cssText = "all: initial; position: fixed; inset: 0; z-index: 2147483647; pointer-events: none;";
      this.shadow = this.host.attachShadow({ mode: "open" });
      this.shadow.innerHTML = `
        <link rel="stylesheet" href="${chrome.runtime.getURL("content.css")}">
        <section class="overlay-shell" aria-label="말풍선 번역 레이어">
          <div class="regions" part="regions"></div>
          <aside class="translation-toolbar" part="toolbar">
            <div class="status-line">
              <span class="status-dot" data-status-dot></span>
              <span class="status-text" data-status-text>대기 중</span>
            </div>
            <div class="toolbar-actions">
              <button type="button" data-action="refresh" title="현재 화면 다시 번역">↻</button>
              <button type="button" data-action="select-region" title="번역할 화면 영역 지정" aria-pressed="false">영역</button>
              <button type="button" data-action="original" title="번역을 숨기고 원문 보기" aria-pressed="false">원문</button>
              <button type="button" data-action="copy-original" title="현재 화면의 OCR 원문 전체 복사">복사</button>
              <button type="button" data-action="close" title="화면 번역 끄기">×</button>
            </div>
          </aside>
          <div class="region-selection" data-region-selection tabindex="-1" hidden>
            <div class="region-selection-surface" data-region-selection-surface aria-hidden="true"></div>
            <div class="region-selection-box" data-region-selection-box hidden></div>
            <aside class="region-selection-panel" aria-live="polite">
              <strong>번역할 영역을 드래그하세요</strong>
              <span data-region-selection-hint>선택한 영역만 OCR과 번역에 사용합니다.</span>
              <div class="region-selection-actions">
                <button type="button" data-action="selection-clear">전체 화면</button>
                <button type="button" data-action="selection-cancel">취소</button>
              </div>
            </aside>
          </div>
          <aside class="reload-required" data-reload-required role="alert" hidden>
            <strong>확장 프로그램이 새로고침되었습니다.</strong>
            <span>이 웹툰 탭도 새로고침한 뒤 번역을 다시 시작해 주세요.</span>
          </aside>
        </section>`;
      this.shell = this.shadow.querySelector(".overlay-shell");
      this.regions = this.shadow.querySelector(".regions");
      this.toolbar = this.shadow.querySelector(".translation-toolbar");
      this.statusText = this.shadow.querySelector("[data-status-text]");
      this.statusDot = this.shadow.querySelector("[data-status-dot]");
      this.regionButton = this.shadow.querySelector("[data-action=select-region]");
      this.originalButton = this.shadow.querySelector("[data-action=original]");
      this.reloadRequired = this.shadow.querySelector("[data-reload-required]");
      this.selectionLayer = this.shadow.querySelector("[data-region-selection]");
      this.selectionSurface = this.shadow.querySelector("[data-region-selection-surface]");
      this.selectionBox = this.shadow.querySelector("[data-region-selection-box]");
      this.selectionHint = this.shadow.querySelector("[data-region-selection-hint]");
      this.capturing = false;
      this.selectionActive = false;
      this.selectionDrag = null;
      // Mounted patches, keyed by store entry id. Anything scrolled out of the
      // viewport is removed from here so no stale translation stays on screen.
      this.nodes = new Map();
      this.hoveredId = null;

      this.shadow.querySelector("[data-action=refresh]").addEventListener("click", () => controller.scheduleScan("manual", true));
      this.regionButton.addEventListener("click", () => controller.startRegionSelection());
      this.originalButton.addEventListener("click", () => controller.toggleOriginal());
      this.shadow.querySelector("[data-action=copy-original]").addEventListener("click", () => {
        controller.copyOriginalText().catch((error) => {
          this.setStatus("error", error?.message || "원문을 복사하지 못했습니다.");
        });
      });
      this.shadow.querySelector("[data-action=close]").addEventListener("click", () => controller.disableFromToolbar());
      this.shadow.querySelector("[data-action=selection-clear]").addEventListener("click", () => controller.clearCaptureRegion());
      this.shadow.querySelector("[data-action=selection-cancel]").addEventListener("click", () => controller.cancelRegionSelection());
      this.selectionSurface.addEventListener("pointerdown", (event) => this.onSelectionPointerDown(event));
      this.selectionSurface.addEventListener("pointermove", (event) => this.onSelectionPointerMove(event));
      this.selectionSurface.addEventListener("pointerup", (event) => this.onSelectionPointerUp(event));
      this.selectionSurface.addEventListener("pointercancel", (event) => this.onSelectionPointerCancel(event));
      this.selectionSurface.addEventListener("wheel", (event) => event.preventDefault(), { passive: false });
      this.selectionLayer.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          controller.cancelRegionSelection();
        }
      });
    }

    mount() {
      if (this.host.isConnected) return;
      document.documentElement.append(this.host);
    }

    setCaptureRegionActive(captureRegion) {
      const active = Boolean(normalizeCaptureRegion(captureRegion));
      this.regionButton.classList.toggle("active", active);
      this.regionButton.setAttribute("aria-pressed", String(active));
      this.regionButton.title = active ? "번역할 화면 영역 다시 지정 (지정됨)" : "번역할 화면 영역 지정";
    }

    beginRegionSelection(captureRegion) {
      if (this.selectionActive) return;
      const selectedRegion = normalizeCaptureRegion(captureRegion);
      this.selectionActive = true;
      this.selectionDrag = null;
      this.selectionLayer.hidden = false;
      this.selectionLayer.classList.remove("is-saving");
      this.regions.style.visibility = "hidden";
      this.setCaptureRegionActive(selectedRegion);
      this.setSelectionHint(
        selectedRegion
          ? "현재 범위가 표시되어 있어요. 새 범위를 드래그하거나 전체 화면을 누르세요."
          : "선택한 영역만 OCR과 번역에 사용합니다."
      );

      if (selectedRegion) {
        this.updateSelectionBox(this.captureRegionToViewportRect(selectedRegion));
      } else {
        this.selectionBox.hidden = true;
      }

      requestAnimationFrame(() => {
        if (this.selectionActive) this.selectionLayer.focus({ preventScroll: true });
      });
    }

    endRegionSelection() {
      if (!this.selectionActive) return;
      if (this.selectionDrag && this.selectionSurface.hasPointerCapture(this.selectionDrag.pointerId)) {
        this.selectionSurface.releasePointerCapture(this.selectionDrag.pointerId);
      }
      this.selectionActive = false;
      this.selectionDrag = null;
      this.selectionLayer.hidden = true;
      this.selectionLayer.classList.remove("is-saving");
      this.selectionBox.hidden = true;
      this.regions.style.visibility = "";
    }

    setSelectionHint(text) {
      this.selectionHint.textContent = text;
    }

    viewportPoint(event) {
      const width = Math.max(1, window.innerWidth);
      const height = Math.max(1, window.innerHeight);
      return {
        x: clamp(Number(event.clientX) || 0, 0, width),
        y: clamp(Number(event.clientY) || 0, 0, height)
      };
    }

    selectionRect(start, end) {
      const x = Math.min(start.x, end.x);
      const y = Math.min(start.y, end.y);
      return {
        x,
        y,
        width: Math.abs(end.x - start.x),
        height: Math.abs(end.y - start.y)
      };
    }

    captureRegionToViewportRect(region) {
      const normalized = normalizeCaptureRegion(region);
      const width = Math.max(1, window.innerWidth);
      const height = Math.max(1, window.innerHeight);
      return normalized
        ? {
            x: normalized.x * width,
            y: normalized.y * height,
            width: normalized.width * width,
            height: normalized.height * height
          }
        : null;
    }

    selectionRectToCaptureRegion(rect) {
      const width = Math.max(1, window.innerWidth);
      const height = Math.max(1, window.innerHeight);
      if (rect.width < MIN_CAPTURE_REGION_PIXELS || rect.height < MIN_CAPTURE_REGION_PIXELS) return null;
      return normalizeCaptureRegion({
        x: rect.x / width,
        y: rect.y / height,
        width: rect.width / width,
        height: rect.height / height
      });
    }

    updateSelectionBox(rect) {
      if (!rect || rect.width <= 0 || rect.height <= 0) {
        this.selectionBox.hidden = true;
        return;
      }
      this.selectionBox.style.left = `${rect.x}px`;
      this.selectionBox.style.top = `${rect.y}px`;
      this.selectionBox.style.width = `${rect.width}px`;
      this.selectionBox.style.height = `${rect.height}px`;
      this.selectionBox.hidden = false;
    }

    onSelectionPointerDown(event) {
      if (!this.selectionActive || this.selectionLayer.classList.contains("is-saving") || event.button !== 0) return;
      event.preventDefault();
      const point = this.viewportPoint(event);
      this.selectionDrag = { pointerId: event.pointerId, start: point, end: point };
      this.selectionSurface.setPointerCapture(event.pointerId);
      this.updateSelectionBox(this.selectionRect(point, point));
      this.setSelectionHint("드래그를 마치면 선택한 영역에 바로 번역을 표시합니다.");
    }

    onSelectionPointerMove(event) {
      if (!this.selectionDrag || this.selectionDrag.pointerId !== event.pointerId) return;
      event.preventDefault();
      this.selectionDrag.end = this.viewportPoint(event);
      this.updateSelectionBox(this.selectionRect(this.selectionDrag.start, this.selectionDrag.end));
    }

    onSelectionPointerUp(event) {
      if (!this.selectionDrag || this.selectionDrag.pointerId !== event.pointerId) return;
      event.preventDefault();
      const drag = this.selectionDrag;
      this.selectionDrag = null;
      if (this.selectionSurface.hasPointerCapture(event.pointerId)) this.selectionSurface.releasePointerCapture(event.pointerId);
      const rect = this.selectionRect(drag.start, this.viewportPoint(event));
      const captureRegion = this.selectionRectToCaptureRegion(rect);
      if (!captureRegion) {
        this.setSelectionHint(`가로와 세로를 각각 ${MIN_CAPTURE_REGION_PIXELS}px 이상 드래그해 주세요.`);
        return;
      }

      this.selectionLayer.classList.add("is-saving");
      this.setSelectionHint("선택 영역을 적용하는 중…");
      Promise.resolve(this.controller.applyCaptureRegion(captureRegion)).then((applied) => {
        if (applied || !this.selectionActive) return;
        this.selectionLayer.classList.remove("is-saving");
        this.setSelectionHint("영역을 적용하지 못했습니다. 다시 드래그해 주세요.");
      });
    }

    onSelectionPointerCancel(event) {
      if (!this.selectionDrag || this.selectionDrag.pointerId !== event.pointerId) return;
      this.selectionDrag = null;
      if (this.selectionSurface.hasPointerCapture(event.pointerId)) this.selectionSurface.releasePointerCapture(event.pointerId);
      this.setSelectionHint("드래그가 취소됐습니다. 다시 범위를 지정해 주세요.");
    }

    setEnabled(enabled) {
      this.host.style.display = enabled ? "block" : "none";
      if (!enabled) {
        this.endRegionSelection();
        this.clearRegions();
      }
    }

    setCapturing(capturing) {
      this.capturing = capturing;
      if (!this.reloadRequired.hidden) {
        this.host.style.visibility = "visible";
        return;
      }
      if (this.selectionActive) {
        this.host.style.visibility = "visible";
        return;
      }
      this.host.style.visibility = capturing ? "hidden" : "visible";
    }

    showReloadRequired() {
      this.endRegionSelection();
      this.clearRegions();
      this.toolbar.hidden = true;
      this.reloadRequired.hidden = false;
      this.host.style.display = "block";
      this.host.style.visibility = "visible";
    }

    setStatus(status, text, detail = "") {
      this.statusText.textContent = text;
      this.statusText.title = detail || text;
      this.statusDot.dataset.state = status;
    }

    clearRegions() {
      this.nodes.clear();
      this.hoveredId = null;
      this.regions.replaceChildren();
    }

    createRegionNode() {
      const box = document.createElement("article");
      box.className = "translation-region";
      const copy = document.createElement("span");
      copy.className = "translation-copy";
      box.append(copy);
      return box;
    }

    fitText(box, fontScale) {
      const copy = box.querySelector(".translation-copy");
      if (!copy) return;

      const availableWidth = box.clientWidth;
      const availableHeight = box.clientHeight;
      if (!availableWidth || !availableHeight) return;

      // A translated sentence can occupy more space than the source line. Fit after
      // the browser has wrapped Korean text so it stays inside the complete bubble box.
      const characters = Array.from(copy.textContent || "").length || 1;
      const densitySize = Math.sqrt((availableWidth * availableHeight) / Math.max(1, characters * 0.68));
      const maximum = Math.max(8, Math.min(30, densitySize * Number(fontScale || 1), availableHeight * 0.82));
      const minimum = Math.max(6, Math.min(11, availableHeight * 0.42, availableWidth * 0.12));
      let fontSize = Math.max(minimum, maximum);

      box.classList.remove("is-overflowing");
      for (; fontSize >= minimum; fontSize -= 0.5) {
        copy.style.fontSize = `${fontSize.toFixed(1)}px`;
        if (copy.scrollHeight <= availableHeight + 1 && copy.scrollWidth <= availableWidth + 1) break;
      }

      // Do not crop a long translation even in a very small OCR region. The grouping
      // service normally gives this text a full bubble-sized box; this fallback keeps
      // the last line visible while that response is loading or for edge cases.
      if (copy.scrollHeight > availableHeight + 1) {
        box.classList.add("is-overflowing");
      }
    }

    // Mount, move, or drop each stored translation for the current scroll
    // position. This is what makes a bubble's patch disappear once its bubble
    // leaves the screen and come straight back when it returns.
    sync(entries, settings, frame, elementRects) {
      const showOriginal = Boolean(settings?.showOriginal);
      const fontScale = clamp(Number(settings?.fontScale || 1), 0.7, 1.7);
      // Remembered by the hover reveal, which has to re-fit whatever it swaps in.
      this.showOriginal = showOriginal;
      this.fontScale = fontScale;
      const viewportBounds = { left: 0, top: 0, width: frame.width, height: frame.height };
      this.regions.style.setProperty("--overlay-opacity", String(settings?.overlayOpacity ?? 0.93));

      const pendingFit = [];
      const live = new Set();
      let visible = 0;

      for (const entry of entries) {
        const documentRect = resolveAnchorRect(entry.anchor, elementRects) || entry.documentRect;
        if (!documentRect) continue;
        const rect = toViewportRect(documentRect, frame);
        if (!rectsIntersect(rect, viewportBounds, VISIBLE_MARGIN_PX)) continue;

        const original = String(entry.text || "").trim();
        const translation = String(entry.translation || "").trim();
        const displayedText = showOriginal ? original : translation || original;
        if (!displayedText) continue;

        live.add(entry.id);
        visible += 1;
        let box = this.nodes.get(entry.id);
        if (!box) {
          box = this.createRegionNode();
          this.nodes.set(entry.id, box);
          this.regions.append(box);
        }

        // Position with a transform so following the scroll stays on the
        // compositor; only width/height, which change rarely, touch layout.
        box.style.transform = `translate3d(${rect.left.toFixed(1)}px, ${rect.top.toFixed(1)}px, 0)`;
        const size = `${rect.width.toFixed(1)}px|${rect.height.toFixed(1)}px`;
        if (box.dataset.size !== size) {
          box.dataset.size = size;
          box.style.width = `${rect.width.toFixed(1)}px`;
          box.style.height = `${rect.height.toFixed(1)}px`;
        }
        // Hit testing for the hover reveal reads these instead of the styles, so
        // it never has to parse a transform.
        box.dataset.hitLeft = String(rect.left);
        box.dataset.hitTop = String(rect.top);
        box.dataset.hitRight = String(rect.left + rect.width);
        box.dataset.hitBottom = String(rect.top + rect.height);
        box.classList.toggle("show-original", showOriginal);
        box.setAttribute(
          "aria-label",
          showOriginal ? `원문 표시: ${original || translation}` : `한국어 번역: ${translation || original}`
        );

        const copy = box.querySelector(".translation-copy");
        copy.lang = showOriginal ? settings?.sourceLanguage || "" : settings?.targetLanguage || "ko";
        // Both texts stay on the node so the hover reveal can show the
        // translation of a bubble that is currently displaying its original.
        box.dataset.displayText = displayedText;
        box.dataset.revealText = translation || original;
        box.dataset.revealLang = settings?.targetLanguage || "ko";
        box.dataset.displayLang = copy.lang;
        if (box.classList.contains("is-hover-revealed")) {
          if (copy.textContent !== box.dataset.revealText) copy.textContent = box.dataset.revealText;
        } else if (copy.textContent !== displayedText) {
          copy.textContent = displayedText;
        }

        // Refitting every patch on every scroll frame would force a layout per
        // bubble. Only the ones whose box or text actually changed need it.
        const fitKey = `${rect.width.toFixed(1)}x${rect.height.toFixed(1)}|${fontScale}|${displayedText}`;
        if (box.dataset.fitKey !== fitKey) {
          box.dataset.fitKey = fitKey;
          pendingFit.push(box);
        }
      }

      for (const [id, box] of this.nodes) {
        if (live.has(id)) continue;
        box.remove();
        this.nodes.delete(id);
        if (this.hoveredId === id) this.hoveredId = null;
      }

      if (pendingFit.length) {
        requestAnimationFrame(() => {
          for (const box of pendingFit) {
            if (box.isConnected) this.fitText(box, fontScale);
          }
        });
      }

      this.originalButton.classList.toggle("active", showOriginal);
      this.originalButton.setAttribute("aria-pressed", String(showOriginal));
      return visible;
    }

    // Reveal a single remembered translation under the pointer. Hit testing is
    // done against the mounted patches rather than by making the overlay
    // interactive, so the page keeps every pointer event it would normally get.
    updateHover(point) {
      let hoveredId = null;
      if (point) {
        for (const [id, box] of this.nodes) {
          const left = Number(box.dataset.hitLeft);
          const top = Number(box.dataset.hitTop);
          const right = Number(box.dataset.hitRight);
          const bottom = Number(box.dataset.hitBottom);
          if (![left, top, right, bottom].every(Number.isFinite)) continue;
          if (point.x < left || point.y < top || point.x > right || point.y > bottom) continue;
          hoveredId = id;
          break;
        }
      }
      if (hoveredId === this.hoveredId) return;

      const previous = this.hoveredId ? this.nodes.get(this.hoveredId) : null;
      if (previous) {
        previous.classList.remove("is-hover-revealed");
        this.setRegionText(previous, previous.dataset.displayText, previous.dataset.displayLang);
      }
      this.hoveredId = hoveredId;
      if (!hoveredId) return;

      const box = this.nodes.get(hoveredId);
      if (!box) return;
      box.classList.add("is-hover-revealed");
      // Revealing the original again would show what the artwork already says.
      // What the reader wants back is the translation.
      this.setRegionText(box, box.dataset.revealText, box.dataset.revealLang);
    }

    setRegionText(box, text, lang) {
      const copy = box.querySelector(".translation-copy");
      if (!copy || !text || copy.textContent === text) return;
      copy.textContent = text;
      copy.lang = lang || copy.lang;
      // The swapped-in text is a different length, so the fitted size no longer
      // applies. Force a refit on the next frame.
      box.dataset.fitKey = "";
      requestAnimationFrame(() => {
        if (box.isConnected) this.fitText(box, this.fontScale || 1);
      });
    }

    visibleEntryIds() {
      return new Set(this.nodes.keys());
    }
  }

  class TranslatorController {
    constructor() {
      this.enabled = false;
      this.inFlight = false;
      this.timer = null;
      this.pendingScan = null;
      this.store = new RegionStore();
      // Document ranges already read for the current layout, and the context
      // they were read in. Together they let a scroll capture only the unread
      // strip, and let a return to earlier page skip capturing entirely.
      this.coverage = [];
      this.coverageContext = null;
      this.consecutiveBandScans = 0;
      this.syncHandle = 0;
      this.hoverHandle = 0;
      this.pointer = null;
      this.memorySaveTimer = null;
      // A nested scroller moves the artwork without changing window.scrollY, so
      // document-anchored patches cannot be trusted until the next capture.
      this.documentAnchorsStale = false;
      this.lastViewportChangeAt = 0;
      this.captureSequence = 0;
      this.activeCaptureToken = null;
      this.settings = null;
      this.glossary = [];
      // Intentionally session-only: a crop chosen on one webtoon/tab must not
      // silently constrain unrelated sites or the next navigation.
      this.captureRegion = null;
      // `null` is also the representation for an explicitly selected "full
      // screen" capture. Keep its confirmation separate so enabling the
      // extension never falls through to an accidental full-screen scan before
      // the reader has made a choice.
      this.captureRegionSelected = false;
      this.contextInvalidated = false;
      this.overlay = new TranslationOverlay(this);
      this.onViewportChange = this.onViewportChange.bind(this);
      this.onPointerMove = this.onPointerMove.bind(this);
      this.onPointerLeave = this.onPointerLeave.bind(this);
      this.onResourceLoad = this.onResourceLoad.bind(this);
    }

    handleExtensionContextInvalidated(error) {
      if (!isExtensionContextInvalidated(error)) return false;
      if (this.contextInvalidated) return true;

      // Reloading an extension invalidates the old content-script world while
      // its DOM overlay can remain in the page. Do not keep making Chrome API
      // calls from that stale world, and do not leave stale translations up.
      this.contextInvalidated = true;
      this.enabled = false;
      this.pendingScan = null;
      this.store.clear();
      this.activeCaptureToken = null;
      this.captureRegion = null;
      this.captureRegionSelected = false;
      clearTimeout(this.timer);
      clearTimeout(this.memorySaveTimer);
      this.timer = null;
      this.memorySaveTimer = null;
      this.overlay.showReloadRequired();
      return true;
    }

    async initialize() {
      this.overlay.mount();
      try {
        [this.settings, this.glossary] = await Promise.all([getSettings(), getGlossary()]);
        if (this.contextInvalidated) return;
        const state = await chrome.runtime.sendMessage({ type: "GET_TAB_STATE" });
        if (this.contextInvalidated) return;
        this.enabled = Boolean(state?.enabled);
        this.store.setScope(getEpisodeCacheScope());
        this.overlay.setEnabled(this.enabled);
        this.overlay.setCaptureRegionActive(this.captureRegion);
        this.overlay.setStatus("idle", this.enabled ? "화면을 인식할 준비가 됐어요" : "번역 꺼짐");

        window.addEventListener("scroll", this.onViewportChange, { passive: true, capture: true });
        window.addEventListener("resize", this.onViewportChange, { passive: true });
        window.visualViewport?.addEventListener("resize", this.onViewportChange, { passive: true });
        window.addEventListener("pointermove", this.onPointerMove, { passive: true });
        window.addEventListener("pointerleave", this.onPointerLeave, { passive: true });
        // A panel that finishes decoding can reflow everything below it, so the
        // page needs rescanning even though nothing scrolled.
        window.addEventListener("load", this.onResourceLoad, { capture: true });
        document.addEventListener("visibilitychange", () => {
          if (document.visibilityState === "visible") this.scheduleScan("tab-visible");
        });

        chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
          if (this.contextInvalidated) return;
          if (message.type === "TRANSLATOR_STATE") {
            this.setEnabled(Boolean(message.enabled)).catch((error) => this.handleExtensionContextInvalidated(error));
            sendResponse({ ok: true });
          }
          if (message.type === "REQUEST_RESCAN") {
            this.scheduleScan(message.reason || "manual", true);
            sendResponse({ ok: true });
          }
          if (message.type === "SETTINGS_CHANGED") {
            this.reloadSettings(true);
            sendResponse({ ok: true });
          }
          if (message.type === "CAPTURE_COMPLETED") {
            sendResponse({ ok: this.completeCapture(message.captureToken) });
          }
          if (message.type === "START_REGION_SELECTION") {
            sendResponse({ ok: this.startRegionSelection() });
          }
          if (message.type === "CLEAR_CAPTURE_REGION") {
            sendResponse({ ok: this.clearCaptureRegion() });
          }
          if (message.type === "GET_ORIGINAL_TEXT") {
            sendResponse({ ok: true, ...this.getOriginalText() });
          }
        });

        chrome.storage.onChanged.addListener((changes) => {
          if (changes.settings || changes.glossary) this.reloadSettings(true);
        });

        await this.hydrateRegionMemory();
        if (this.enabled) {
          this.startCaptureRegionSelectionIfNeeded();
          this.scheduleScan("start", true);
        }
      } catch (error) {
        if (this.handleExtensionContextInvalidated(error)) return;
        throw error;
      }
    }

    // Restore this episode's translations after a reload inside the same tab
    // session, so the reader sees them before any capture happens.
    async hydrateRegionMemory() {
      if (this.contextInvalidated) return;
      try {
        const response = await chrome.runtime.sendMessage({
          type: "GET_REGION_MEMORY",
          cacheScope: this.store.scope
        });
        if (!response?.ok || !Array.isArray(response.entries) || !response.entries.length) return;
        this.store.hydrate(response.entries);
        this.syncOverlays();
      } catch (error) {
        if (this.handleExtensionContextInvalidated(error)) return;
        // A missing memory only costs a rescan; never block startup on it.
        console.warn("이전 번역 기억을 불러오지 못했습니다.", error);
      }
    }

    scheduleRegionMemorySave() {
      if (this.contextInvalidated) return;
      clearTimeout(this.memorySaveTimer);
      this.memorySaveTimer = setTimeout(() => {
        this.memorySaveTimer = null;
        chrome.runtime
          .sendMessage({
            type: "SAVE_REGION_MEMORY",
            cacheScope: this.store.scope,
            entries: this.store.entries
          })
          .catch((error) => {
            if (this.handleExtensionContextInvalidated(error)) return;
            console.warn("번역 기억을 저장하지 못했습니다.", error);
          });
      }, REGION_MEMORY_SAVE_DELAY_MS);
    }

    syncOverlays() {
      if (this.contextInvalidated || !this.enabled || !this.settings) return 0;
      const frame = getViewportFrame();
      const entries = this.documentAnchorsStale
        ? this.store.entries.filter((entry) => entry.anchor?.kind === "element")
        : this.store.entries;
      const elementRects = elementRectsForKeys(this.store.anchorKeys(), frame);
      const visible = this.overlay.sync(entries, this.settings, frame, elementRects);
      this.overlay.updateHover(this.settings.hoverReveal === false ? null : this.pointer);
      return visible;
    }

    scheduleSync() {
      if (this.syncHandle) return;
      this.syncHandle = requestAnimationFrame(() => {
        this.syncHandle = 0;
        this.syncOverlays();
      });
    }

    onViewportChange(event) {
      if (this.contextInvalidated) return;
      this.lastViewportChangeAt = Date.now();
      if (this.overlay.selectionActive) {
        this.overlay.endRegionSelection();
        this.overlay.setStatus("idle", "화면이 바뀌어 영역 지정을 취소했습니다.");
      }
      if (event?.type === "scroll" && !isRootScrollEvent(event)) {
        // Artwork inside a nested scroller moved without a document scroll.
        // Element anchors still track it; plain document anchors cannot, and
        // window.scrollY cannot tell the band planner that new page arrived.
        this.documentAnchorsStale = true;
        this.invalidateCoverage();
      }
      if (event?.type === "resize") this.invalidateCoverage();
      // Patches are anchored in document/element space, so they simply follow
      // the page instead of being thrown away and re-fetched.
      this.scheduleSync();
      this.scheduleScan("viewport-change");
    }

    onResourceLoad(event) {
      if (this.contextInvalidated || !this.enabled || !(event?.target instanceof HTMLImageElement)) return;

      // A panel decoding far below the reader changes nothing that has been
      // read yet. Only a panel at or above the viewport can reflow it, and
      // treating every lazy load as a reflow would cancel incremental captures
      // for the whole episode.
      const rect = event.target.getBoundingClientRect();
      if (rect.top > Math.max(1, window.innerHeight)) return;
      this.invalidateCoverage();
      this.scheduleSync();
      this.scheduleScan("image-loaded");
    }

    onPointerMove(event) {
      if (this.contextInvalidated || !this.enabled) return;
      this.pointer = { x: event.clientX, y: event.clientY };
      if (this.settings?.hoverReveal === false || this.hoverHandle) return;
      this.hoverHandle = requestAnimationFrame(() => {
        this.hoverHandle = 0;
        this.overlay.updateHover(this.pointer);
      });
    }

    onPointerLeave() {
      this.pointer = null;
      this.overlay.updateHover(null);
    }

    // A reflow, resize, or new crop makes previously scanned page ranges
    // unreliable, so the next capture must read the whole selected region again.
    invalidateCoverage() {
      this.coverage = [];
      this.coverageContext = null;
      this.consecutiveBandScans = 0;
    }

    coverageContextKey(frame) {
      return [
        frame.width,
        frame.height,
        frame.devicePixelRatio,
        JSON.stringify(this.captureRegion)
      ].join("|");
    }

    completeCapture(captureToken) {
      if (!Number.isInteger(captureToken) || captureToken !== this.activeCaptureToken) return false;
      // The screenshot already exists in the service worker. Leave prior patches
      // visible during the slower OCR/translation portion of the request.
      this.overlay.setCapturing(false);
      return true;
    }

    async reloadSettings(rescan) {
      if (this.contextInvalidated) return false;
      try {
        [this.settings, this.glossary] = await Promise.all([getSettings(), getGlossary()]);
        if (this.contextInvalidated) return false;
        this.overlay.setCaptureRegionActive(this.captureRegion);
        this.syncOverlays();
        if (rescan && this.enabled) this.scheduleScan("settings-change", true);
        return true;
      } catch (error) {
        if (this.handleExtensionContextInvalidated(error)) return false;
        this.overlay.setStatus("error", "번역 설정을 다시 읽지 못했습니다.");
        console.warn("말풍선 번역 설정을 읽지 못했습니다.", error);
        return false;
      }
    }

    async setEnabled(enabled) {
      if (this.contextInvalidated) return;
      this.enabled = enabled;
      clearTimeout(this.timer);
      this.timer = null;
      this.pendingScan = null;
      this.overlay.setEnabled(enabled);
      if (!enabled) {
        this.overlay.setStatus("idle", "번역 꺼짐");
        return;
      }
      if (!(await this.reloadSettings(false))) return;
      this.overlay.setStatus("idle", "화면을 인식할 준비가 됐어요");
      this.syncOverlays();
      this.startCaptureRegionSelectionIfNeeded();
      this.scheduleScan("enabled", true);
    }

    async disableFromToolbar() {
      if (this.contextInvalidated) return;
      try {
        const response = await chrome.runtime.sendMessage({ type: "SET_TAB_ENABLED", enabled: false });
        if (!response?.ok) throw new Error(response?.error || "상태 저장에 실패했습니다.");
      } catch (error) {
        if (this.handleExtensionContextInvalidated(error)) return;
        // The visual layer should still disappear if the service worker was restarted.
        await this.setEnabled(false);
        console.warn("말풍선 번역 상태를 저장하지 못했습니다.", error);
      }
    }

    async toggleOriginal() {
      if (this.contextInvalidated) return;
      try {
        this.settings = await globalThis.WebtoonTranslator.saveSettings({ showOriginal: !this.settings.showOriginal });
        this.syncOverlays();
      } catch (error) {
        if (this.handleExtensionContextInvalidated(error)) return;
        this.overlay.setStatus("error", "원문 표시 설정을 저장하지 못했습니다.");
        console.warn("원문 표시 설정을 저장하지 못했습니다.", error);
      }
    }

    startRegionSelection() {
      if (this.contextInvalidated || !this.enabled) {
        this.overlay.setStatus("error", "먼저 화면 번역을 시작해 주세요.");
        return false;
      }
      if (this.inFlight) {
        this.overlay.setStatus("working", "현재 화면 번역이 끝난 뒤 영역을 지정해 주세요.");
        return false;
      }
      clearTimeout(this.timer);
      this.timer = null;
      this.pendingScan = null;
      this.overlay.beginRegionSelection(this.captureRegion);
      return true;
    }

    startCaptureRegionSelectionIfNeeded() {
      if (this.contextInvalidated || !this.enabled || this.captureRegionSelected || this.overlay.selectionActive) {
        return false;
      }
      const started = this.startRegionSelection();
      if (started) this.overlay.setStatus("idle", "번역할 화면 영역을 드래그해 주세요.");
      return started;
    }

    cancelRegionSelection() {
      if (!this.overlay.selectionActive) return false;
      this.overlay.endRegionSelection();
      this.overlay.setCaptureRegionActive(this.captureRegion);
      this.overlay.setStatus("idle", this.captureRegion ? "지정한 영역만 인식합니다." : "영역 지정을 취소했습니다.");
      this.syncOverlays();
      if (this.captureRegionSelected) this.scheduleScan("selection-cancelled", true);
      return true;
    }

    applyCaptureRegion(region) {
      const captureRegion = normalizeCaptureRegion(region);
      if (this.contextInvalidated || !captureRegion) return false;
      this.captureRegion = captureRegion;
      this.captureRegionSelected = true;
      this.resetForNewCaptureRegion();
      this.overlay.setCaptureRegionActive(captureRegion);
      this.overlay.setStatus("idle", "지정한 영역만 인식합니다.");
      this.scheduleScan("capture-region", true);
      return true;
    }

    clearCaptureRegion() {
      if (this.contextInvalidated) return false;
      this.captureRegion = null;
      // This is an explicit choice from the selection dialog ("전체 화면"), not
      // an unset region. It may therefore scan the whole viewport.
      this.captureRegionSelected = true;
      this.resetForNewCaptureRegion();
      this.overlay.setCaptureRegionActive(null);
      this.overlay.setStatus("idle", "전체 화면을 인식합니다.");
      this.scheduleScan("capture-region-cleared", true);
      return true;
    }

    resetForNewCaptureRegion() {
      // A different crop reads a different part of the page. Keeping patches
      // from the old crop would leave translations outside the new region.
      this.store.clear();
      this.invalidateCoverage();
      this.overlay.endRegionSelection();
      this.overlay.clearRegions();
      this.scheduleRegionMemorySave();
    }

    getOriginalText() {
      const visible = this.overlay.visibleEntryIds();
      const text = this.store.entries
        .filter((entry) => visible.has(entry.id))
        .map((entry) => String(entry.text || "").trim())
        .filter(Boolean);
      return {
        text: text.join("\n\n"),
        count: text.length
      };
    }

    async appendTranslationLog(cacheScope, candidates, scanSettings) {
      if (this.contextInvalidated) return;
      const entries = candidates
        .map((candidate) => ({
          original: String(candidate?.text || "").trim(),
          translation: String(candidate?.translation || "").trim()
        }))
        .filter((entry) => entry.original && entry.translation);
      if (!entries.length) return;

      try {
        const response = await chrome.runtime.sendMessage({
          type: "APPEND_TRANSLATION_LOG",
          cacheScope,
          sourceLanguage: scanSettings?.sourceLanguage,
          targetLanguage: scanSettings?.targetLanguage,
          provider: candidates[0]?.provider || scanSettings?.translationProvider,
          entries
        });
        if (!response?.ok) throw new Error(response?.error || "번역 로그를 저장하지 못했습니다.");
      } catch (error) {
        if (this.handleExtensionContextInvalidated(error)) return;
        // Logging is best-effort. A temporary session-storage failure should not
        // hide an already rendered translation from the reader.
        console.warn("번역 로그를 저장하지 못했습니다.", error);
      }
    }

    async copyOriginalText() {
      const original = this.getOriginalText();
      if (!original.text) {
        throw new Error("복사할 OCR 원문이 없습니다. 먼저 화면 번역을 실행해 주세요.");
      }
      await writeTextToClipboard(original.text);
      this.overlay.setStatus("ready", `원문 ${original.count}개를 클립보드에 복사했습니다.`);
      return original;
    }

    scheduleScan(reason, force = false) {
      if (this.contextInvalidated || !this.enabled || this.overlay.selectionActive || document.visibilityState !== "visible") return;
      if (!this.captureRegionSelected) {
        this.startCaptureRegionSelectionIfNeeded();
        return;
      }
      if (!force && !this.settings?.autoScan) return;
      if (this.inFlight) {
        this.pendingScan = { reason, force };
        return;
      }
      clearTimeout(this.timer);
      const configuredDelay = Math.max(200, Number(this.settings?.scanDelay) || 500);
      // Every scroll event restarts this timer, so a reader who is still moving
      // never triggers a capture: the page has to sit still for the whole window
      // first. When an OCR request instead finishes after the reader already
      // stopped, the wait is credited so they do not sit through it twice.
      const followsViewportChange = reason === "viewport-change" || reason === "viewport-changed-during-scan";
      const elapsedSinceViewportChange = followsViewportChange && this.lastViewportChangeAt
        ? Math.max(0, Date.now() - this.lastViewportChangeAt)
        : 0;
      const delay = force ? 0 : Math.max(0, configuredDelay - elapsedSinceViewportChange);
      this.timer = setTimeout(() => this.scan(reason), delay);
    }

    // The crop the server actually read, in document coordinates.
    capturedDocumentRect(payload, frame) {
      const captureRegion = normalizeCaptureRegion(payload?.captureRegion) || { x: 0, y: 0, width: 1, height: 1 };
      return {
        left: captureRegion.x * frame.width + frame.scrollX,
        top: captureRegion.y * frame.height + frame.scrollY,
        width: captureRegion.width * frame.width,
        height: captureRegion.height * frame.height
      };
    }

    // Map a rectangle from cropped-image pixels into document coordinates.
    documentRectFromImage(rect, payload, frame) {
      const imageWidth = Number(payload.imageWidth) || 1;
      const imageHeight = Number(payload.imageHeight) || 1;
      const captureRegion = normalizeCaptureRegion(payload.captureRegion) || { x: 0, y: 0, width: 1, height: 1 };
      const left = (captureRegion.x + (Number(rect.x) / imageWidth) * captureRegion.width) * frame.width;
      const top = (captureRegion.y + (Number(rect.y) / imageHeight) * captureRegion.height) * frame.height;
      const width = (Number(rect.width) / imageWidth) * captureRegion.width * frame.width;
      const height = (Number(rect.height) / imageHeight) * captureRegion.height * frame.height;
      if (!(width > 0) || !(height > 0)) return null;
      return { left: left + frame.scrollX, top: top + frame.scrollY, width, height };
    }

    // Convert one server region from cropped-image pixels into a document rect,
    // then anchor it to the artwork it sits on.
    regionCandidate(region, payload, frame, currentFrame, scanSettings) {
      const documentRect = this.documentRectFromImage(region, payload, frame);
      if (!documentRect) return null;
      return {
        anchor: anchorForDocumentRect(documentRect, currentFrame),
        documentRect,
        text: String(region.text || "").trim(),
        translation: String(region.translation || "").trim(),
        confidence: Number(region.confidence) || 0,
        sourceLanguage: scanSettings?.sourceLanguage || "",
        targetLanguage: scanSettings?.targetLanguage || "",
        provider: payload.translationProvider || scanSettings?.translationProvider || ""
      };
    }

    async scan(reason) {
      if (this.contextInvalidated || !this.enabled || !this.captureRegionSelected || this.overlay.selectionActive || this.inFlight) return;
      this.inFlight = true;
      this.timer = null;
      const captureToken = ++this.captureSequence;
      this.activeCaptureToken = captureToken;
      const scanSettings = this.settings;
      const startedAt = performance.now();
      this.overlay.setStatus("working", reason === "manual" ? "다시 읽는 중" : "말풍선을 읽는 중");

      try {
        if (reason === "manual") this.invalidateCoverage();

        // Work out how little of the page actually needs reading before paying
        // for a screenshot at all.
        const plannedFrame = getViewportFrame();
        const contextKey = this.coverageContextKey(plannedFrame);
        const plan = incrementalBand({
          coverage: this.coverage,
          viewport: { pageTop: plannedFrame.scrollY, height: plannedFrame.height },
          captureRegion: this.captureRegion,
          sameContext: contextKey === this.coverageContext && this.consecutiveBandScans < MAX_CONSECUTIVE_BAND_SCANS
        });

        if (plan.mode === "none") {
          // Everything on screen was already read. The store repaints it with
          // no capture, no OCR, and no network call.
          const restored = this.syncOverlays();
          this.overlay.setStatus(
            "ready",
            restored ? `${restored}개 말풍선 번역 표시 중` : "읽을 글자를 찾지 못했어요",
            "이미 읽은 구간이라 기억해 둔 번역을 그대로 표시했습니다."
          );
          return;
        }

        // 캡처에 직전 번역 패치가 들어가지 않도록 한 프레임 동안 숨깁니다.
        // Two animation frames guarantee one paint with the old translation
        // patches hidden. With nothing mounted there is nothing to hide, so the
        // first scan of a screen does not pay for them.
        this.overlay.setCapturing(true);
        if (this.overlay.nodes.size) {
          await nextFrame();
          await nextFrame();
        }
        const frame = getViewportFrame();
        const glossary = entriesForLanguage(this.glossary, this.settings.sourceLanguage, this.settings.targetLanguage);
        const cacheScope = getEpisodeCacheScope();
        if (this.store.setScope(cacheScope)) {
          // A same-tab chapter change discards the store, so nothing on this
          // page has been read yet.
          this.invalidateCoverage();
          this.overlay.clearRegions();
        }

        const requestedAt = performance.now();
        const response = await chrome.runtime.sendMessage({
          type: "TRANSLATE_VISIBLE_FRAME",
          settings: this.settings,
          glossary,
          cacheScope,
          // Either the reader's whole selection, or just the strip that scrolled
          // into view since the last read.
          captureRegion: plan.captureRegion,
          // The service worker compares screenshot pixels with this CSS
          // viewport size before downscaling a high-DPI capture.  It keeps
          // enough pixels for small lettering, while leaving overlay
          // coordinates in the same normalized capture-region system.
          captureViewport: {
            width: frame.width,
            height: frame.height,
            devicePixelRatio: frame.devicePixelRatio
          },
          // A manual refresh is an explicit "read this again", so it must not
          // be answered from the unchanged-frame shortcut.
          bypassFrameSkip: reason === "manual",
          captureToken
        });
        const respondedAt = performance.now();
        if (!response?.ok) throw new Error(response?.error || "화면 번역 요청에 실패했습니다.");
        if (response.throttled) {
          this.overlay.setStatus("idle", "잠시 뒤 다시 읽습니다");
          setTimeout(() => this.scheduleScan("capture-limit", true), response.retryAfter || 400);
          return;
        }
        if (!this.enabled) return;
        if (response.unchanged) {
          // Nothing on screen changed since the last read, so the store already
          // describes it. Repaint from memory and skip OCR entirely.
          const visible = this.syncOverlays();
          this.overlay.setStatus(
            "ready",
            visible ? `${visible}개 말풍선 번역 표시 중` : "읽을 글자를 찾지 못했어요",
            "화면이 그대로여서 다시 인식하지 않았습니다."
          );
          return;
        }

        // Patches live in document space now, so a scroll during OCR no longer
        // invalidates the result; the regions simply land where they belong.
        const currentFrame = getViewportFrame();
        const candidates = [];
        for (const region of response.payload?.regions || []) {
          const candidate = this.regionCandidate(region, response.payload, frame, currentFrame, scanSettings);
          if (candidate && (candidate.text || candidate.translation)) candidates.push(candidate);
        }

        // A bubble the server refused to translate because the capture cut it in
        // half must not let this area count as read, otherwise the next scroll
        // would skip it and that bubble would never be translated at all.
        const clipped = [];
        for (const region of response.payload?.incompleteRegions || []) {
          const rect = this.documentRectFromImage(region, response.payload, frame);
          if (rect) clipped.push({ ...rect, edges: region.edges });
        }

        const merged = this.store.merge(candidates, this.capturedDocumentRect(response.payload, frame));
        this.documentAnchorsStale = false;
        // Only page the server actually read, and read whole, may be claimed.
        const readBand = trimBandForClipped(plan.band, clipped);
        this.coverage = readBand ? addCoverage(this.coverage, readBand) : this.coverage;
        this.coverageContext = contextKey;
        this.consecutiveBandScans = plan.mode === "band" ? this.consecutiveBandScans + 1 : 0;
        const visible = this.syncOverlays();
        this.scheduleRegionMemorySave();
        void this.appendTranslationLog(cacheScope, candidates, scanSettings);

        const clientTimings = {
          totalMs: performance.now() - startedAt,
          roundTripMs: respondedAt - requestedAt
        };
        const actualProvider = response.payload?.translationProvider;
        const fallbackReason = response.payload?.providerFallbackReason;
        const usesPaidApi = response.payload?.usesPaidApi === true;
        const timingDetail = processingTimingDetail(response.payload?.processingTimings, clientTimings);
        // A billed engine is always named on screen. The reader must never have
        // to open the popup to find out that this screen cost money.
        const providerText = actualProvider
          ? ` · ${translationProviderLabel(actualProvider)}${usesPaidApi ? " · 유료 사용 중" : fallbackReason ? " 대체 사용" : ""}`
          : "";
        const scanScope = plan.mode === "band" ? "새로 나온 부분만 읽음" : "화면 전체를 읽음";
        const timings = response.payload?.processingTimings;
        const skipped = [
          clipped.length ? `잘린 말풍선 ${clipped.length}개는 다음에` : "",
          timings?.bubblelessRegionCount ? `말풍선 밖 글자 ${timings.bubblelessRegionCount}개 제외` : "",
          merged.dropped ? `사라진 번역 ${merged.dropped}개 정리` : ""
        ].filter(Boolean).join(" · ");
        this.overlay.setStatus(
          "ready",
          visible ? `${visible}개 말풍선 번역 표시 중${providerText}` : "읽을 글자를 찾지 못했어요",
          [fallbackReason, `${scanScope} · 새 말풍선 ${merged.added}개`, skipped, timingDetail].filter(Boolean).join("\n")
        );
      } catch (error) {
        if (this.handleExtensionContextInvalidated(error)) return;
        const message = error?.message || "번역 중 오류가 발생했습니다.";
        this.overlay.setStatus("error", message);
      } finally {
        if (this.activeCaptureToken === captureToken) this.activeCaptureToken = null;
        this.overlay.setCapturing(false);
        this.inFlight = false;
        if (this.pendingScan && this.enabled) {
          const next = this.pendingScan;
          this.pendingScan = null;
          this.scheduleScan(next.reason, next.force);
        }
      }
    }
  }

  const controller = new TranslatorController();
  controller.initialize().catch((error) => console.warn("말풍선 번역 레이어 초기화 실패", error));
})();
