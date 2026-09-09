const ACTIVE_TABS_KEY = "activeTranslationTabs";
const TRANSLATION_LOGS_KEY = "translationLogs";
const REGION_MEMORY_KEY = "regionMemory";
// Incremental strips, JPEG captures, and the unchanged-frame skip each made a
// scan much cheaper, so the reader no longer has to wait out the old interval
// before a freshly scrolled screen can be read.
const MIN_CAPTURE_INTERVAL_MS = 260;
const MIN_CAPTURE_REGION_SIDE_PX = 8;
// A screenshot only has to be good enough for OCR. JPEG removes the PNG encode
// from every capture and shrinks the payload roughly ten-fold, which shortens
// base64 building, message passing, the localhost POST, and the server decode
// all at once. Lower this if comic lettering ever starts to be misread.
const CAPTURE_JPEG_QUALITY = 92;
// Grid used to decide "this screen is pixel-identical to the last one".  64x64
// is fine enough that a single new speech bubble always changes several cells,
// so the skip stays conservative.
const FRAME_SIGNATURE_SIZE = 64;
// Paddle's detector becomes materially slower on high-DPI screenshots, while
// ordinary 1x desktop captures are already the right resolution for small
// comic lettering.  Keep at least this many raster pixels for each CSS pixel
// in the selected viewport.  That means a 10 CSS-px caption still reaches the
// OCR at roughly 12-13 pixels high, instead of being blindly reduced to a
// fixed image size.
const MIN_OCR_RASTER_DENSITY = 1.25;
// Keep a little extra headroom above the hard floor whenever possible.  This
// is the normal target for a Retina crop: it removes pixels the OCR cannot
// use efficiently without turning fine comic captions into 1x raster text.
const PREFERRED_OCR_RASTER_DENSITY = 1.5;
// This is a soft budget: the density floor above always wins.  It primarily
// removes redundant 2x/3x Retina pixels from large captures; it never shrinks
// a normal 1x 1080p/1440p viewport merely to meet the budget.
const OCR_RASTER_PIXEL_BUDGET = 3_600_000;
const OCR_RASTER_LONG_EDGE_BUDGET = 2_560;
// Re-encoding a PNG for a one- or two-percent size reduction costs more than
// it saves.  Leave near-native captures untouched.
const DOWNSCALE_EPSILON = 0.95;
const MAX_TRANSLATION_LOG_ENTRIES = 400;
const MAX_TRANSLATION_LOG_TEXT_LENGTH = 2_000;
const MAX_REGION_MEMORY_ENTRIES = 600;
const lastCaptureAt = new Map();
// Per tab: the last capture that was actually sent for OCR, so an identical
// screen can skip the whole round trip.
const lastFrameByTab = new Map();

const FULL_CAPTURE_REGION = Object.freeze({ x: 0, y: 0, width: 1, height: 1 });

function episodeScopeFromUrl(value) {
  try {
    const url = new URL(String(value));
    // Hashes are normally just the reading position inside the same chapter.
    return `${url.origin}${url.pathname}${url.search}`;
  } catch {
    return null;
  }
}

function cleanLogText(value) {
  return String(value ?? "")
    .replace(/\u0000/g, "")
    .trim()
    .slice(0, MAX_TRANSLATION_LOG_TEXT_LENGTH);
}

function normalizedLogText(value) {
  return cleanLogText(value).normalize("NFKC").replace(/\s+/g, " ");
}

function cleanLogCode(value) {
  return cleanLogText(value).toLowerCase().slice(0, 32);
}

function translationLogDedupeKey(entry) {
  return [
    normalizedLogText(entry.original),
    normalizedLogText(entry.translation),
    cleanLogCode(entry.sourceLanguage),
    cleanLogCode(entry.targetLanguage),
    cleanLogCode(entry.provider)
  ].join("\u001f");
}

// Session storage has no read-modify-write primitive, so each record type gets
// its own queue. Independent keys therefore never contend with one another.
function createSessionWriteQueue() {
  let tail = Promise.resolve();
  return (operation) => {
    const result = tail.then(operation, operation);
    // A failed storage operation must not block the following update.
    tail = result.catch(() => {});
    return result;
  };
}

const serializeTranslationLogOperation = createSessionWriteQueue();
const serializeRegionMemoryOperation = createSessionWriteQueue();

async function getTranslationLogs() {
  const stored = await chrome.storage.session.get(TRANSLATION_LOGS_KEY);
  return stored[TRANSLATION_LOGS_KEY] || {};
}

async function saveTranslationLogs(logs) {
  await chrome.storage.session.set({ [TRANSLATION_LOGS_KEY]: logs });
}

