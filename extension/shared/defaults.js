(() => {
  const LANGUAGE_OPTIONS = [
    { code: "en", label: "영어" },
    { code: "ko", label: "한국어" },
    { code: "ja", label: "일본어" },
    { code: "zh", label: "중국어(간체)" },
    { code: "es", label: "스페인어" },
    { code: "fr", label: "프랑스어" },
    { code: "de", label: "독일어" },
    { code: "pt", label: "포르투갈어" },
    { code: "ru", label: "러시아어" },
    { code: "vi", label: "베트남어" }
  ];

  // API keys never live in the extension. This value only tells the local
  // server which translator it should use for the OCR text it receives.
  const TRANSLATION_PROVIDER_OPTIONS = [
    { code: "auto", label: "자동 (DeepL → Azure)" },
    { code: "azure", label: "Azure Translator (무료 F0)" },
    { code: "deepl", label: "DeepL API Free" }
  ];

  // The paid plans are never selectable: they are what the server falls back to
  // once every free engine is exhausted and the reader has allowed it. They
  // still need labels, because a log entry or an overlay badge has to be able
  // to name the engine that actually answered.
  const PAID_TRANSLATION_PROVIDER_LABELS = Object.freeze({
    deepl_pro: "DeepL API Pro (유료)",
    azure_paid: "Azure Translator (유료)"
  });

  const DEFAULT_SETTINGS = Object.freeze({
    sourceLanguage: "en",
    targetLanguage: "ko",
    serverUrl: "http://127.0.0.1:8765",
    autoScan: true,
    // When a reader has not explicitly drawn a crop, prefer the large comic
    // image/canvas in the viewport over browser chrome, chapter headers, and
    // site navigation around it.
    autoDetectComicRegion: true,
    // Time the page must sit still before a scan starts. Long enough that
    // scrolling does not fire off captures of page the reader is speeding past.
    scanDelay: 500,
    // Webtoon lettering often has outlines, gradients, or inverse colours.
    // The server's bubble-aware filtering removes obvious artwork text, so a
    // slightly lower threshold is safer than dropping valid dialogue first.
    minConfidence: 0.45,
    // Only translate text inside a speech bubble that is fully on screen. Half a
    // bubble reads as half a sentence, and lettering on the artwork produces a
    // patch where there is visibly no bubble.
    requireCompleteBubble: true,
    overlayOpacity: 0.93,
    fontScale: 1,
    showOriginal: false,
    // Point at a bubble whose translation is currently hidden (원문 보기) and it
    // reappears for as long as the pointer stays on it.
    hoverReveal: true,
    translationProvider: "auto",
    // Money is never spent without the reader saying so first. While this is
    // off, an exhausted free quota simply stops translating.
    allowPaidFallback: false
  });

  const SETTINGS_KEY = "settings";
  const GLOSSARY_KEY = "glossary";
  const LOCAL_SERVER_HOSTS = new Set(["127.0.0.1", "localhost"]);

  // A stored value that is exactly one of the previous defaults means the reader
  // never touched this setting, so it follows the current default instead of
  // being frozen at an old one. A value they actually chose is left alone.
  const LEGACY_DEFAULT_SCAN_DELAYS = new Set([750, 300]);
  const LEGACY_DEFAULT_MIN_CONFIDENCES = new Set([0.55]);

  function normalizeScanDelay(value) {
    const delay = Number(value);
    if (!Number.isFinite(delay) || LEGACY_DEFAULT_SCAN_DELAYS.has(delay)) return DEFAULT_SETTINGS.scanDelay;
    return Math.min(2_500, Math.max(200, delay));
  }

  function normalizeMinConfidence(value) {
    const confidence = Number(value);
    if (!Number.isFinite(confidence) || LEGACY_DEFAULT_MIN_CONFIDENCES.has(confidence)) {
      return DEFAULT_SETTINGS.minConfidence;
    }
    return Math.min(0.95, Math.max(0.2, confidence));
  }

  // Keep the image/OCR privacy boundary intact even before the service worker
  // makes its own defensive check. Stored values can outlive extension updates,
  // so an old malformed setting falls back safely; a newly saved value receives
  // the more useful validation error from `validateServerUrl` below.
  function normalizeServerUrl(value) {
    try {
      return validateServerUrl(value);
    } catch {
      return DEFAULT_SETTINGS.serverUrl;
    }
  }

  function validateServerUrl(value) {
    const raw = String(value ?? "").trim();
    if (!raw) return DEFAULT_SETTINGS.serverUrl;

    let url;
    try {
      url = new URL(raw);
    } catch {
      throw new Error("로컬 서버 주소 형식이 올바르지 않습니다.");
    }
    if (url.protocol !== "http:") {
      throw new Error("로컬 서버 주소는 http://127.0.0.1 또는 http://localhost로 시작해야 합니다.");
    }
    if (!LOCAL_SERVER_HOSTS.has(url.hostname) || url.username || url.password) {
      throw new Error("개인정보 보호를 위해 로컬 서버(127.0.0.1 또는 localhost)만 사용할 수 있습니다.");
    }
    if (url.pathname !== "/" || url.search || url.hash) {
      throw new Error("로컬 서버 주소에는 포트까지만 입력해 주세요. 예: http://127.0.0.1:8765");
    }
    return url.origin;
  }

  function normalizeTranslationProvider(value) {
    const code = String(value || "").trim().toLowerCase();
    return TRANSLATION_PROVIDER_OPTIONS.some((provider) => provider.code === code)
      ? code
      : DEFAULT_SETTINGS.translationProvider;
  }

  async function getSettings() {
    const stored = await chrome.storage.sync.get(SETTINGS_KEY);
    return {
      ...DEFAULT_SETTINGS,
      ...(stored[SETTINGS_KEY] || {}),
      serverUrl: normalizeServerUrl(stored[SETTINGS_KEY]?.serverUrl),
      scanDelay: normalizeScanDelay(stored[SETTINGS_KEY]?.scanDelay),
      minConfidence: normalizeMinConfidence(stored[SETTINGS_KEY]?.minConfidence),
      translationProvider: normalizeTranslationProvider(stored[SETTINGS_KEY]?.translationProvider),
      autoDetectComicRegion: stored[SETTINGS_KEY]?.autoDetectComicRegion !== false,
      allowPaidFallback: stored[SETTINGS_KEY]?.allowPaidFallback === true
    };
  }

  async function saveSettings(patch) {
    const next = { ...(await getSettings()), ...patch };
    next.serverUrl = validateServerUrl(next.serverUrl);
    next.minConfidence = normalizeMinConfidence(next.minConfidence);
    next.translationProvider = normalizeTranslationProvider(next.translationProvider);
    // It is on by default, and only an explicit false opts out. This lets old
    // installations receive the safer reader-area behavior on upgrade.
    next.autoDetectComicRegion = next.autoDetectComicRegion !== false;
    // Anything other than an explicit true means the reader has not opted in.
    next.allowPaidFallback = next.allowPaidFallback === true;
    await chrome.storage.sync.set({ [SETTINGS_KEY]: next });
    return next;
  }

  async function getGlossary() {
    const stored = await chrome.storage.local.get(GLOSSARY_KEY);
    return Array.isArray(stored[GLOSSARY_KEY]) ? stored[GLOSSARY_KEY] : [];
  }

  async function saveGlossary(entries) {
    await chrome.storage.local.set({ [GLOSSARY_KEY]: entries });
  }

  function languageLabel(code) {
    return LANGUAGE_OPTIONS.find((language) => language.code === code)?.label || code;
  }

  function translationProviderLabel(code) {
    return TRANSLATION_PROVIDER_OPTIONS.find((provider) => provider.code === code)?.label
      || PAID_TRANSLATION_PROVIDER_LABELS[code]
      || code;
  }

  function isPaidTranslationProvider(code) {
    return Object.prototype.hasOwnProperty.call(PAID_TRANSLATION_PROVIDER_LABELS, String(code || ""));
  }

  function formatCharacterCount(value) {
    const count = Number(value);
    return Number.isFinite(count) ? `${Math.max(0, Math.round(count)).toLocaleString("ko-KR")}자` : "0자";
  }

  globalThis.WebtoonTranslator = {
    LANGUAGE_OPTIONS,
    TRANSLATION_PROVIDER_OPTIONS,
    PAID_TRANSLATION_PROVIDER_LABELS,
    DEFAULT_SETTINGS,
    SETTINGS_KEY,
    GLOSSARY_KEY,
    getSettings,
    saveSettings,
    getGlossary,
    saveGlossary,
    languageLabel,
    translationProviderLabel,
    isPaidTranslationProvider,
    formatCharacterCount,
    normalizeServerUrl,
    validateServerUrl,
    normalizeScanDelay,
    normalizeMinConfidence
  };
})();
