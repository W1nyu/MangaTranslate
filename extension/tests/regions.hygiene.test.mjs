import test from "node:test";
import assert from "node:assert/strict";

import { regions, rect, candidate, makeIdFactory } from "./helpers.mjs";

const { mergeRegions, trimBandForClipped } = regions;

function merge(entries, incoming, options = {}) {
  return mergeRegions(entries, incoming, { makeId: makeIdFactory(), now: 1_000, ...options });
}

// A capture that read the whole first screen.
const COVERED = rect(0, 0, 1_000, 800);

test("a bubble the capture found again keeps its translation", () => {
  const first = merge([], [candidate({ documentRect: rect(10, 10, 200, 60), text: "hi" })]);
  const second = merge(first.entries, [candidate({ documentRect: rect(10, 10, 200, 60), text: "hi" })], {
    coveredRect: COVERED,
    now: 2_000
  });

  assert.equal(second.entries.length, 1);
  assert.equal(second.dropped, 0);
  assert.equal(second.entries[0].misses ?? 0, 0);
});

test("a stray patch is dropped once the same area has been read again enough times", () => {
  const seeded = merge([], [candidate({ documentRect: rect(10, 10, 200, 60), text: "noise" })]);

  // Two further captures cover it and never find it: still shown, in case OCR
  // simply missed it.
  const once = merge(seeded.entries, [], { coveredRect: COVERED, now: 2_000 });
  assert.equal(once.entries.length, 1);
  assert.equal(once.entries[0].misses, 1);

  const twice = merge(once.entries, [], { coveredRect: COVERED, now: 3_000 });
  assert.equal(twice.entries.length, 1);
  assert.equal(twice.entries[0].misses, 2);

  const thrice = merge(twice.entries, [], { coveredRect: COVERED, now: 4_000 });
  assert.equal(thrice.entries.length, 0, "a bubble that is repeatedly not there is removed");
  assert.equal(thrice.dropped, 1);
});

test("finding a bubble again resets its strikes", () => {
  const seeded = merge([], [candidate({ documentRect: rect(10, 10, 200, 60), text: "hi" })]);
  const missed = merge(seeded.entries, [], { coveredRect: COVERED, now: 2_000 });
  const found = merge(missed.entries, [candidate({ documentRect: rect(10, 10, 200, 60), text: "hi" })], {
    coveredRect: COVERED,
    now: 3_000
  });

  assert.equal(found.entries[0].misses, 0);
  const missedAgain = merge(found.entries, [], { coveredRect: COVERED, now: 4_000 });
  assert.equal(missedAgain.entries.length, 1);
});

test("a bubble outside the captured area is never counted as missing", () => {
  const seeded = merge([], [candidate({ documentRect: rect(10, 4_000, 200, 60), text: "far below" })]);

  for (let pass = 0; pass < 6; pass += 1) {
    const result = merge(seeded.entries, [], { coveredRect: COVERED, now: 2_000 + pass });
    assert.equal(result.entries.length, 1, "an off-screen bubble must survive");
    assert.equal(result.entries[0].misses ?? 0, 0);
  }
});

test("a bubble only partly inside the captured area is not counted as missing", () => {
  // Straddles the bottom edge of the capture, so this pass could not see it all.
  const seeded = merge([], [candidate({ documentRect: rect(10, 760, 200, 120), text: "half read" })]);
  const result = merge(seeded.entries, [], { coveredRect: COVERED, now: 2_000 });

  assert.equal(result.entries.length, 1);
  assert.equal(result.entries[0].misses ?? 0, 0);
});

test("without a covered area nothing is ever dropped", () => {
  const seeded = merge([], [candidate({ documentRect: rect(10, 10, 200, 60), text: "hi" })]);
  let entries = seeded.entries;
  for (let pass = 0; pass < 5; pass += 1) entries = merge(entries, [], { now: 2_000 + pass }).entries;

  assert.equal(entries.length, 1);
});

test("a band is cut back above a bubble clipped by the bottom of the capture", () => {
  const trimmed = trimBandForClipped({ top: 0, bottom: 1_000 }, [
    { left: 100, top: 880, width: 300, height: 120, edges: ["bottom"] }
  ]);

  assert.deepEqual(trimmed, { top: 0, bottom: 880 }, "the half-read bubble stays unread");
});

test("a band is cut back below a bubble clipped by the top of the capture", () => {
  const trimmed = trimBandForClipped({ top: 1_000, bottom: 2_000 }, [
    { left: 100, top: 980, width: 300, height: 120, edges: ["top"] }
  ]);

  assert.deepEqual(trimmed, { top: 1_100, bottom: 2_000 });
});

test("a bubble clipped only at the side does not change the band", () => {
  const band = { top: 0, bottom: 1_000 };
  assert.deepEqual(trimBandForClipped(band, [{ left: 0, top: 400, width: 200, height: 80, edges: ["left"] }]), band);
});

test("several clipped bubbles all narrow the band", () => {
  const trimmed = trimBandForClipped({ top: 0, bottom: 1_000 }, [
    { left: 0, top: 900, width: 200, height: 100, edges: ["bottom"] },
    { left: 400, top: 850, width: 200, height: 150, edges: ["bottom"] }
  ]);

  assert.deepEqual(trimmed, { top: 0, bottom: 850 });
});

test("a band swallowed entirely by clipped bubbles claims nothing", () => {
  assert.equal(trimBandForClipped({ top: 0, bottom: 1_000 }, [
    { left: 0, top: 0, width: 200, height: 100, edges: ["bottom"] }
  ]), null);
});

test("no clipped bubbles leaves the band untouched", () => {
  const band = { top: 200, bottom: 900 };
  assert.deepEqual(trimBandForClipped(band, []), band);
  assert.deepEqual(trimBandForClipped(band, undefined), band);
});