async function clearTranslationLog(tabId, scope = null) {
  return serializeTranslationLogOperation(async () => {
    const logs = await getTranslationLogs();
    const key = String(tabId);
    const current = logs[key];
    if (!current || (scope && current.scope !== scope)) return false;
    delete logs[key];
    await saveTranslationLogs(logs);
    return true;
  });
}

async function getTranslationLog(tabId, scope) {
  return serializeTranslationLogOperation(async () => {
    const logs = await getTranslationLogs();
    const key = String(tabId);
    const current = logs[key];
    if (!current || current.scope === scope) {
      return Array.isArray(current?.entries) ? current.entries : [];
    }

    // A same-tab SPA navigation can change its chapter URL without closing the
    // tab. Never let a previous chapter's dialogue leak into the new log view.
    delete logs[key];
    await saveTranslationLogs(logs);
    return [];
  });
}

async function appendTranslationLog(tabId, scope, message) {
  return serializeTranslationLogOperation(async () => {
    const logs = await getTranslationLogs();
    const key = String(tabId);
    const current = logs[key];
    const record = current?.scope === scope && Array.isArray(current.entries)
      ? { scope, entries: current.entries }
      : { scope, entries: [] };
    const seen = new Set(record.entries.map(translationLogDedupeKey));
    const sourceLanguage = cleanLogCode(message.sourceLanguage);
    const targetLanguage = cleanLogCode(message.targetLanguage);
    const provider = cleanLogCode(message.provider);
    const timestamp = Date.now();
    let appended = 0;

    for (const candidate of Array.isArray(message.entries) ? message.entries : []) {
      const original = cleanLogText(candidate?.original);
      const translation = cleanLogText(candidate?.translation);
      if (!original || !translation) continue;

      const entry = { original, translation, sourceLanguage, targetLanguage, provider, timestamp };
      const dedupeKey = translationLogDedupeKey(entry);
      if (seen.has(dedupeKey)) continue;
      seen.add(dedupeKey);
      record.entries.push(entry);
      appended += 1;
    }

    if (record.entries.length > MAX_TRANSLATION_LOG_ENTRIES) {
      record.entries.splice(0, record.entries.length - MAX_TRANSLATION_LOG_ENTRIES);
    }
    logs[key] = record;
    await saveTranslationLogs(logs);
    return { appended, count: record.entries.length };
  });
}

// ---------------------------------------------------------------------------
// Region memory: the translations already recognised for the episode being
// read. It exists so scrolling back re-shows a bubble instantly instead of
// paying for another OCR round trip, and so a reload inside the same tab
// session does not start from an empty screen.  It is deliberately session
// storage: nothing about a comic the user read is written to disk.
// ---------------------------------------------------------------------------

async function getRegionMemories() {
  const stored = await chrome.storage.session.get(REGION_MEMORY_KEY);
  return stored[REGION_MEMORY_KEY] || {};
}

function sanitizeMemoryEntry(candidate) {
  const rect = candidate?.documentRect;
  const numbers = [rect?.left, rect?.top, rect?.width, rect?.height].map(Number);
  if (!candidate?.id || !numbers.every(Number.isFinite) || numbers[2] <= 0 || numbers[3] <= 0) return null;

  return {
    id: String(candidate.id).slice(0, 120),
    anchor: candidate.anchor && typeof candidate.anchor === "object" ? candidate.anchor : null,
    documentRect: { left: numbers[0], top: numbers[1], width: numbers[2], height: numbers[3] },
    text: cleanLogText(candidate.text),
    normalizedText: cleanLogText(candidate.normalizedText),
    translation: cleanLogText(candidate.translation),
    confidence: Number(candidate.confidence) || 0,
    sourceLanguage: cleanLogCode(candidate.sourceLanguage),
    targetLanguage: cleanLogCode(candidate.targetLanguage),
    provider: cleanLogCode(candidate.provider),
    createdAt: Number(candidate.createdAt) || 0,
    lastSeenAt: Number(candidate.lastSeenAt) || 0
  };
}

async function getRegionMemory(tabId, scope) {
  return serializeRegionMemoryOperation(async () => {
    const memories = await getRegionMemories();
    const key = String(tabId);
    const current = memories[key];
    if (!current || current.scope === scope) {
      return Array.isArray(current?.entries) ? current.entries : [];
    }

    // A same-tab SPA navigation can change chapter without reloading. The
    // previous chapter's bubbles must never be drawn over the new one.
    delete memories[key];
    await chrome.storage.session.set({ [REGION_MEMORY_KEY]: memories });
    return [];
  });
}

