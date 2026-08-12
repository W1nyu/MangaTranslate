import test from "node:test";
import assert from "node:assert/strict";

import { loadSharedScript } from "./helpers.mjs";

loadSharedScript("shared/defaults.js");
const { DEFAULT_SETTINGS, normalizeScanDelay, normalizeServerUrl } = globalThis.WebtoonTranslator;

test("the page must sit still for about half a second before a scan", () => {
  assert.equal(DEFAULT_SETTINGS.scanDelay, 500);
});

test("hover reveal and complete-bubble filtering are on by default", () => {
  assert.equal(DEFAULT_SETTINGS.hoverReveal, true);
  assert.equal(DEFAULT_SETTINGS.requireCompleteBubble, true);
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

test("the server url keeps its loopback default and loses a trailing slash", () => {
  assert.equal(normalizeServerUrl(""), "http://127.0.0.1:8765");
  assert.equal(normalizeServerUrl("http://localhost:8765/"), "http://localhost:8765");
});
