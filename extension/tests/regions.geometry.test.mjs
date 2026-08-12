import test from "node:test";
import assert from "node:assert/strict";

import { regions, rect } from "./helpers.mjs";

const { rectIoU, rectsIntersect, normalizeRegionText, resolveAnchorRect, elementAnchor, documentAnchor } = regions;

test("rectIoU is 1 for identical rectangles and 0 for disjoint ones", () => {
  assert.equal(rectIoU(rect(0, 0, 10, 10), rect(0, 0, 10, 10)), 1);
  assert.equal(rectIoU(rect(0, 0, 10, 10), rect(20, 20, 10, 10)), 0);
});

test("rectIoU reports partial overlap", () => {
  // 5x10 shared out of a 150 union.
  assert.equal(rectIoU(rect(0, 0, 10, 10), rect(5, 0, 10, 10)), 50 / 150);
});

test("rectIoU rejects degenerate rectangles", () => {
  assert.equal(rectIoU(rect(0, 0, 0, 10), rect(0, 0, 10, 10)), 0);
  assert.equal(rectIoU(null, rect(0, 0, 10, 10)), 0);
});

test("rectsIntersect treats edge contact as separate but honours a margin", () => {
  assert.equal(rectsIntersect(rect(0, 0, 10, 10), rect(10, 0, 10, 10)), false);
  assert.equal(rectsIntersect(rect(0, 0, 10, 10), rect(10, 0, 10, 10), 1), true);
  assert.equal(rectsIntersect(rect(0, 0, 10, 10), rect(9, 0, 10, 10)), true);
});

test("normalizeRegionText folds case, width variants and grouped line breaks", () => {
  assert.equal(normalizeRegionText("  Hello\nWORLD  "), "hello world");
  assert.equal(normalizeRegionText("ＨＥＬＬＯ"), "hello");
  assert.equal(normalizeRegionText(null), "");
});

test("an element anchor follows its panel when layout shifts", () => {
  const panel = rect(100, 500, 800, 1200);
  const bubble = rect(300, 800, 200, 100);
  const anchor = elementAnchor("panel-3.jpg|0", panel, bubble);

  // Same layout resolves back to the original rectangle.
  assert.deepEqual(resolveAnchorRect(anchor, new Map([["panel-3.jpg|0", panel]])), bubble);

  // A lazily loaded image above pushed the panel down by 400px.
  const moved = resolveAnchorRect(anchor, new Map([["panel-3.jpg|0", rect(100, 900, 800, 1200)]]));
  assert.deepEqual(moved, rect(300, 1200, 200, 100));
});

test("an element anchor scales with its panel", () => {
  const anchor = elementAnchor("p|0", rect(0, 0, 100, 100), rect(20, 30, 40, 20));
  const scaled = resolveAnchorRect(anchor, { "p|0": rect(0, 0, 200, 200) });
  assert.deepEqual(scaled, rect(40, 60, 80, 40));
});

test("an unresolvable element anchor yields no rectangle", () => {
  const anchor = elementAnchor("gone.jpg|0", rect(0, 0, 100, 100), rect(10, 10, 20, 20));
  assert.equal(resolveAnchorRect(anchor, new Map()), null);
});

test("a document anchor resolves without any element map", () => {
  assert.deepEqual(resolveAnchorRect(documentAnchor(rect(5, 6, 7, 8)), new Map()), rect(5, 6, 7, 8));
  assert.equal(documentAnchor(rect(5, 6, 0, 8)), null);
});