async function saveRegionMemory(tabId, scope, entries) {
  return serializeRegionMemoryOperation(async () => {
    const memories = await getRegionMemories();
    const key = String(tabId);
    const sanitized = (Array.isArray(entries) ? entries : [])
      .map(sanitizeMemoryEntry)
      .filter(Boolean)
      .slice(-MAX_REGION_MEMORY_ENTRIES);

    if (!sanitized.length) {
      if (!memories[key]) return { count: 0 };
      delete memories[key];
    } else {
      memories[key] = { scope, entries: sanitized };
    }
    await chrome.storage.session.set({ [REGION_MEMORY_KEY]: memories });
    return { count: sanitized.length };
  });
}

async function clearRegionMemory(tabId) {
  return serializeRegionMemoryOperation(async () => {
    const memories = await getRegionMemories();
    const key = String(tabId);
    if (!memories[key]) return false;
    delete memories[key];
    await chrome.storage.session.set({ [REGION_MEMORY_KEY]: memories });
    return true;
  });
}

function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

// The content script sends a CSS-viewport rectangle normalized to 0..1.  Keeping
// this coordinate system at the boundary makes it independent of devicePixelRatio;
// captureVisibleTab's bitmap is converted to pixels only here in the service worker.
function normalizeCaptureRegion(value) {
  if (value == null) return null;
  if (typeof value !== "object") {
    throw new Error("선택 영역 형식이 올바르지 않습니다.");
  }

  const x = Number(value.x);
  const y = Number(value.y);
  const width = Number(value.width);
  const height = Number(value.height);
  if (![x, y, width, height].every(Number.isFinite) || width <= 0 || height <= 0) {
    throw new Error("선택 영역의 크기가 올바르지 않습니다.");
  }

  const left = clamp(x, 0, 1);
  const top = clamp(y, 0, 1);
  const right = clamp(x + width, 0, 1);
  const bottom = clamp(y + height, 0, 1);
  if (right <= left || bottom <= top) {
    throw new Error("선택 영역이 화면 밖에 있습니다.");
  }
  return { x: left, y: top, width: right - left, height: bottom - top };
}

function captureRegionMetadata(sourceWidth, sourceHeight, cropX, cropY, cropWidth, cropHeight) {
  return {
    captureRegion: {
      x: cropX / sourceWidth,
      y: cropY / sourceHeight,
      width: cropWidth / sourceWidth,
      height: cropHeight / sourceHeight
    },
    captureImage: {
      sourceWidth,
      sourceHeight,
      cropX,
      cropY,
      cropWidth,
      cropHeight
    }
  };
}

function normalizeCaptureViewport(value) {
  const width = Number(value?.width);
  const height = Number(value?.height);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return null;
  }
  return { width, height };
}

function optimizedRasterSize(sourceWidth, sourceHeight, cssWidth, cssHeight) {
  if (!sourceWidth || !sourceHeight || !cssWidth || !cssHeight) {
    return { width: sourceWidth, height: sourceHeight, scale: 1 };
  }

  // A uniform scale preserves the screenshot's geometry, so the server's OCR
  // coordinates still map back through imageWidth/imageHeight without any
  // special overlay transform.  If either axis is already below the density
  // floor, do not downscale at all.
  const densityFloorScale = Math.min(
    1,
    Math.max(
      (cssWidth * MIN_OCR_RASTER_DENSITY) / sourceWidth,
      (cssHeight * MIN_OCR_RASTER_DENSITY) / sourceHeight
    )
  );
  const preferredDensityScale = Math.min(
    1,
    Math.max(
      (cssWidth * PREFERRED_OCR_RASTER_DENSITY) / sourceWidth,
      (cssHeight * PREFERRED_OCR_RASTER_DENSITY) / sourceHeight
    )
  );
  const pixelBudgetScale = Math.min(1, Math.sqrt(OCR_RASTER_PIXEL_BUDGET / (sourceWidth * sourceHeight)));
  const edgeBudgetScale = Math.min(1, OCR_RASTER_LONG_EDGE_BUDGET / Math.max(sourceWidth, sourceHeight));
  // The preferred density reduces redundant 2x/3x pixels even for a small
  // crop.  Large captures are additionally constrained by the pixel/edge
  // budgets, unless doing so would cross the hard readability floor.
  const budgetScale = Math.min(pixelBudgetScale, edgeBudgetScale);
  const scale = Math.min(preferredDensityScale, Math.max(densityFloorScale, budgetScale));

  if (scale >= DOWNSCALE_EPSILON) {
    return { width: sourceWidth, height: sourceHeight, scale: 1 };
  }
  return {
    width: Math.max(1, Math.round(sourceWidth * scale)),
    height: Math.max(1, Math.round(sourceHeight * scale)),
    scale
  };
}

