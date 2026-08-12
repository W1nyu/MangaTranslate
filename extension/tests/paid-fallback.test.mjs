import test from "node:test";
import assert from "node:assert/strict";

import { loadSharedScript } from "./helpers.mjs";

loadSharedScript("shared/defaults.js");
const {
  DEFAULT_SETTINGS,
  TRANSLATION_PROVIDER_OPTIONS,
  translationProviderLabel,
  isPaidTranslationProvider,
  formatCharacterCount,
  normalizeScanDelay
} = globalThis.WebtoonTranslator;

test("spending money is off until the reader turns it on", () => {
  assert.equal(DEFAULT_SETTINGS.allowPaidFallback, false);
});

test("a paid engine is never offered in the engine dropdown", () => {
  const selectable = TRANSLATION_PROVIDER_OPTIONS.map((provider) => provider.code);
  assert.deepEqual(selectable, ["auto", "azure", "deepl"]);
  assert.ok(!selectable.some(isPaidTranslationProvider));
});

test("an engine that answered can still be named even when it is a paid one", () => {
  assert.equal(translationProviderLabel("deepl_pro"), "DeepL API Pro (유료)");
  assert.equal(translationProviderLabel("azure_paid"), "Azure Translator (유료)");
  assert.equal(translationProviderLabel("deepl"), "DeepL API Free");
  // An unknown code from a newer server falls through to itself rather than
  // rendering "undefined" over the comic.
  assert.equal(translationProviderLabel("something-new"), "something-new");
});

test("only the paid codes are treated as paid", () => {
  assert.equal(isPaidTranslationProvider("deepl_pro"), true);
  assert.equal(isPaidTranslationProvider("azure_paid"), true);
  assert.equal(isPaidTranslationProvider("deepl"), false);
  assert.equal(isPaidTranslationProvider("auto"), false);
  assert.equal(isPaidTranslationProvider(undefined), false);
  // Object.prototype keys must not read as a paid engine.
  assert.equal(isPaidTranslationProvider("toString"), false);
});

test("usage figures stay readable and never render NaN", () => {
  assert.equal(formatCharacterCount(0), "0자");
  assert.equal(formatCharacterCount(1234567), "1,234,567자");
  assert.equal(formatCharacterCount(null), "0자");
  assert.equal(formatCharacterCount(undefined), "0자");
  assert.equal(formatCharacterCount("nope"), "0자");
  assert.equal(formatCharacterCount(-5), "0자");
});

test("the new setting does not disturb the existing normalizers", () => {
  assert.equal(normalizeScanDelay(750), DEFAULT_SETTINGS.scanDelay);
});
