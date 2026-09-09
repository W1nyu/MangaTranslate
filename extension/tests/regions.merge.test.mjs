import test from "node:test";
import assert from "node:assert/strict";

import { regions, rect, candidate, makeIdFactory } from "./helpers.mjs";

const { mergeRegions, elementAnchor, documentAnchor } = regions;

function merge(entries, incoming, options = {}) {
  return mergeRegions(entries, incoming, { makeId: makeIdFactory(), now: 1_000, ...options });
}

test("a first capture creates one entry per region", () => {
  const result = merge([], [
    candidate({ documentRect: rect(0, 0, 100, 40), text: "one" }),
    candidate({ documentRect: rect(0, 200, 100, 40), text: "two" })
  ]);

  assert.equal(result.entries.length, 2);
  assert.equal(result.added, 2);
  assert.equal(result.updated, 0);
  assert.deepEqual(result.entries.map((entry) => entry.text), ["one", "two"]);
});

test("a contained OCR subset does not create a second translation patch", () => {
  const result = merge([], [
    candidate({ documentRect: rect(100, 80, 260, 100), text: "I will not abandon you." }),
    candidate({ documentRect: rect(122, 116, 190, 24), text: "not abandon you" })
  ]);

  assert.equal(result.entries.length, 1);
  assert.equal(result.added, 1);
  assert.equal(result.entries[0].text, "I will not abandon you.");
});

test("a repeated phrase outside the complete bubble remains a separate target", () => {
  const result = merge([], [
    candidate({ documentRect: rect(100, 80, 260, 100), text: "I will not abandon you." }),
    candidate({ documentRect: rect(440, 116, 190, 24), text: "not abandon you" })
  ]);

  assert.equal(result.entries.length, 2);
  assert.deepEqual(result.entries.map((entry) => entry.text), ["I will not abandon you.", "not abandon you"]);
});

test("re-detecting the same bubble updates it instead of duplicating", () => {
  const first = merge([], [candidate({ documentRect: rect(0, 0, 100, 40), text: "hello" })]);
  // The next capture shifts the detected box slightly, as OCR normally does.
  const second = merge(first.entries, [candidate({ documentRect: rect(3, 2, 102, 41), text: "hello" })], { now: 2_000 });

  assert.equal(second.entries.length, 1);
  assert.equal(second.updated, 1);
  assert.equal(second.added, 0);
  assert.equal(second.entries[0].id, first.entries[0].id, "the entry keeps its identity");
  assert.equal(second.entries[0].createdAt, 1_000);
  assert.equal(second.entries[0].lastSeenAt, 2_000);
});

test("a seam duplicate from an incremental strip merges into the existing entry", () => {
  // The bubble was first read whole; the strip capture re-reads it lower down
  // with a different box because only part of it was in the strip.
  const first = merge([], [candidate({ documentRect: rect(40, 1_000, 220, 90), text: "I told you already" })]);
  const second = merge(
    first.entries,
    [candidate({ documentRect: rect(46, 1_012, 210, 84), text: "I told you already", translation: "" })],
    { now: 2_000 }
  );

  assert.equal(second.entries.length, 1);
  assert.equal(second.updated, 1);
  assert.equal(second.entries[0].translation, "안녕", "an empty re-read keeps the known translation");
});

test("identical text far away stays a separate bubble", () => {
  const first = merge([], [candidate({ documentRect: rect(0, 0, 100, 40), text: "what?" })]);
  const second = merge(
    first.entries,
    [candidate({ documentRect: rect(0, 4_000, 100, 40), text: "what?" })],
    { now: 2_000, textProximity: 80 }
  );

  assert.equal(second.entries.length, 2);
  assert.equal(second.added, 1);
});

test("identical text nearby merges even when the box changed a lot", () => {
  const first = merge([], [candidate({ documentRect: rect(0, 0, 60, 20), text: "what?" })]);
  // Grouping joined the line into a full bubble box, so IoU alone would fail.
  const second = merge(
    first.entries,
    [candidate({ documentRect: rect(0, 0, 240, 96), text: "what?" })],
    { now: 2_000, textProximity: 200 }
  );

  assert.equal(second.entries.length, 1);
  assert.equal(second.updated, 1);
});