async function blobToDataUrl(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const chunkSize = 0x8000;
  let binary = "";
  for (let start = 0; start < bytes.length; start += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(start, start + chunkSize));
  }
  return `data:${blob.type || "image/png"};base64,${btoa(binary)}`;
}

// Reduce the area that will actually be sent for OCR to a small luminance grid.
// Two captures of the same still screen produce the same grid, which is all the
// caller needs to know that another OCR pass would return what it already has.
function frameSignatureFromSource(source, sx, sy, sw, sh) {
  const canvas = new OffscreenCanvas(FRAME_SIGNATURE_SIZE, FRAME_SIGNATURE_SIZE);
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return null;
  context.drawImage(source, sx, sy, sw, sh, 0, 0, FRAME_SIGNATURE_SIZE, FRAME_SIGNATURE_SIZE);
  const { data } = context.getImageData(0, 0, FRAME_SIGNATURE_SIZE, FRAME_SIGNATURE_SIZE);

  let signature = "";
  for (let index = 0; index < data.length; index += 4) {
    const luma = (data[index] * 299 + data[index + 1] * 587 + data[index + 2] * 114) / 1000;
    signature += ((luma | 0) >> 4).toString(16);
  }
  return signature;
}

async function frameSignatureFromBlob(blob) {
  // Decoding straight to the signature grid avoids materialising the full
  // screenshot when nothing else needs it.
  const thumbnail = await createImageBitmap(blob, {
    resizeWidth: FRAME_SIGNATURE_SIZE,
    resizeHeight: FRAME_SIGNATURE_SIZE,
    resizeQuality: "low"
  });
  try {
    return frameSignatureFromSource(thumbnail, 0, 0, thumbnail.width, thumbnail.height);
  } finally {
    thumbnail.close?.();
  }
}

// Crop, downscale, and fingerprint one screenshot.
//
// When the fingerprint matches `previousSignature` the screen has not changed
// since the last OCR, so this returns early: no crop, no re-encode, no request.
async function prepareCapture(image, requestedRegion, captureViewport, previousSignature) {
  // A 1x full-screen capture is already appropriate for OCR and can go straight
  // to localhost.  Crops always need a bitmap; high-DPI full captures are
  // inspected so their surplus pixels can be safely removed before the
  // expensive OCR step.
  const viewport = normalizeCaptureViewport(captureViewport);
  const shouldInspectFullCapture = Boolean(
    viewport
      && Number(captureViewport?.devicePixelRatio) > MIN_OCR_RASTER_DENSITY
  );

  const imageResponse = await fetch(image);
  if (!imageResponse.ok) {
    throw new Error("캡처 이미지를 선택 영역으로 준비하지 못했습니다.");
  }
  const blob = await imageResponse.blob();

  if (!requestedRegion && !shouldInspectFullCapture) {
    const signature = await frameSignatureFromBlob(blob);
    if (signature && signature === previousSignature) return { unchanged: true, signature };
    return { image, captureRegion: { ...FULL_CAPTURE_REGION }, captureImage: null, signature };
  }

  const bitmap = await createImageBitmap(blob);
  try {
    const sourceWidth = bitmap.width;
    const sourceHeight = bitmap.height;
    if (!sourceWidth || !sourceHeight) {
      throw new Error("캡처 이미지의 크기를 확인하지 못했습니다.");
    }

    const captureRegion = requestedRegion || FULL_CAPTURE_REGION;
    const cropX = clamp(Math.floor(captureRegion.x * sourceWidth), 0, sourceWidth - 1);
    const cropY = clamp(Math.floor(captureRegion.y * sourceHeight), 0, sourceHeight - 1);
    const cropRight = clamp(Math.ceil((captureRegion.x + captureRegion.width) * sourceWidth), cropX + 1, sourceWidth);
    const cropBottom = clamp(Math.ceil((captureRegion.y + captureRegion.height) * sourceHeight), cropY + 1, sourceHeight);
    const cropWidth = cropRight - cropX;
    const cropHeight = cropBottom - cropY;
    if (cropWidth < MIN_CAPTURE_REGION_SIDE_PX || cropHeight < MIN_CAPTURE_REGION_SIDE_PX) {
      throw new Error("선택 영역이 너무 작습니다. 글자가 보이도록 조금 더 넓게 지정해 주세요.");
    }

    // Fingerprint the cropped area only, and before any re-encoding, so an
    // unchanged screen costs neither the encode nor the request.
    const signature = frameSignatureFromSource(bitmap, cropX, cropY, cropWidth, cropHeight);
    if (signature && signature === previousSignature) return { unchanged: true, signature };

    const metadata = captureRegionMetadata(sourceWidth, sourceHeight, cropX, cropY, cropWidth, cropHeight);
    const cssCropWidth = viewport ? viewport.width * (cropWidth / sourceWidth) : 0;
    const cssCropHeight = viewport ? viewport.height * (cropHeight / sourceHeight) : 0;
    const output = optimizedRasterSize(cropWidth, cropHeight, cssCropWidth, cssCropHeight);
    if (
      cropX === 0
      && cropY === 0
      && cropWidth === sourceWidth
      && cropHeight === sourceHeight
      && output.scale === 1
    ) {
      return { image, ...metadata, signature };
    }

    const canvas = new OffscreenCanvas(output.width, output.height);
    const context = canvas.getContext("2d");
    if (!context) {
      throw new Error("선택 영역 캔버스를 만들지 못했습니다.");
    }
    // High-quality resampling only applies when the source has redundant
    // high-DPI pixels.  The original crop is retained at 1x, avoiding a
    // quality-changing re-encode for ordinary desktop screenshots.
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = "high";
    context.drawImage(bitmap, cropX, cropY, cropWidth, cropHeight, 0, 0, output.width, output.height);
    const croppedBlob = await canvas.convertToBlob({ type: "image/jpeg", quality: CAPTURE_JPEG_QUALITY / 100 });
    return { image: await blobToDataUrl(croppedBlob), ...metadata, signature };
  } finally {
    bitmap.close?.();
  }
}

