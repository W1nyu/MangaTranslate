(() => {
  function normalizeEntry(entry) {
    return {
      id: entry.id || crypto.randomUUID(),
      source: String(entry.source || "").trim(),
      target: String(entry.target || "").trim(),
      sourceLanguage: entry.sourceLanguage || "en",
      targetLanguage: entry.targetLanguage || "ko",
      enabled: entry.enabled !== false,
      caseSensitive: Boolean(entry.caseSensitive),
      wholeWord: entry.wholeWord !== false,
      priority: Number.isFinite(Number(entry.priority)) ? Number(entry.priority) : 0
    };
  }

  function validateEntry(entry) {
    const normalized = normalizeEntry(entry);
    const errors = [];
    if (!normalized.source) errors.push("원문 표현을 입력하세요.");
    if (!normalized.target) errors.push("지정 번역을 입력하세요.");
    if (normalized.source.length > 160) errors.push("원문 표현은 160자 이하로 입력하세요.");
    if (normalized.target.length > 160) errors.push("지정 번역은 160자 이하로 입력하세요.");
    return { normalized, errors };
  }

  function entriesForLanguage(entries, sourceLanguage, targetLanguage) {
    return entries
      .map(normalizeEntry)
      .filter((entry) => entry.enabled && entry.sourceLanguage === sourceLanguage && entry.targetLanguage === targetLanguage)
      .sort((a, b) => b.priority - a.priority || b.source.length - a.source.length);
  }

  globalThis.WebtoonGlossary = {
    normalizeEntry,
    validateEntry,
    entriesForLanguage
  };
})();
