import test from "node:test";
import assert from "node:assert/strict";

import { loadSharedScript } from "./helpers.mjs";

loadSharedScript("shared/defaults.js");
const {
  DEFAULT_SETTINGS,
  normalizeScanDelay,
  normalizeMinConfidence,
  normalizeServerUrl,
  validateServerUrl
} = globalThis.WebtoonTranslator;

test("the page must sit still for about half a second before a scan", () => {
  assert.equal(DEFAULT_SETTINGS.scanDelay, 500);
});

test("hover reveal and complete-bubble filtering are on by default", () => {
  assert.equal(DEFAULT_SETTINGS.hoverReveal, true);
  assert.equal(DEFAULT_SETTINGS.requireCompleteBubble, true);
  assert.equal(DEFAULT_SETTINGS.autoDetectComicRegion, true);
});

test("low-contrast comic lettering gets the more forgiving confidence default", () => {
  assert.equal(DEFAULT_SETTINGS.minConfidence, 0.45);
  assert.equal(normalizeMinConfidence(0.55), 0.45, "the previous default migrates automatically");
  assert.equal(normalizeMinConfidence(0.7), 0.7, "a reader's explicit choice is preserved");
});

test("a stored value equal to any previous default follows the new default", () => {
  assert.equal(normalizeScanDelay(750), DEFAULT_SETTINGS.scanDelay);
  assert.equal(normalizeScanDelay(300), DEFAULT_SETTINGS.scanDelay);
});

test("a value the reader actually chose is preserved", () => {
  assert.equal(normalizeScanDelay(1_200), 1_200);
  assert.equal(normalizeScanDelay(400), 400);
});

test("out of range and missing values are brought back into range", () => {
  assert.equal(normalizeScanDelay(10), 200);
  assert.equal(normalizeScanDelay(99_999), 2_500);
  assert.equal(normalizeScanDelay(undefined), DEFAULT_SETTINGS.scanDelay);
  assert.equal(normalizeScanDelay("nope"), DEFAULT_SETTINGS.scanDelay);
});

test("the server url keeps its loopback default and is stored as an origin", () => {
  assert.equal(normalizeServerUrl(""), "http://127.0.0.1:8765");
  assert.equal(normalizeServerUrl("http://localhost:8765/"), "http://localhost:8765");
});

test("only a plain HTTP loopback origin can be saved as the local server", () => {
  assert.equal(validateServerUrl(" http://LOCALHOST:9876/ "), "http://localhost:9876");
  assert.throws(() => validateServerUrl("https://localhost:8765"), /http/);
  assert.throws(() => validateServerUrl("http://example.com"), /로컬 서버/);
  assert.throws(() => validateServerUrl("http://user@127.0.0.1:8765"), /로컬 서버/);
  assert.throws(() => validateServerUrl("http://127.0.0.1:8765/v1"), /포트까지만/);
});

test("a stale invalid server setting safely returns to the loopback default", () => {
  assert.equal(normalizeServerUrl("https://example.com"), DEFAULT_SETTINGS.serverUrl);
});