async function getActiveTabs() {
  const stored = await chrome.storage.session.get(ACTIVE_TABS_KEY);
  return stored[ACTIVE_TABS_KEY] || {};
}

async function isTabEnabled(tabId) {
  const tabs = await getActiveTabs();
  return Boolean(tabs[String(tabId)]);
}

async function setTabEnabled(tabId, enabled) {
  const tabs = await getActiveTabs();
  if (enabled) {
    tabs[String(tabId)] = true;
  } else {
    delete tabs[String(tabId)];
  }
  await chrome.storage.session.set({ [ACTIVE_TABS_KEY]: tabs });
}

async function sendToTab(tabId, message) {
  try {
    await chrome.tabs.sendMessage(tabId, message);
    return true;
  } catch {
    return false;
  }
}

async function injectContentScripts(tabId) {
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["shared/defaults.js", "shared/glossary.js", "shared/regions.js", "content.js"]
  });
}

function localServerUrl(value) {
  let url;
  try {
    url = new URL(String(value));
  } catch {
    throw new Error("로컬 서버 주소 형식이 올바르지 않습니다.");
  }
  if (url.protocol !== "http:") {
    throw new Error("로컬 서버 주소는 http://127.0.0.1 또는 http://localhost로 시작해야 합니다.");
  }
  if (!new Set(["127.0.0.1", "localhost"]).has(url.hostname) || url.username || url.password) {
    throw new Error("개인정보 보호를 위해 로컬 서버(127.0.0.1 또는 localhost)만 사용할 수 있습니다.");
  }
  if (url.pathname !== "/" || url.search || url.hash) {
    throw new Error("로컬 서버 주소에는 포트까지만 입력해 주세요. 예: http://127.0.0.1:8765");
  }
  return url.origin;
}

async function requestJson(url, options, timeoutMs = 35000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.detail || data.message || `로컬 서버 오류 (${response.status})`);
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("로컬 OCR/번역 서버의 응답 시간이 초과되었습니다.");
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function stableHash(value) {
  const text = JSON.stringify(value ?? null);
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(36);
}

// Identical pixels only mean "nothing to do" while the translation settings,
// glossary, episode, and crop are also unchanged.
function captureContextKey(message) {
  return [
    message.cacheScope,
    message.settings?.sourceLanguage,
    message.settings?.targetLanguage,
    message.settings?.translationProvider,
    message.settings?.allowPaidFallback === true,
    message.settings?.minConfidence,
    message.settings?.requireCompleteBubble !== false,
    stableHash(message.captureRegion),
    stableHash(message.glossary)
  ].join("|");
}

