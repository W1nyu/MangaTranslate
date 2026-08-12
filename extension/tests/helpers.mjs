import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const extensionRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

// The shared browser modules are classic IIFE scripts that publish onto
// globalThis, matching how `background.js` injects them. Evaluating the file
// verbatim keeps the browser sources free of any test-only export shim.
export function loadSharedScript(relativePath) {
  const source = readFileSync(join(extensionRoot, relativePath), "utf8");
  new Function(source)();
}

loadSharedScript("shared/regions.js");

export const regions = globalThis.WebtoonRegions;

let sequence = 0;

/** Deterministic entry ids so merge results can be asserted directly. */
export function makeIdFactory(prefix = "id") {
  sequence = 0;
  return () => {
    sequence += 1;
    return `${prefix}-${sequence}`;
  };
}

export function rect(left, top, width, height) {
  return { left, top, width, height };
}

/** An OCR result already converted into the store's candidate shape. */
export function candidate(overrides = {}) {
  return {
    anchor: null,
    documentRect: rect(0, 0, 100, 40),
    text: "hello",
    translation: "안녕",
    confidence: 0.9,
    sourceLanguage: "en",
    targetLanguage: "ko",
    provider: "deepl",
    ...overrides
  };
}