test("different bubbles in the same place on different panels stay separate", () => {
  const panelA = rect(0, 0, 800, 1_200);
  const panelB = rect(0, 1_200, 800, 1_200);
  const first = merge([], [
    candidate({
      anchor: elementAnchor("a.jpg|0", panelA, rect(100, 100, 200, 80)),
      documentRect: rect(100, 100, 200, 80),
      text: "first panel"
    })
  ]);
  const second = merge(
    first.entries,
    [
      candidate({
        anchor: elementAnchor("b.jpg|0", panelB, rect(100, 1_300, 200, 80)),
        documentRect: rect(100, 1_300, 200, 80),
        text: "second panel"
      })
    ],
    { now: 2_000 }
  );

  assert.equal(second.entries.length, 2);
});

test("one capture never folds two distinct regions into a single entry", () => {
  const existing = merge([], [candidate({ documentRect: rect(0, 0, 100, 40), text: "a" })]);
  // Both incoming boxes overlap the stored one, but they are two bubbles.
  const result = merge(
    existing.entries,
    [
      candidate({ documentRect: rect(0, 0, 100, 40), text: "a" }),
      candidate({ documentRect: rect(2, 1, 100, 40), text: "b" })
    ],
    { now: 2_000 }
  );

  assert.equal(result.entries.length, 2);
  assert.equal(result.added, 1);
  assert.equal(result.updated, 1);
});

test("element anchored regions match through a layout shift", () => {
  const before = rect(0, 0, 800, 1_200);
  const after = rect(0, 640, 800, 1_200);
  const first = merge([], [
    candidate({
      anchor: elementAnchor("p.jpg|0", before, rect(100, 300, 200, 80)),
      documentRect: rect(100, 300, 200, 80),
      text: "hi"
    })
  ]);
  // The panel moved down, so the document rect differs entirely; the shared
  // element key and relative box still identify the same bubble.
  const second = merge(
    first.entries,
    [
      candidate({
        anchor: elementAnchor("p.jpg|0", after, rect(100, 940, 200, 80)),
        documentRect: rect(100, 940, 200, 80),
        text: "hi"
      })
    ],
    { now: 2_000, textProximity: 0 }
  );

  assert.equal(second.entries.length, 1);
  assert.equal(second.updated, 1);
});

test("the store drops the least recently seen entries past its cap", () => {
  const seeded = merge([], [
    candidate({ documentRect: rect(0, 0, 50, 20), text: "oldest" }),
    candidate({ documentRect: rect(0, 100, 50, 20), text: "middle" })
  ]);
  const refreshed = merge(seeded.entries, [candidate({ documentRect: rect(0, 100, 50, 20), text: "middle" })], { now: 3_000 });
  const capped = merge(
    refreshed.entries,
    [candidate({ documentRect: rect(0, 400, 50, 20), text: "newest" })],
    { now: 4_000, maxEntries: 2 }
  );

  assert.deepEqual(capped.entries.map((entry) => entry.text), ["middle", "newest"]);
});

test("regions without a usable rectangle are ignored", () => {
  const result = merge([], [
    candidate({ documentRect: null }),
    candidate({ documentRect: rect(0, 0, 0, 40) }),
    candidate({ documentRect: rect(0, 0, 10, 10), text: "kept" })
  ]);

  assert.deepEqual(result.entries.map((entry) => entry.text), ["kept"]);
});

test("merging does not mutate the entries it was given", () => {
  const first = merge([], [candidate({ documentRect: rect(0, 0, 100, 40), text: "hello" })]);
  const snapshot = structuredClone(first.entries);
  merge(first.entries, [candidate({ documentRect: rect(0, 0, 100, 40), text: "hello there" })], { now: 5_000 });

  assert.deepEqual(first.entries, snapshot);
});

test("a document anchored entry keeps working without an element", () => {
  const result = merge([], [
    candidate({ anchor: documentAnchor(rect(10, 20, 30, 40)), documentRect: rect(10, 20, 30, 40) })
  ]);

  assert.equal(result.entries[0].anchor.kind, "document");
});