async function translateVisibleFrame(message, sender) {
  const tab = sender.tab;
  if (!tab?.id || !tab.windowId) {
    throw new Error("현재 탭 정보를 찾지 못했습니다.");
  }
  if (!(await isTabEnabled(tab.id))) {
    throw new Error("화면 번역이 꺼져 있습니다.");
  }

  const [activeBeforeCapture] = await chrome.tabs.query({ active: true, windowId: tab.windowId });
  if (activeBeforeCapture?.id !== tab.id) {
    throw new Error("탭이 전환되어 현재 화면 번역을 취소했습니다.");
  }

  const now = Date.now();
  const elapsed = now - (lastCaptureAt.get(tab.id) || 0);
  if (elapsed < MIN_CAPTURE_INTERVAL_MS) {
    return { throttled: true, retryAfter: MIN_CAPTURE_INTERVAL_MS - elapsed };
  }
  lastCaptureAt.set(tab.id, now);

  const serverUrl = localServerUrl(message.settings?.serverUrl || "http://127.0.0.1:8765");
  const image = await chrome.tabs.captureVisibleTab(tab.windowId, {
    format: "jpeg",
    quality: CAPTURE_JPEG_QUALITY
  });
  // The content layer hides its patches only for this screenshot. OCR and
  // translation can take much longer, so let it restore and track the prior
  // patches as soon as the captured pixels are safely in memory.
  if (Number.isInteger(message.captureToken)) {
    void sendToTab(tab.id, { type: "CAPTURE_COMPLETED", captureToken: message.captureToken });
  }
  const requestedCaptureRegion = normalizeCaptureRegion(message.captureRegion);
  // Anything that changes what a translation should say invalidates the
  // unchanged-frame shortcut even when the pixels are identical.
  const frameContext = captureContextKey(message);
  const previous = lastFrameByTab.get(tab.id);
  const previousSignature = !message.bypassFrameSkip && previous?.context === frameContext
    ? previous.signature
    : null;
  const captured = await prepareCapture(image, requestedCaptureRegion, message.captureViewport, previousSignature);
  const [activeAfterCapture] = await chrome.tabs.query({ active: true, windowId: tab.windowId });
  if (activeAfterCapture?.id !== tab.id) {
    throw new Error("탭이 전환되어 캡처 결과를 버렸습니다.");
  }
  if (captured.unchanged) {
    // The screen is pixel-identical to the last one that was read. The content
    // script already holds those translations, so nothing needs to happen.
    return { throttled: false, unchanged: true };
  }
  const payload = await requestJson(`${serverUrl}/v1/translate-image`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      image: captured.image,
      cacheScope: message.cacheScope,
      sourceLanguage: message.settings?.sourceLanguage,
      targetLanguage: message.settings?.targetLanguage,
      translationProvider: message.settings?.translationProvider,
      allowPaidFallback: message.settings?.allowPaidFallback === true,
      minConfidence: message.settings?.minConfidence,
      requireCompleteBubble: message.settings?.requireCompleteBubble !== false,
      glossary: Array.isArray(message.glossary) ? message.glossary : []
    })
  });

  // `regions` and `imageWidth`/`imageHeight` deliberately remain in the cropped
  // image's pixel space. Content can map them back to the full overlay with the
  // normalized captureRegion supplied here, without assuming a device pixel ratio.
  const captureImage = captured.captureImage || {
    sourceWidth: Number(payload.imageWidth) || 0,
    sourceHeight: Number(payload.imageHeight) || 0,
    cropX: 0,
    cropY: 0,
    cropWidth: Number(payload.imageWidth) || 0,
    cropHeight: Number(payload.imageHeight) || 0
  };
  // Only remember a frame the server actually read. A failed request must not
  // make the next attempt believe this screen is already translated.
  if (captured.signature) {
    lastFrameByTab.set(tab.id, { context: frameContext, signature: captured.signature });
  }
  return {
    throttled: false,
    payload: {
      ...payload,
      captureRegion: captured.captureRegion,
      captureImage
    }
  };
}

async function healthCheck(serverUrl) {
  const url = localServerUrl(serverUrl || "http://127.0.0.1:8765");
  return requestJson(`${url}/health`, { method: "GET" }, 5000);
}

async function runtimeStatus(serverUrl) {
  const extensionVersion = chrome.runtime.getManifest().version;
  try {
    const health = await healthCheck(serverUrl);
    const serverVersion = typeof health?.version === "string" ? health.version : null;
    return {
      extensionVersion,
      serverVersion,
      compatible: Boolean(serverVersion && serverVersion === extensionVersion),
      health
    };
  } catch (error) {
    return {
      extensionVersion,
      serverVersion: null,
      compatible: false,
      error: error?.message || String(error)
    };
  }
}

async function paidUsage(serverUrl) {
  const url = localServerUrl(serverUrl || "http://127.0.0.1:8765");
  return requestJson(`${url}/v1/paid-usage`, { method: "GET" }, 5000);
}

