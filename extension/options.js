(() => {
  const {
    LANGUAGE_OPTIONS,
    getSettings,
    saveSettings,
    getGlossary,
    saveGlossary,
    languageLabel,
    translationProviderLabel,
    formatCharacterCount
  } = globalThis.WebtoonTranslator;
  const { normalizeEntry, validateEntry } = globalThis.WebtoonGlossary;
  const $ = (selector) => document.querySelector(selector);
  const elements = {
    serverUrl: $("[data-server-url]"),
    delay: $("[data-scan-delay]"),
    confidence: $("[data-min-confidence]"),
    opacity: $("[data-overlay-opacity]"),
    fontScale: $("[data-font-scale]"),
    hoverReveal: $("[data-hover-reveal]"),
    requireCompleteBubble: $("[data-require-complete-bubble]"),
    autoDetectComicRegion: $("[data-auto-detect-comic-region]"),
    allowPaidFallback: $("[data-allow-paid-fallback]"),
    paidUsage: $("[data-paid-usage]"),
    delayValue: $("[data-delay-value]"),
    confidenceValue: $("[data-confidence-value]"),
    opacityValue: $("[data-opacity-value]"),
    fontValue: $("[data-font-value]"),
    serverStatus: $("[data-server-status]"),
    healthCheck: $("[data-health-check]"),
    saveSettings: $("[data-save-settings]"),
    settingsFeedback: $("[data-settings-feedback]"),
    form: $("[data-glossary-form]"),
    entryId: $("[data-entry-id]"),
    source: $("[data-entry-source]"),
    target: $("[data-entry-target]"),
    sourceLanguage: $("[data-entry-source-language]"),
    targetLanguage: $("[data-entry-target-language]"),
    priority: $("[data-entry-priority]"),
    caseSensitive: $("[data-entry-case-sensitive]"),
    wholeWord: $("[data-entry-whole-word]"),
    enabled: $("[data-entry-enabled]"),
    resetForm: $("[data-reset-form]"),
    example: $("[data-use-example]"),
    list: $("[data-glossary-list]"),
    count: $("[data-entry-count]"),
    glossaryFeedback: $("[data-glossary-feedback]")
  };
  let settings;
  let glossary;

  function populateLanguages(select) {
    select.replaceChildren(...LANGUAGE_OPTIONS.map((language) => {
      const option = document.createElement("option");
      option.value = language.code;
      option.textContent = language.label;
      return option;
    }));
  }

  function updateRangeLabels() {
    elements.delayValue.textContent = `${elements.delay.value}ms`;
    elements.confidenceValue.textContent = `${Math.round(Number(elements.confidence.value) * 100)}%`;
    elements.opacityValue.textContent = `${Math.round(Number(elements.opacity.value) * 100)}%`;
    elements.fontValue.textContent = `${Math.round(Number(elements.fontScale.value) * 100)}%`;
  }

  function setFeedback(element, message, isError = false) {
    element.textContent = message;
    element.classList.toggle("error", isError);
  }

  function renderSettings() {
    elements.serverUrl.value = settings.serverUrl;
    elements.delay.value = settings.scanDelay;
    elements.confidence.value = settings.minConfidence;
    elements.opacity.value = settings.overlayOpacity;
    elements.fontScale.value = settings.fontScale;
    elements.hoverReveal.checked = settings.hoverReveal !== false;
    elements.requireCompleteBubble.checked = settings.requireCompleteBubble !== false;
    elements.autoDetectComicRegion.checked = settings.autoDetectComicRegion !== false;
    elements.allowPaidFallback.checked = settings.allowPaidFallback === true;
    updateRangeLabels();
  }

  function setPaidUsageText(message, kind = "") {
    elements.paidUsage.textContent = message;
    elements.paidUsage.className = `paid-usage${kind ? ` ${kind}` : ""}`;
  }

  function paidUsageSummary(usage) {
    if (!usage?.available) {
      return {
        kind: "off",
        message:
          "유료 키가 아직 없습니다. local-server/.env의 DEEPL_PRO_AUTH_KEY 또는 AZURE_TRANSLATOR_PAID_KEY를 채우면 이 전환을 쓸 수 있습니다."
      };
    }

    const used = formatCharacterCount(usage.usedCharacters);
    const month = String(usage.month || "이번 달");
    const engines = Object.entries(usage.byProvider || {})
      .filter(([, characters]) => Number(characters) > 0)
      .map(([code, characters]) => `${translationProviderLabel(code)} ${formatCharacterCount(characters)}`)
      .join(" · ");
    const detail = engines ? ` (${engines})` : "";

    if (!usage.limited) {
      return { kind: "warn", message: `${month} 유료 API 사용량 ${used}${detail} · 월 상한 없음 (PAID_MONTHLY_CHAR_LIMIT=0)` };
    }

    const limit = formatCharacterCount(usage.monthlyLimit);
    const remaining = formatCharacterCount(usage.remainingCharacters);
    const exhausted = Number(usage.remainingCharacters) <= 0;
    return {
      kind: exhausted ? "warn" : "",
      message: exhausted
        ? `${month} 유료 API 월 상한 ${limit}를 모두 사용했습니다${detail}. 다음 달까지 유료 전환은 멈춥니다.`
        : `${month} 유료 API 사용량 ${used} / ${limit} · 남은 한도 ${remaining}${detail}`
    };
  }

  async function refreshPaidUsage() {
    try {
      const response = await chrome.runtime.sendMessage({
        type: "GET_PAID_USAGE",
        serverUrl: elements.serverUrl.value || settings?.serverUrl
      });
      if (!response?.ok) throw new Error(response?.error || "유료 API 사용량을 불러오지 못했습니다.");
      const summary = paidUsageSummary(response.usage);
      setPaidUsageText(summary.message, summary.kind);
    } catch {
      // The reader may simply not have the server running yet; that is not an
      // error worth colouring red on a settings page.
      setPaidUsageText("로컬 서버에 연결되면 이번 달 유료 API 사용량을 보여 줍니다.", "off");
    }
  }

  function renderList() {
    elements.list.replaceChildren();
    elements.count.textContent = `${glossary.length}개`;
    if (!glossary.length) {
      const empty = document.createElement("p");
      empty.className = "empty-list";
      empty.textContent = "아직 등록한 표현이 없어요. 자주 나오는 이름이나 지명을 추가해 보세요.";
      elements.list.append(empty);
      return;
    }
    const sorted = [...glossary].sort((a, b) => b.priority - a.priority || b.source.length - a.source.length);
    for (const entry of sorted) {
      const item = document.createElement("article");
      item.className = `glossary-item${entry.enabled ? "" : " disabled"}`;
      const source = document.createElement("div");
      const sourceTerm = document.createElement("div");
      sourceTerm.className = "term";
      sourceTerm.textContent = entry.source;
      const sourceMeta = document.createElement("div");
      sourceMeta.className = "item-meta";
      sourceMeta.textContent = `${languageLabel(entry.sourceLanguage)} → ${languageLabel(entry.targetLanguage)}`;
      source.append(sourceTerm, sourceMeta);

      const target = document.createElement("div");
      const targetTerm = document.createElement("div");
      targetTerm.className = "term target";
      targetTerm.textContent = entry.target;
      const targetMeta = document.createElement("div");
      targetMeta.className = "item-meta";
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = `우선 ${entry.priority}`;
      targetMeta.append(tag);
      if (entry.caseSensitive) {
        const caseTag = document.createElement("span");
        caseTag.className = "tag";
        caseTag.textContent = "Aa";
        targetMeta.append(caseTag);
      }
      target.append(targetTerm, targetMeta);

      const arrow = document.createElement("span");
      arrow.className = "item-arrow";
      arrow.textContent = "→";
      const actions = document.createElement("div");
      actions.className = "item-actions";
      const edit = document.createElement("button");
      edit.type = "button";
      edit.textContent = "수정";
      edit.addEventListener("click", () => loadEntry(entry));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "delete";
      remove.textContent = "삭제";
      remove.addEventListener("click", () => removeEntry(entry.id));
      actions.append(edit, remove);
      item.append(source, arrow, target, actions);
      elements.list.append(item);
    }
  }

  function resetForm() {
    elements.form.reset();
    elements.entryId.value = "";
    elements.sourceLanguage.value = settings.sourceLanguage;
    elements.targetLanguage.value = settings.targetLanguage;
    elements.priority.value = "100";
    elements.caseSensitive.checked = true;
    elements.wholeWord.checked = true;
    elements.enabled.checked = true;
    $("[data-save-entry]").textContent = "사전에 저장";
  }

  function loadEntry(entry) {
    elements.entryId.value = entry.id;
    elements.source.value = entry.source;
    elements.target.value = entry.target;
    elements.sourceLanguage.value = entry.sourceLanguage;
    elements.targetLanguage.value = entry.targetLanguage;
    elements.priority.value = entry.priority;
    elements.caseSensitive.checked = entry.caseSensitive;
    elements.wholeWord.checked = entry.wholeWord;
    elements.enabled.checked = entry.enabled;
    $("[data-save-entry]").textContent = "수정 내용 저장";
    elements.source.focus();
    setFeedback(elements.glossaryFeedback, `“${entry.source}” 규칙을 수정하고 있어요.`);
  }

  async function saveEntry(event) {
    event.preventDefault();
    const result = validateEntry({
      id: elements.entryId.value || crypto.randomUUID(),
      source: elements.source.value,
      target: elements.target.value,
      sourceLanguage: elements.sourceLanguage.value,
      targetLanguage: elements.targetLanguage.value,
      priority: Number(elements.priority.value),
      caseSensitive: elements.caseSensitive.checked,
      wholeWord: elements.wholeWord.checked,
      enabled: elements.enabled.checked
    });
    if (result.errors.length) {
      setFeedback(elements.glossaryFeedback, result.errors[0], true);
      return;
    }
    const index = glossary.findIndex((entry) => entry.id === result.normalized.id);
    if (index >= 0) glossary.splice(index, 1, result.normalized);
    else glossary.push(result.normalized);
    await saveGlossary(glossary);
    renderList();
    resetForm();
    setFeedback(elements.glossaryFeedback, "고유명사 규칙을 저장했고, 다음 화면 번역부터 적용합니다.");
  }

  async function removeEntry(id) {
    const entry = glossary.find((item) => item.id === id);
    glossary = glossary.filter((item) => item.id !== id);
    await saveGlossary(glossary);
    renderList();
    if (elements.entryId.value === id) resetForm();
    setFeedback(elements.glossaryFeedback, `“${entry?.source || "규칙"}”을 삭제했습니다.`);
  }

  async function persistSettings() {
    try {
      settings = await saveSettings({
        serverUrl: elements.serverUrl.value,
        scanDelay: Number(elements.delay.value),
        minConfidence: Number(elements.confidence.value),
        overlayOpacity: Number(elements.opacity.value),
        fontScale: Number(elements.fontScale.value),
        hoverReveal: elements.hoverReveal.checked,
        requireCompleteBubble: elements.requireCompleteBubble.checked,
        autoDetectComicRegion: elements.autoDetectComicRegion.checked,
        allowPaidFallback: elements.allowPaidFallback.checked
      });
      renderSettings();
      await refreshPaidUsage();
      setFeedback(
        elements.settingsFeedback,
        settings.allowPaidFallback
          ? "설정을 저장했습니다. 무료 한도가 끝나면 유료 API로 이어서 번역합니다."
          : "설정을 저장했습니다. 켜져 있는 번역 레이어에 즉시 반영됩니다."
      );
    } catch (error) {
      setFeedback(elements.settingsFeedback, error.message || "설정을 저장하지 못했습니다.", true);
    }
  }

  async function checkHealth() {
    elements.serverStatus.textContent = "확인 중";
    elements.serverStatus.className = "status-badge";
    try {
      const response = await chrome.runtime.sendMessage({ type: "GET_RUNTIME_STATUS", serverUrl: elements.serverUrl.value });
      if (!response?.ok) throw new Error(response?.error || "서버에 연결할 수 없습니다.");
      const runtime = response.runtime;
      const health = runtime?.health;
      const compatible = runtime?.compatible === true;
      elements.serverStatus.textContent = compatible ? "로컬 서버 연결됨" : "버전 불일치";
      elements.serverStatus.className = `status-badge ${compatible ? "ready" : "warn"}`;
      const ocrLanguageCount = health?.supportedOcrLanguages?.length || 0;
      const summary = paidUsageSummary(health?.paidUsage);
      setPaidUsageText(summary.message, summary.kind);
      setFeedback(
        elements.settingsFeedback,
        compatible
          ? `확장 ${runtime.extensionVersion} · 서버 ${runtime.serverVersion}이 연결되었습니다. 지원 OCR 언어: ${ocrLanguageCount}개`
          : `확장 ${runtime?.extensionVersion || "?"} · 서버 ${runtime?.serverVersion || "?"}입니다. 소스 업데이트 뒤에는 로컬 서버를 다시 시작해 버전을 맞춰 주세요.`
      );
    } catch (error) {
      elements.serverStatus.textContent = "연결 안 됨";
      elements.serverStatus.className = "status-badge error";
      setPaidUsageText("로컬 서버에 연결되면 이번 달 유료 API 사용량을 보여 줍니다.", "off");
      setFeedback(elements.settingsFeedback, error.message || "로컬 서버에 연결할 수 없습니다.", true);
    }
  }

  async function initialize() {
    populateLanguages(elements.sourceLanguage);
    populateLanguages(elements.targetLanguage);
    [settings, glossary] = await Promise.all([getSettings(), getGlossary()]);
    glossary = glossary.map(normalizeEntry);
    renderSettings();
    renderList();
    resetForm();
    checkHealth();

    [elements.delay, elements.confidence, elements.opacity, elements.fontScale].forEach((input) => input.addEventListener("input", updateRangeLabels));
    elements.saveSettings.addEventListener("click", persistSettings);
    elements.healthCheck.addEventListener("click", checkHealth);
    elements.form.addEventListener("submit", saveEntry);
    elements.resetForm.addEventListener("click", resetForm);
    elements.example.addEventListener("click", () => {
      resetForm();
      elements.source.value = "SEOUL";
      elements.target.value = "세울";
      elements.sourceLanguage.value = "en";
      elements.targetLanguage.value = "ko";
      elements.caseSensitive.checked = true;
      elements.wholeWord.checked = true;
      elements.source.focus();
      setFeedback(elements.glossaryFeedback, "예시를 채웠습니다. ‘사전에 저장’을 누르면 적용됩니다.");
    });
  }

  initialize().catch((error) => setFeedback(elements.settingsFeedback, error.message || "설정을 불러오지 못했습니다.", true));
})();
