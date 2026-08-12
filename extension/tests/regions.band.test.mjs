import test from "node:test";
import assert from "node:assert/strict";

import { regions } from "./helpers.mjs";

const { incrementalBand, addCoverage, uncoveredSpan } = regions;

const VIEWPORT_HEIGHT = 1_000;

function band(options = {}) {
  return incrementalBand({
    viewport: { pageTop: 0, height: VIEWPORT_HEIGHT },
    captureRegion: null,
    overlapPx: 100,
    minBandPx: 48,
    maxBandRatio: 0.72,
    ...options
  });
}

/** Scroll to `pageTop` carrying coverage forward, as the controller does. */
function scrollTo(coverage, pageTop, options = {}) {
  return band({ coverage, viewport: { pageTop, height: VIEWPORT_HEIGHT }, ...options });
}

test("addCoverage merges touching and overlapping ranges", () => {
  assert.deepEqual(addCoverage([], { top: 0, bottom: 100 }), [{ top: 0, bottom: 100 }]);
  assert.deepEqual(addCoverage([{ top: 0, bottom: 100 }], { top: 80, bottom: 200 }), [{ top: 0, bottom: 200 }]);
  assert.deepEqual(addCoverage([{ top: 0, bottom: 100 }], { top: 100, bottom: 200 }), [{ top: 0, bottom: 200 }]);
  assert.deepEqual(
    addCoverage([{ top: 0, bottom: 100 }], { top: 300, bottom: 400 }),
    [{ top: 0, bottom: 100 }, { top: 300, bottom: 400 }]
  );
});

test("addCoverage ignores degenerate ranges", () => {
  assert.deepEqual(addCoverage([], { top: 100, bottom: 100 }), []);
  assert.deepEqual(addCoverage([{ top: 0, bottom: 50 }], null), [{ top: 0, bottom: 50 }]);
});

test("addCoverage keeps the ranges nearest what is being read", () => {
  let coverage = [];
  for (let index = 0; index < 8; index += 1) {
    coverage = addCoverage(coverage, { top: index * 1_000, bottom: index * 1_000 + 100 }, { maxRanges: 3 });
  }

  assert.equal(coverage.length, 3);
  assert.deepEqual(coverage.at(-1), { top: 7_000, bottom: 7_100 }, "the range just read is kept");
});

test("uncoveredSpan finds unread page at either end and in the middle", () => {
  const coverage = [{ top: 0, bottom: 100 }, { top: 300, bottom: 400 }];
  assert.equal(uncoveredSpan(coverage, 0, 100), null);
  assert.deepEqual(uncoveredSpan(coverage, 50, 200), { top: 100, bottom: 200 });
  assert.deepEqual(uncoveredSpan(coverage, 250, 400), { top: 250, bottom: 300 });
  assert.deepEqual(uncoveredSpan(coverage, 50, 350), { top: 100, bottom: 300 });
  assert.deepEqual(uncoveredSpan([], 0, 100), { top: 0, bottom: 100 });
});

test("the first scan of a page reads everything", () => {
  const result = band({ coverage: [] });

  assert.equal(result.mode, "full");
  assert.equal(result.captureRegion, null);
  assert.deepEqual(result.coverage, [{ top: 0, bottom: 1_000 }]);
});

test("scrolling down reads only the newly revealed strip plus a seam overlap", () => {
  const result = scrollTo([{ top: 0, bottom: 1_000 }], 300);

  assert.equal(result.mode, "band");
  // Unread is 1000..1300; the overlap reaches back to 900 so a bubble across
  // the seam is captured whole.
  assert.equal(result.captureRegion.y * VIEWPORT_HEIGHT + 300, 900);
  assert.equal(result.captureRegion.height * VIEWPORT_HEIGHT, 400);
  assert.deepEqual(result.coverage, [{ top: 0, bottom: 1_300 }]);
});

test("scrolling up reads the strip revealed above", () => {
  const result = scrollTo([{ top: 1_000, bottom: 2_000 }], 700);

  assert.equal(result.mode, "band");
  assert.equal(result.captureRegion.y * VIEWPORT_HEIGHT + 700, 700);
  assert.equal(result.captureRegion.height * VIEWPORT_HEIGHT, 400);
  assert.deepEqual(result.coverage, [{ top: 700, bottom: 2_000 }]);
});