async function warmupOcr(settings) {
  const serverUrl = localServerUrl(settings?.serverUrl || "http://127.0.0.1:8765");
  return requestJson(
    `${serverUrl}/v1/warmup`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        sourceLanguage: settings?.sourceLanguage,
        targetLanguage: settings?.targetLanguage,
        translationProvider: settings?.translationProvider,
        allowPaidFallback: settings?.allowPaidFallback === true
      })
    },
    180000
  );
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    switch (message.type) {
      case "GET_TAB_STATE": {
        const tabId = sender.tab?.id || message.tabId;
        sendResponse({ ok: Boolean(tabId), enabled: tabId ? await isTabEnabled(tabId) : false });
        return;
      }

      case "SET_TAB_ENABLED": {
        const tabId = Number.isInteger(message.tabId) ? message.tabId : sender.tab?.id;
        if (!Number.isInteger(tabId)) throw new Error("대상 탭을 찾지 못했습니다.");
        await setTabEnabled(tabId, Boolean(message.enabled));
        const delivered = await sendToTab(tabId, { type: "TRANSLATOR_STATE", enabled: Boolean(message.enabled) });
        if (message.enabled && !delivered) {
          try {
            await injectContentScripts(tabId);
          } catch {
            await setTabEnabled(tabId, false);
            throw new Error("이 페이지에는 번역 레이어를 삽입할 수 없습니다. 일반 웹페이지에서 다시 시도하세요.");
          }
        }
        sendResponse({ ok: true, enabled: Boolean(message.enabled) });
        return;
      }

      case "REQUEST_RESCAN": {
        const tabId = message.tabId || sender.tab?.id;
        if (!Number.isInteger(tabId)) throw new Error("대상 탭을 찾지 못했습니다.");
        await sendToTab(tabId, { type: "REQUEST_RESCAN", reason: message.reason || "manual" });
        sendResponse({ ok: true });
        return;
      }

      case "START_REGION_SELECTION": {
        const tabId = Number.isInteger(message.tabId) ? message.tabId : sender.tab?.id;
        if (!Number.isInteger(tabId)) throw new Error("대상 탭을 찾지 못했습니다.");
        const delivered = await sendToTab(tabId, { type: "START_REGION_SELECTION" });
        if (!delivered) {
          throw new Error("번역 화면에 연결하지 못했습니다. 웹툰 탭을 새로고침한 뒤 다시 시도해 주세요.");
        }
        sendResponse({ ok: true });
        return;
      }

      case "CLEAR_CAPTURE_REGION": {
        const tabId = Number.isInteger(message.tabId) ? message.tabId : sender.tab?.id;
        if (!Number.isInteger(tabId)) throw new Error("대상 탭을 찾지 못했습니다.");
        const delivered = await sendToTab(tabId, { type: "CLEAR_CAPTURE_REGION" });
        if (!delivered) {
          throw new Error("번역 화면에 연결하지 못했습니다. 웹툰 탭을 새로고침한 뒤 다시 시도해 주세요.");
        }
        sendResponse({ ok: true });
        return;
      }

      case "GET_ORIGINAL_TEXT": {
        const tabId = message.tabId || sender.tab?.id;
        if (!Number.isInteger(tabId)) throw new Error("탭을 찾지 못했습니다.");
        try {
          const original = await chrome.tabs.sendMessage(tabId, { type: "GET_ORIGINAL_TEXT" });
          sendResponse(original || { ok: false, error: "원문을 가져오지 못했습니다." });
        } catch {
          sendResponse({ ok: false, error: "복사할 OCR 원문이 없습니다. 먼저 화면 번역을 실행해 주세요." });
        }
        return;
      }

      case "GET_TRANSLATION_LOG": {
        const tabId = message.tabId || sender.tab?.id;
        const scope = episodeScopeFromUrl(message.cacheScope);
        if (!Number.isInteger(tabId) || !scope) throw new Error("현재 회차 정보를 확인하지 못했습니다.");
        const entries = await getTranslationLog(tabId, scope);
        sendResponse({ ok: true, entries, count: entries.length });
        return;
      }

      case "CLEAR_TRANSLATION_LOG": {
        const tabId = message.tabId || sender.tab?.id;
        const scope = episodeScopeFromUrl(message.cacheScope);
        if (!Number.isInteger(tabId) || !scope) throw new Error("현재 회차 정보를 확인하지 못했습니다.");
        await clearTranslationLog(tabId, scope);
        sendResponse({ ok: true });
        return;
      }

      case "APPEND_TRANSLATION_LOG": {
        const tabId = sender.tab?.id;
        const senderScope = episodeScopeFromUrl(sender.tab?.url);
        const messageScope = episodeScopeFromUrl(message.cacheScope);
        if (!Number.isInteger(tabId) || !senderScope || !messageScope) {
          throw new Error("번역 로그의 회차 정보를 확인하지 못했습니다.");
        }
        // Ignore a late message from a content script that belonged to the
        // previous chapter; it must not erase or contaminate the new chapter log.
        if (senderScope !== messageScope) {
          sendResponse({ ok: true, appended: 0, ignored: true });
          return;
        }
        const result = await appendTranslationLog(tabId, senderScope, message);
        sendResponse({ ok: true, ...result });
        return;
      }

      case "GET_REGION_MEMORY": {
        const tabId = sender.tab?.id;
        const scope = episodeScopeFromUrl(message.cacheScope);
        if (!Number.isInteger(tabId) || !scope) {
          sendResponse({ ok: true, entries: [] });
          return;
        }
        const entries = await getRegionMemory(tabId, scope);
        sendResponse({ ok: true, entries });
        return;
      }

      case "SAVE_REGION_MEMORY": {
        const tabId = sender.tab?.id;
        const senderScope = episodeScopeFromUrl(sender.tab?.url);
        const messageScope = episodeScopeFromUrl(message.cacheScope);
        if (!Number.isInteger(tabId) || !senderScope || !messageScope) {
          throw new Error("번역 기억의 회차 정보를 확인하지 못했습니다.");
        }
        // Ignore a late save from a content script that belonged to the
        // previous chapter, exactly as the translation log does.
        if (senderScope !== messageScope) {
          sendResponse({ ok: true, ignored: true });
          return;
        }
        const result = await saveRegionMemory(tabId, senderScope, message.entries);
        sendResponse({ ok: true, ...result });
        return;
      }

      case "SETTINGS_CHANGED": {
        if (Number.isInteger(message.tabId)) {
          await sendToTab(message.tabId, { type: "SETTINGS_CHANGED" });
        }
        sendResponse({ ok: true });
        return;
      }

      case "TRANSLATE_VISIBLE_FRAME": {
        const result = await translateVisibleFrame(message, sender);
        sendResponse({ ok: true, ...result });
        return;
      }

      case "HEALTH_CHECK": {
        const health = await healthCheck(message.serverUrl);
        sendResponse({ ok: true, health });
        return;
      }

      case "GET_RUNTIME_STATUS": {
        const runtime = await runtimeStatus(message.serverUrl);
        if (runtime.error) throw new Error(runtime.error);
        sendResponse({ ok: true, runtime });
        return;
      }

      case "GET_PAID_USAGE": {
        const usage = await paidUsage(message.serverUrl);
        sendResponse({ ok: true, usage });
        return;
      }

      case "WARMUP_OCR": {
        const warmup = await warmupOcr(message.settings);
        sendResponse({ ok: true, warmup });
        return;
      }

      default:
        sendResponse({ ok: false, error: "알 수 없는 요청입니다." });
    }
  })().catch((error) => sendResponse({ ok: false, error: error.message || String(error) }));
  return true;
});

