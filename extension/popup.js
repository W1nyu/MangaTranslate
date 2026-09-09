(() => {
  const {
    LANGUAGE_OPTIONS,
    TRANSLATION_PROVIDER_OPTIONS,
    getSettings,
    saveSettings,
    languageLabel,
    translationProviderLabel,
    formatCharacterCount
  } = globalThis.WebtoonTranslator;
  const elements = {
    source: document.querySelector("[data-source-language]"),
    target: document.querySelector("[data-target-language]"),
    provider: document.querySelector("[data-translation-provider]"),
    autoScan: document.querySelector("[data-auto-scan]"),
    toggle: document.querySelector("[data-toggle]"),
    rescan: document.querySelector("[data-rescan]"),
    selectRegion: document.querySelector("[data-select-region]"),
    copyOriginal: document.querySelector("[data-copy-original]"),
    translationLog: document.querySelector("[data-translation-log]"),
    logEmpty: document.querySelector("[data-log-empty]"),
    logCount: document.querySelector("[data-log-count]"),
    refreshLog: document.querySelector("[data-refresh-log]"),
    copyLog: document.querySelector("[data-copy-log]"),
    clearLog: document.querySelector("[data-clear-log]"),
    serverStatus: document.querySelector("[data-server-status]"),
    readiness: document.querySelector("[data-readiness]"),
    readinessText: document.querySelector("[data-readiness-text]"),
    options: document.querySelector("[data-options]")
  };

  let tab = null;
  let settings = null;
  let enabled = false;
  let busy = false;
  let translationLog = [];

  function getEpisodeScope() {
    try {
      const url = new URL(String(tab?.url || ""));
      return `${url.origin}${url.pathname}${url.search}`;
    } catch {
      return null;
    }
  }

  function formatLogTimestamp(value) {
    const timestamp = Number(value);
    if (!Number.isFinite(timestamp) || timestamp <= 0) return "기록 시각 없음";
    return new Intl.DateTimeFormat("ko-KR", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit"
    }).format(new Date(timestamp));
  }

  function logMetaText(entry) {
    const source = languageLabel(String(entry?.sourceLanguage || "")) || "원문 언어";
    const target = languageLabel(String(entry?.targetLanguage || "")) || "번역 언어";
    const provider = translationProviderLabel(String(entry?.provider || "")) || "번역 제공자";
    return `${formatLogTimestamp(entry?.timestamp)} · ${source} → ${target} · ${provider}`;
  }

  function renderTranslationLog() {
    const entries = Array.isArray(translationLog) ? translationLog : [];
    const hasEntries = entries.length > 0;
    elements.logCount.textContent = `${entries.length}개`;
    elements.logEmpty.hidden = hasEntries;
    elements.translationLog.replaceChildren();

    for (const entry of [...entries].reverse()) {
      const item = document.createElement("li");
      item.className = "translation-log-entry";

      const meta = document.createElement("span");
      meta.className = "translation-log-meta";
      meta.textContent = logMetaText(entry);

      const original = document.createElement("p");
      original.className = "translation-log-original";
      original.textContent = `원문: ${String(entry?.original || "")}`;

      const translation = document.createElement("p");
      translation.className = "translation-log-translation";
      translation.textContent = `번역: ${String(entry?.translation || "")}`;

      item.append(meta, original, translation);
      elements.translationLog.append(item);
    }

    const disabled = busy || !hasEntries;
    elements.copyLog.disabled = disabled;
    elements.clearLog.disabled = disabled;
    elements.refreshLog.disabled = busy || !tab?.id || !getEpisodeScope();
  }

  function setOptions(select) {
    select.replaceChildren(...LANGUAGE_OPTIONS.map((language) => {
      const option = document.createElement("option");
      option.value = language.code;
      option.textContent = language.label;
      return option;
    }));
  }

  function setProviderOptions(select) {
    select.replaceChildren(...TRANSLATION_PROVIDER_OPTIONS.map((provider) => {
      const option = document.createElement("option");
      option.value = provider.code;
      option.textContent = provider.label;
      return option;
    }));
  }

  function setReadiness(kind, message, chipText) {
    elements.readiness.className = `readiness${kind ? ` ${kind}` : ""}`;
    elements.readinessText.textContent = message;
    elements.serverStatus.className = `connection-chip${kind === "ready" ? " ready" : kind === "error" ? " error" : kind === "warn" ? " warn" : ""}`;
    elements.serverStatus.textContent = chipText || (kind === "ready" ? "로컬 연결됨" : kind === "error" ? "연결 안 됨" : kind === "warn" ? "확인 필요" : "확인 중");
  }

  function render() {
    elements.source.value = settings.sourceLanguage;
    elements.target.value = settings.targetLanguage;
    elements.provider.value = settings.translationProvider;
    elements.autoScan.checked = settings.autoScan;
    elements.toggle.textContent = enabled ? "화면 번역 끄기" : "화면 번역 시작";
    elements.toggle.classList.toggle("stop", enabled);
    elements.toggle.disabled = busy;
    elements.rescan.disabled = !enabled;
    elements.selectRegion.disabled = !enabled || busy;
    // Keep copying available after a completed scan, even if the overlay is off.
    elements.copyOriginal.disabled = busy;
    renderTranslationLog();
  }

  async function saveLanguageSettings() {
    settings = await saveSettings({
      sourceLanguage: elements.source.value,
      targetLanguage: elements.target.value,
      translationProvider: elements.provider.value,
      autoScan: elements.autoScan.checked
    });
    render();
    const ready = await refreshHealth();
    if (!tab?.id) return;
    if (enabled && !ready) {
      await chrome.runtime.sendMessage({ type: "SET_TAB_ENABLED", tabId: tab.id, enabled: false });
      enabled = false;
      render();
      return;
    }
    await chrome.runtime.sendMessage({ type: "SETTINGS_CHANGED", tabId: tab.id });
  }

  // One sentence appended to the readiness line so the reader always knows,
  // before starting, whether an exhausted free quota will stop translating or
  // quietly start costing money.
  function paidFallbackNote(health) {
    const usage = health?.paidUsage;
    if (!settings.allowPaidFallback) {
      return health?.paidFallbackAvailable
        ? " 무료 한도가 끝나면 번역이 멈춥니다. 설정에서 유료 API 전환을 켤 수 있습니다."
        : "";
    }
    if (!usage?.available) {
      return " 유료 전환은 켜져 있지만 local-server/.env에 유료 키가 없어 무료 한도가 끝나면 번역이 멈춥니다.";
    }
    if (usage.limited && Number(usage.remainingCharacters) <= 0) {
      return ` 유료 API 월 상한 ${formatCharacterCount(usage.monthlyLimit)}를 모두 사용해 이번 달에는 전환하지 않습니다.`;
    }
    return usage.limited
      ? ` 무료 한도가 끝나면 유료 API로 이어서 번역합니다 (이번 달 남은 한도 ${formatCharacterCount(usage.remainingCharacters)}).`
      : " 무료 한도가 끝나면 유료 API로 이어서 번역합니다 (월 상한 없음).";
  }

  function providerReadinessMessage(health) {
    const requested = settings.translationProvider;
    const providers = health?.translationProviders || {};
    const isReady = (name) => Boolean(providers[name]?.configured && providers[name]?.available);
    const fallback = requested === "deepl" ? "azure" : "deepl";

    // A reader who configured only paid keys and turned the switch on is
    // genuinely ready, even though no free engine can answer.
    const paidOnlyReady = settings.allowPaidFallback && Boolean(health?.paidUsage?.available);
    const noFreeKeyMessage = {
      kind: paidOnlyReady ? "ready" : "error",
      message: paidOnlyReady
        ? "무료 API 키가 없어 처음부터 유료 API로 번역합니다. 사용량이 바로 발생합니다."
        : "DeepL 또는 Azure Translator API 키를 local-server/.env에 설정해 주세요. 로컬 Argos 번역은 사용하지 않습니다.",
      chipText: paidOnlyReady ? "유료 API 사용" : "API 설정 필요",
      ready: paidOnlyReady
    };

    if (requested === "auto") {
      const firstReady = ["deepl", "azure"].find(isReady);
      if (!firstReady) return noFreeKeyMessage;
      return {
        kind: "ready",
        message: `자동 엔진은 DeepL → Azure 순으로 시도합니다. 현재 우선 사용할 API: ${translationProviderLabel(firstReady)}.`,
        ready: true
      };
    }

    if (isReady(requested)) {
      return {
        kind: "ready",
        message: `${translationProviderLabel(requested)}로 ${languageLabel(settings.sourceLanguage)} → ${languageLabel(settings.targetLanguage)} 번역합니다. 오류·한도 초과 시 ${translationProviderLabel(fallback)}만 대체 사용합니다.`,
        ready: true
      };
    }

    if (isReady(fallback)) {
      return {
        kind: "ready",
        message: `${translationProviderLabel(requested)}를 사용할 수 없어 ${translationProviderLabel(fallback)}로 번역합니다. 로컬 Argos로는 전환하지 않습니다.`,
        ready: true
      };
    }

    return noFreeKeyMessage;
  }

  async function refreshHealth() {
    try {
      const response = await chrome.runtime.sendMessage({ type: "GET_RUNTIME_STATUS", serverUrl: settings.serverUrl });
      if (!response?.ok) throw new Error(response?.error || "서버에 연결할 수 없습니다.");
      const runtime = response.runtime;
      const health = runtime?.health;
      const readiness = providerReadinessMessage(health);
      const versionNote = runtime?.compatible
        ? ""
        : ` 확장 ${runtime?.extensionVersion || "?"} · 서버 ${runtime?.serverVersion || "?"}입니다. 로컬 서버를 다시 시작해 버전을 맞춰 주세요.`;
      const kind = runtime?.compatible || !readiness.ready ? readiness.kind : "warn";
      const chipText = runtime?.compatible ? readiness.chipText : "버전 불일치";
      setReadiness(kind, `${readiness.message}${paidFallbackNote(health)}${versionNote}`, chipText);
      return readiness.ready;
    } catch (error) {
      setReadiness("error", "로컬 모델 서버가 꺼져 있어요. README의 1단계에서 서버를 실행한 뒤 다시 열어 주세요.");
      return false;
    }
  }

  async function warmupOcr() {
    setReadiness("", `${languageLabel(settings.sourceLanguage)} OCR 모델을 준비하고 있어요. 처음 한 번만 내려받을 수 있습니다.`, "OCR 준비 중");
    try {
      const response = await chrome.runtime.sendMessage({ type: "WARMUP_OCR", settings });
      if (!response?.ok) throw new Error(response?.error || "OCR 모델을 준비하지 못했습니다.");
      const actualProvider = response.warmup?.translationProvider;
      const fallbackReason = response.warmup?.providerFallbackReason;
      const usesPaidApi = response.warmup?.usesPaidApi === true;
      const providerNote = actualProvider
        ? `${translationProviderLabel(actualProvider)} 엔진으로 준비했습니다${usesPaidApi ? " (유료 API 사용 중)" : fallbackReason ? " (대체 엔진 사용)" : ""}.`
        : `${translationProviderLabel(settings.translationProvider)} 엔진으로 번역합니다.`;
      setReadiness("ready", `OCR 모델까지 준비됐어요. ${providerNote} 이제 ${languageLabel(settings.sourceLanguage)} → ${languageLabel(settings.targetLanguage)} 화면 번역을 시작합니다.`, usesPaidApi ? "유료 API 사용" : "OCR 준비됨");
      return true;
    } catch (error) {
      setReadiness("error", error.message || "OCR 모델을 준비하지 못했습니다.");
      return false;
    }
  }

  async function toggle() {
    if (busy) return;
    busy = true;
    render();
    try {
    if (!tab?.id || !/^https?:/.test(tab.url || "")) {
      setReadiness("error", "웹페이지(http/https)를 연 탭에서만 사용할 수 있어요.");
      return;
    }
    if (!enabled && !(await refreshHealth())) return;
    if (!enabled && !(await warmupOcr())) return;
    const wasEnabled = enabled;
    const response = await chrome.runtime.sendMessage({ type: "SET_TAB_ENABLED", tabId: tab.id, enabled: !enabled });
    if (!response?.ok) {
      setReadiness("error", response?.error || "화면 번역 상태를 바꾸지 못했습니다.");
      return;
    }
    enabled = response.enabled;
    render();
    // The page immediately enters capture-region selection when translation is
    // enabled. Close the popup so its first drag reaches that selection layer.
    if (!wasEnabled && enabled) window.close();
    } finally {
      busy = false;
      render();
    }
  }

  async function writeClipboardText(value) {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        return;
      } catch {
        // The fallback also supports browsers that reject Clipboard API access
        // while the extension popup is losing focus.
      }
    }
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "");
    textarea.style.cssText = "position:fixed;opacity:0;pointer-events:none;";
    document.body.append(textarea);
    textarea.select();
    const copied = document.execCommand("copy");
    textarea.remove();
    if (!copied) throw new Error("클립보드에 원문을 복사하지 못했습니다.");
  }

  async function copyOriginalText() {
    if (!tab?.id) throw new Error("현재 탭을 찾지 못했습니다.");
    const response = await chrome.runtime.sendMessage({ type: "GET_ORIGINAL_TEXT", tabId: tab.id });
    if (!response?.ok) throw new Error(response?.error || "원문을 가져오지 못했습니다.");
    const originalText = String(response.text || "").trim();
    if (!originalText) throw new Error("복사할 OCR 원문이 없습니다. 먼저 화면 번역을 실행해 주세요.");
    await writeClipboardText(originalText);
    const count = Number(response.count) || 0;
    setReadiness("ready", count ? `현재 화면의 OCR 원문 ${count}개를 클립보드에 복사했습니다.` : "현재 화면의 OCR 원문을 클립보드에 복사했습니다.", "원문 복사됨");
  }

  async function refreshTranslationLog() {
    const cacheScope = getEpisodeScope();
    if (!tab?.id || !cacheScope) {
      translationLog = [];
      renderTranslationLog();
      return;
    }
    const response = await chrome.runtime.sendMessage({
      type: "GET_TRANSLATION_LOG",
      tabId: tab.id,
      cacheScope
    });
    if (!response?.ok) throw new Error(response?.error || "번역 로그를 불러오지 못했습니다.");
    translationLog = Array.isArray(response.entries) ? response.entries : [];
    renderTranslationLog();
  }

  function translationLogClipboardText() {
    return translationLog.map((entry) => [
      `[${logMetaText(entry)}]`,
      `원문: ${String(entry?.original || "")}`,
      `번역: ${String(entry?.translation || "")}`
    ].join("\n")).join("\n\n");
  }

  async function copyTranslationLog() {
    const text = translationLogClipboardText();
    if (!text) throw new Error("복사할 번역 로그가 없습니다.");
    await writeClipboardText(text);
    setReadiness("ready", `이번 회차 번역 로그 ${translationLog.length}개를 클립보드에 복사했습니다.`, "로그 복사됨");
  }

  async function clearTranslationLog() {
    const cacheScope = getEpisodeScope();
    if (!tab?.id || !cacheScope) throw new Error("현재 회차 정보를 확인하지 못했습니다.");
    const response = await chrome.runtime.sendMessage({
      type: "CLEAR_TRANSLATION_LOG",
      tabId: tab.id,
      cacheScope
    });
    if (!response?.ok) throw new Error(response?.error || "번역 로그를 비우지 못했습니다.");
    translationLog = [];
    renderTranslationLog();
    setReadiness("ready", "이번 회차 번역 로그를 비웠습니다.", "로그 초기화됨");
  }

  async function runLogAction(action) {
    if (busy) return;
    busy = true;
    render();
    try {
      await action();
    } finally {
      busy = false;
      render();
    }
  }

  async function selectCaptureRegion() {
    if (!tab?.id || !enabled) {
      throw new Error("먼저 화면 번역을 시작해 주세요.");
    }
    const response = await chrome.runtime.sendMessage({ type: "START_REGION_SELECTION", tabId: tab.id });
    if (!response?.ok) {
      throw new Error(response?.error || "번역 영역 선택을 시작하지 못했습니다.");
    }
    // The popup otherwise blocks the first drag on some Chrome builds.
    window.close();
  }

  async function initialize() {
    setOptions(elements.source);
    setOptions(elements.target);
    setProviderOptions(elements.provider);
    [settings, [tab]] = await Promise.all([getSettings(), chrome.tabs.query({ active: true, currentWindow: true })]);
    if (tab?.id) {
      const response = await chrome.runtime.sendMessage({ type: "GET_TAB_STATE", tabId: tab.id });
      enabled = Boolean(response?.enabled);
    }
    render();
    await refreshHealth();
    await refreshTranslationLog().catch((error) => {
      translationLog = [];
      renderTranslationLog();
      console.warn("번역 로그를 불러오지 못했습니다.", error);
    });

    elements.source.addEventListener("change", saveLanguageSettings);
    elements.target.addEventListener("change", saveLanguageSettings);
    elements.provider.addEventListener("change", saveLanguageSettings);
    elements.autoScan.addEventListener("change", saveLanguageSettings);
    elements.toggle.addEventListener("click", () => toggle().catch((error) => setReadiness("error", error.message)));
    elements.rescan.addEventListener("click", () => chrome.runtime.sendMessage({ type: "REQUEST_RESCAN", tabId: tab?.id, reason: "manual" }));
    elements.selectRegion.addEventListener("click", () => selectCaptureRegion().catch((error) => setReadiness("error", error.message || "번역 영역을 지정하지 못했습니다.")));
    elements.copyOriginal.addEventListener("click", () => copyOriginalText().catch((error) => setReadiness("error", error.message || "원문을 복사하지 못했습니다.")));
    elements.refreshLog.addEventListener("click", () => runLogAction(refreshTranslationLog).catch((error) => setReadiness("error", error.message || "번역 로그를 불러오지 못했습니다.")));
    elements.copyLog.addEventListener("click", () => runLogAction(copyTranslationLog).catch((error) => setReadiness("error", error.message || "번역 로그를 복사하지 못했습니다.")));
    elements.clearLog.addEventListener("click", () => runLogAction(clearTranslationLog).catch((error) => setReadiness("error", error.message || "번역 로그를 비우지 못했습니다.")));
    elements.options.addEventListener("click", () => chrome.runtime.openOptionsPage());
  }

  initialize().catch((error) => setReadiness("error", error.message || "확장 프로그램을 초기화하지 못했습니다."));
})();