test("a screen that has already been read needs no capture at all", () => {
  const result = band({ coverage: [{ top: 0, bottom: 1_000 }], viewport: { pageTop: 200, height: 400 } });

  assert.equal(result.mode, "none");
  assert.deepEqual(result.coverage, [{ top: 0, bottom: 1_000 }]);
});

test("scrolling back to a screen read much earlier is free", () => {
  // Read the top, then work down four screens, then come back to the top.
  let coverage = band({ coverage: [] }).coverage;
  for (const pageTop of [400, 800, 1_200, 1_600, 2_000]) {
    coverage = scrollTo(coverage, pageTop).coverage;
  }

  const back = scrollTo(coverage, 0);

  assert.equal(back.mode, "none", "returning to already-read page must not re-OCR it");
});

test("a scroll too small to hold text is deferred without extending coverage", () => {
  const coverage = [{ top: 0, bottom: 1_000 }];
  const result = scrollTo(coverage, 20);

  assert.equal(result.mode, "none");
  assert.deepEqual(result.coverage, coverage, "coverage must not absorb the unread sliver");
});

test("repeated tiny scrolls accumulate until the strip is worth reading", () => {
  const coverage = [{ top: 0, bottom: 1_000 }];
  assert.equal(scrollTo(coverage, 20).mode, "none");
  assert.equal(scrollTo(coverage, 40).mode, "none");
  assert.equal(scrollTo(coverage, 60).mode, "band");
});

test("a large jump falls back to a full capture", () => {
  const result = scrollTo([{ top: 0, bottom: 1_000 }], 900);

  assert.equal(result.mode, "full");
  assert.deepEqual(result.coverage, [{ top: 0, bottom: 1_900 }]);
});

test("a jump into completely unread page is a full capture", () => {
  const result = scrollTo([{ top: 0, bottom: 1_000 }], 8_000);

  assert.equal(result.mode, "full");
  assert.deepEqual(result.coverage, [{ top: 0, bottom: 1_000 }, { top: 8_000, bottom: 9_000 }]);
});

test("a changed context discards coverage entirely", () => {
  const result = scrollTo([{ top: 0, bottom: 1_000 }], 300, { sameContext: false });

  assert.equal(result.mode, "full");
  assert.deepEqual(result.coverage, [{ top: 300, bottom: 1_300 }], "old ranges must not survive a reflow");
});

test("a selected capture region constrains the strip and is preserved", () => {
  const captureRegion = { x: 0.1, y: 0.2, width: 0.6, height: 0.5 };
  const first = band({ coverage: [], captureRegion });
  assert.equal(first.mode, "full");
  assert.equal(first.captureRegion, captureRegion, "a full scan reuses the caller's region object");
  assert.deepEqual(first.coverage, [{ top: 200, bottom: 700 }]);

  const second = scrollTo(first.coverage, 200, { captureRegion });

  assert.equal(second.mode, "band");
  assert.equal(second.captureRegion.x, 0.1, "the horizontal selection is untouched");
  assert.equal(second.captureRegion.width, 0.6);
  // The region now covers 400..900; unread is 700..900, overlapped back to 600.
  assert.equal(second.captureRegion.y * VIEWPORT_HEIGHT + 200, 600);
  assert.equal(second.captureRegion.height * VIEWPORT_HEIGHT, 300);
});

test("an unread gap between two read ranges is captured as one strip", () => {
  const coverage = [{ top: 0, bottom: 400 }, { top: 800, bottom: 2_000 }];
  const result = scrollTo(coverage, 100, { overlapPx: 20, maxBandRatio: 0.9 });

  assert.equal(result.mode, "band");
  // Unread is 400..800, widened by the seam overlap on both sides.
  assert.equal(result.captureRegion.y * VIEWPORT_HEIGHT + 100, 380);
  assert.equal(result.captureRegion.height * VIEWPORT_HEIGHT, 440);
});

test("the strip never escapes the viewport in normalized coordinates", () => {
  for (const pageTop of [0, 137, 900, 2_500]) {
    const result = scrollTo([{ top: 0, bottom: 1_000 }, { top: 2_400, bottom: 2_600 }], pageTop);
    if (result.mode !== "band") continue;
    assert.ok(result.captureRegion.y >= 0, `y ${result.captureRegion.y} at ${pageTop}`);
    assert.ok(result.captureRegion.y + result.captureRegion.height <= 1 + 1e-9, `overflow at ${pageTop}`);
  }
});