chrome.commands.onCommand.addListener(async (command) => {
  if (command !== "toggle-translation") return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !/^https?:/.test(tab.url || "")) return;
  const enabled = !(await isTabEnabled(tab.id));
  await setTabEnabled(tab.id, enabled);
  const delivered = await sendToTab(tab.id, { type: "TRANSLATOR_STATE", enabled });
  if (enabled && !delivered) {
    try {
      await injectContentScripts(tab.id);
    } catch {
      // Keyboard shortcuts should fail silently on protected browser pages.
      await setTabEnabled(tab.id, false);
    }
  }
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const tabs = await getActiveTabs();
  if (tabs[String(tabId)]) {
    delete tabs[String(tabId)];
    await chrome.storage.session.set({ [ACTIVE_TABS_KEY]: tabs });
  }
  lastCaptureAt.delete(tabId);
  lastFrameByTab.delete(tabId);
  await clearTranslationLog(tabId);
  await clearRegionMemory(tabId);
});

// activeTab grants access only for the current document. A page navigation therefore
// deliberately turns the layer off instead of retaining access to future pages.
chrome.tabs.onUpdated.addListener(async (tabId, changeInfo) => {
  if (changeInfo.status === "loading") {
    if (await isTabEnabled(tabId)) {
      await setTabEnabled(tabId, false);
    }
    lastCaptureAt.delete(tabId);
    lastFrameByTab.delete(tabId);
  }

  if (changeInfo.url) {
    const scope = episodeScopeFromUrl(changeInfo.url);
    if (scope) {
      // Reading these with the new scope is what drops a previous chapter's
      // records; both stores share that guard.
      await getTranslationLog(tabId, scope);
      await getRegionMemory(tabId, scope);
      lastFrameByTab.delete(tabId);
    }
  }
});
