(() => {
  // Geometry and bookkeeping for the persistent translation store.
  //
  // Everything here is deliberately a pure function over plain objects: the
  // overlay lifetime rules (which bubble is still the same bubble, which strip
  // of the page still needs OCR) are the part of this extension most likely to
  // regress silently, and this file is what `extension/tests` exercises without
  // a browser.
  //
  // Coordinate systems used below:
  //   document rect  - { left, top, width, height } in CSS pixels from the
  //                    document origin. Stable across scrolling.
  //   element anchor - fractions of an anchor element's border box, so an
  //                    overlay follows its panel when lazy loading reflows the
  //                    page.
  //   capture region - { x, y, width, height } normalized to the viewport, the
  //                    shape `background.js` already crops screenshots with.

  const DEFAULT_IOU_THRESHOLD = 0.45;
  const DEFAULT_TEXT_PROXIMITY_RATIO = 0.1;
  const DEFAULT_MAX_ENTRIES = 600;
  // How many times a capture may cover a stored bubble without finding it again
  // before it is treated as gone. OCR occasionally misses a bubble it read a
  // moment ago, so one miss is not enough to remove a translation.
  const DEFAULT_MAX_MISSES = 2;
  // Roughly one speech bubble tall. A bubble sitting across the seam between
  // the already-scanned area and a freshly revealed strip must appear complete
  // in the strip, otherwise OCR reads half a sentence.
  const DEFAULT_BAND_OVERLAP_PX = 130;
  // Below this the newly revealed strip cannot hold readable lettering. Leave
  // it unscanned and let it accumulate instead of paying for a whole capture.
  const DEFAULT_MIN_BAND_PX = 48;
  // Past this fraction of the scanned region a strip is no cheaper than simply
  // rescanning the viewport, and a full scan resynchronizes any drift.
  const DEFAULT_MAX_BAND_RATIO = 0.72;
  // Bounds how many separate read ranges are remembered for one episode.
  const MAX_COVERAGE_RANGES = 24;

  let idCounter = 0;

  function isFiniteNumber(value) {
    return Number.isFinite(Number(value));
  }

  function finiteRect(value) {
    if (!value) return null;
    const left = Number(value.left);
    const top = Number(value.top);
    const width = Number(value.width);
    const height = Number(value.height);
    if (![left, top, width, height].every(isFiniteNumber)) return null;
    if (width <= 0 || height <= 0) return null;
    return { left, top, width, height };
  }

  function rectArea(rect) {
    return rect ? Math.max(0, rect.width) * Math.max(0, rect.height) : 0;
  }

  function rectIntersectionArea(a, b) {
    if (!a || !b) return 0;
    const left = Math.max(a.left, b.left);
    const top = Math.max(a.top, b.top);
    const right = Math.min(a.left + a.width, b.left + b.width);
    const bottom = Math.min(a.top + a.height, b.top + b.height);
    if (right <= left || bottom <= top) return 0;
    return (right - left) * (bottom - top);
  }

  function rectIoU(a, b) {
    const first = finiteRect(a);
    const second = finiteRect(b);
    if (!first || !second) return 0;
    const intersection = rectIntersectionArea(first, second);
    if (intersection <= 0) return 0;
    const union = rectArea(first) + rectArea(second) - intersection;
    return union > 0 ? intersection / union : 0;
  }

  function rectsIntersect(a, b, margin = 0) {
    const first = finiteRect(a);
    const second = finiteRect(b);
    if (!first || !second) return false;
    return (
      first.left + first.width > second.left - margin
      && first.left < second.left + second.width + margin
      && first.top + first.height > second.top - margin
      && first.top < second.top + second.height + margin
    );
  }

  function rectCenterDistance(a, b) {
    const first = finiteRect(a);
    const second = finiteRect(b);
    if (!first || !second) return Infinity;
    const dx = first.left + first.width / 2 - (second.left + second.width / 2);
    const dy = first.top + first.height / 2 - (second.top + second.height / 2);
    return Math.hypot(dx, dy);
  }

  // OCR re-reads the same lettering slightly differently between captures.
  // Compare on a form that ignores width/compatibility variants, casing, and
  // the line breaks that grouping introduces.
  function normalizeRegionText(value) {
    return String(value ?? "")
      .normalize("NFKC")
      .replace(/\s+/g, " ")
      .trim()
      .toLowerCase();
  }

  function anchorRelRect(anchor) {
    if (!anchor || anchor.kind !== "element") return null;
    return finiteRect({
      left: anchor.relX,
      top: anchor.relY,
      width: anchor.relW,
      height: anchor.relH
    });
  }

  function readElementRect(elementRects, key) {
    if (!elementRects || key == null) return null;
    if (typeof elementRects.get === "function") return finiteRect(elementRects.get(key));
    return finiteRect(elementRects[key]);
  }

  // Turn an anchor back into a document rect for the current layout. An element
  // anchor that cannot be resolved means its panel is not in the document right
  // now, so the caller should stop drawing that overlay rather than fall back to
  // a document position that no longer describes anything.
  function resolveAnchorRect(anchor, elementRects) {
    if (!anchor) return null;
    if (anchor.kind === "element") {
      const base = readElementRect(elementRects, anchor.key);
      const relative = anchorRelRect(anchor);
      if (!base || !relative) return null;
      return {
        left: base.left + relative.left * base.width,
        top: base.top + relative.top * base.height,
        width: relative.width * base.width,
        height: relative.height * base.height
      };
    }
    return finiteRect(anchor);
  }

  function documentAnchor(rect) {
    const value = finiteRect(rect);
    return value ? { kind: "document", ...value } : null;
  }

  function elementAnchor(key, elementRect, regionRect) {
    const base = finiteRect(elementRect);
    const region = finiteRect(regionRect);
    if (!base || !region || key == null) return null;
    return {
      kind: "element",
      key: String(key),
      relX: (region.left - base.left) / base.width,
      relY: (region.top - base.top) / base.height,
      relW: region.width / base.width,
      relH: region.height / base.height
    };
  }

  function nextEntryId() {
    idCounter += 1;
    return `wtr-${Date.now().toString(36)}-${idCounter.toString(36)}`;
  }

  // Decide whether an incoming OCR region describes a bubble the store already
  // knows. Two independent signals are used because neither alone is enough:
  // the element/IoU test survives OCR text jitter, and the text test survives a
  // bubble being detected with a noticeably different box between captures.
  function findMatchingEntry(entries, candidate, options = {}) {
    const iouThreshold = Number.isFinite(options.iouThreshold) ? options.iouThreshold : DEFAULT_IOU_THRESHOLD;
    const textProximity = Number.isFinite(options.textProximity)
      ? options.textProximity
      : DEFAULT_TEXT_PROXIMITY_RATIO * 800;
    const skip = options.skip instanceof Set ? options.skip : new Set();

    const candidateRect = finiteRect(candidate?.documentRect);
    const candidateRel = anchorRelRect(candidate?.anchor);
    const candidateKey = candidate?.anchor?.kind === "element" ? candidate.anchor.key : null;
    const candidateText = candidate?.normalizedText ?? normalizeRegionText(candidate?.text);

    let best = null;
    let bestScore = 0;

    for (const entry of Array.isArray(entries) ? entries : []) {
      if (!entry || skip.has(entry.id)) continue;

      const entryKey = entry.anchor?.kind === "element" ? entry.anchor.key : null;
      const entryRect = finiteRect(entry.documentRect);
      const entryText = entry.normalizedText ?? normalizeRegionText(entry.text);

      let score = 0;
      if (candidateKey != null && entryKey === candidateKey) {
        score = rectIoU(candidateRel, anchorRelRect(entry.anchor));
      } else if (candidateKey == null && entryKey == null) {
        score = rectIoU(candidateRect, entryRect);
      }

      if (score < iouThreshold) {
        // A bubble can be re-detected with a quite different box after grouping
        // changes. Identical text nearby is then the stronger signal.
        const sameText = Boolean(candidateText) && candidateText === entryText;
        if (!sameText || rectCenterDistance(candidateRect, entryRect) > textProximity) continue;
        score = Math.max(score, iouThreshold);
      }

      if (score > bestScore) {
        best = entry;
        bestScore = score;
      }
    }

    return best;
  }

  function makeEntry(candidate, now, makeId) {
    const text = String(candidate?.text ?? "");
    return {
      id: makeId(),
      anchor: candidate?.anchor || null,
      documentRect: finiteRect(candidate?.documentRect),
      text,
      normalizedText: normalizeRegionText(text),
      translation: String(candidate?.translation ?? ""),
      confidence: Number(candidate?.confidence) || 0,
      sourceLanguage: String(candidate?.sourceLanguage ?? ""),
      targetLanguage: String(candidate?.targetLanguage ?? ""),
      provider: String(candidate?.provider ?? ""),
      createdAt: now,
      lastSeenAt: now
    };
  }

  function updatedEntry(entry, candidate, now) {
    const text = String(candidate?.text ?? "") || entry.text;
    return {
      ...entry,
      anchor: candidate?.anchor || entry.anchor,
      documentRect: finiteRect(candidate?.documentRect) || entry.documentRect,
      text,
      normalizedText: normalizeRegionText(text),
      // A seam re-detection can arrive without a translation when the server
      // filtered it out. Keeping the previous translation is what makes a
      // scrolled-back bubble reappear instantly instead of blinking.
      translation: String(candidate?.translation ?? "") || entry.translation,
      confidence: Number.isFinite(Number(candidate?.confidence)) ? Number(candidate.confidence) : entry.confidence,
      sourceLanguage: String(candidate?.sourceLanguage ?? "") || entry.sourceLanguage,
      targetLanguage: String(candidate?.targetLanguage ?? "") || entry.targetLanguage,
      provider: String(candidate?.provider ?? "") || entry.provider,
      lastSeenAt: now
    };
  }

  // Fold a capture's regions into the store without disturbing bubbles the
  // capture did not cover. Never mutates the input array.
  //
  // `coveredRect` is the document area this capture actually read. An entry
  // inside it that the capture did not find again is a bubble that is no longer
  // there - OCR noise from an earlier pass, or page that has since changed - and
  // is dropped after `maxMisses` such passes. Without this a spurious patch,
  // once stored, would stay on screen forever.
  function mergeRegions(entries, incoming, options = {}) {
    const now = Number.isFinite(options.now) ? options.now : Date.now();
    const makeId = typeof options.makeId === "function" ? options.makeId : nextEntryId;
    const maxEntries = Number.isFinite(options.maxEntries) ? options.maxEntries : DEFAULT_MAX_ENTRIES;
    const maxMisses = Number.isFinite(options.maxMisses) ? options.maxMisses : DEFAULT_MAX_MISSES;
    const coveredRect = finiteRect(options.coveredRect);
    const working = (Array.isArray(entries) ? entries : []).filter(Boolean).slice();
    const consumed = new Set();
    let added = 0;
    let updated = 0;

    for (const candidate of Array.isArray(incoming) ? incoming : []) {
      if (!candidate) continue;
      const rect = finiteRect(candidate.documentRect);
      if (!rect) continue;

      const match = findMatchingEntry(working, { ...candidate, documentRect: rect }, { ...options, skip: consumed });
      if (match) {
        // One capture must not fold two distinct bubbles into a single entry.
        consumed.add(match.id);
        working[working.indexOf(match)] = updatedEntry(match, candidate, now);
        updated += 1;
        continue;
      }

      const entry = makeEntry({ ...candidate, documentRect: rect }, now, makeId);
      consumed.add(entry.id);
      working.push(entry);
      added += 1;
    }

    let dropped = 0;
    if (coveredRect) {
      for (let index = working.length - 1; index >= 0; index -= 1) {
        const entry = working[index];
        if (consumed.has(entry.id)) {
          if (entry.misses) working[index] = { ...entry, misses: 0 };
          continue;
        }
        // Only an entry the capture could actually have seen counts as missing.
        const rect = finiteRect(entry.documentRect);
        if (!rect || rectIntersectionArea(rect, coveredRect) < rectArea(rect) * 0.9) continue;

        const misses = (Number(entry.misses) || 0) + 1;
        if (misses > maxMisses) {
          working.splice(index, 1);
          dropped += 1;
        } else {
          working[index] = { ...entry, misses };
        }
      }
    }

    if (working.length > maxEntries) {
      working.sort((a, b) => a.lastSeenAt - b.lastSeenAt);
      working.splice(0, working.length - maxEntries);
    }

    return { entries: working, added, updated, dropped };
  }

  function normalizedCaptureRegion(value) {
    if (!value) return null;
    const x = Number(value.x);
    const y = Number(value.y);
    const width = Number(value.width);
    const height = Number(value.height);
    if (![x, y, width, height].every(isFiniteNumber) || width <= 0 || height <= 0) return null;
    const left = Math.min(Math.max(x, 0), 1);
    const top = Math.min(Math.max(y, 0), 1);
    const right = Math.min(Math.max(x + width, 0), 1);
    const bottom = Math.min(Math.max(y + height, 0), 1);
    if (right <= left || bottom <= top) return null;
    return { x: left, y: top, width: right - left, height: bottom - top };
  }

  // ---------------------------------------------------------------------------
  // Coverage: which document ranges have already been read.
  //
  // A single "last band" is not enough. A reader who scrolls down a few screens
  // and then back up is returning to page that was read minutes ago, and should
  // see it repaint instantly rather than wait for another OCR pass.
  // ---------------------------------------------------------------------------

  function normalizeRange(value) {
    const top = Number(value?.top);
    const bottom = Number(value?.bottom);
    if (![top, bottom].every(isFiniteNumber) || bottom <= top) return null;
    return { top, bottom };
  }

  function addCoverage(ranges, band, options = {}) {
    const added = normalizeRange(band);
    const existing = (Array.isArray(ranges) ? ranges : []).map(normalizeRange).filter(Boolean);
    if (!added) return existing;
    const maxRanges = Number.isFinite(options.maxRanges) ? options.maxRanges : MAX_COVERAGE_RANGES;

    const merged = [];
    let current = { ...added };
    for (const range of [...existing, added].sort((a, b) => a.top - b.top)) {
      if (!merged.length) {
        merged.push({ ...range });
        continue;
      }
      const last = merged[merged.length - 1];
      // Touching ranges are joined: there is no unread page between them.
      if (range.top <= last.bottom) {
        last.bottom = Math.max(last.bottom, range.bottom);
      } else {
        merged.push({ ...range });
      }
    }
    current = merged.find((range) => range.top <= added.top && range.bottom >= added.bottom) || added;

    if (merged.length > maxRanges) {
      // Keep the ranges nearest what is being read now; distant ones are the
      // least likely to be revisited and the most likely to have reflowed.
      const distance = (range) => Math.max(0, Math.max(current.top - range.bottom, range.top - current.bottom));
      merged.sort((a, b) => distance(a) - distance(b));
      merged.length = maxRanges;
      merged.sort((a, b) => a.top - b.top);
    }
    return merged;
  }

  // The smallest interval inside [top, bottom] that contains every unread point,
  // or null when the whole span has already been read.
  function uncoveredSpan(ranges, top, bottom) {
    const span = normalizeRange({ top, bottom });
    if (!span) return null;
    const sorted = (Array.isArray(ranges) ? ranges : [])
      .map(normalizeRange)
      .filter(Boolean)
      .sort((a, b) => a.top - b.top);

    let first = null;
    let last = null;
    let cursor = span.top;
    for (const range of sorted) {
      if (range.bottom <= cursor) continue;
      if (range.top >= span.bottom) break;
      if (range.top > cursor) {
        if (first === null) first = cursor;
        last = Math.min(range.top, span.bottom);
      }
      cursor = Math.max(cursor, range.bottom);
      if (cursor >= span.bottom) break;
    }
    if (cursor < span.bottom) {
      if (first === null) first = cursor;
      last = span.bottom;
    }
    return first === null || last === null || last <= first ? null : { top: first, bottom: last };
  }

  // Hold back the part of a band where a bubble was cut off by the capture edge.
  //
  // Without this the reader would lose that bubble entirely: the capture claims
  // the range as read, the server refused to translate a half-visible bubble,
  // and the next scroll would then skip the area as already done.
  function trimBandForClipped(band, clipped) {
    const range = normalizeRange(band);
    if (!range) return null;
    let { top, bottom } = range;

    for (const region of Array.isArray(clipped) ? clipped : []) {
      const rect = finiteRect(region);
      if (!rect) continue;
      const edges = Array.isArray(region.edges) ? region.edges : [];
      const rectTop = rect.top;
      const rectBottom = rect.top + rect.height;
      // The bubble continues past the bottom of the capture, so nothing from
      // this region downward may be treated as read.
      if (edges.includes("bottom")) bottom = Math.min(bottom, rectTop);
      if (edges.includes("top")) top = Math.max(top, rectBottom);
    }
    return bottom > top ? { top, bottom } : null;
  }

  // Work out how much of the page still needs OCR.
  //
  // Returns one of:
  //   full - read the whole selected region (first scan, resize, jump to a
  //          completely unread part of the episode)
  //   band - read only the unread strip; OCR cost falls roughly with its height
  //   none - every pixel on screen has already been read, so the store alone
  //          repaints it with no capture and no server round trip
  function incrementalBand(options = {}) {
    const viewportHeight = Math.max(1, Number(options.viewport?.height) || 0);
    const pageTop = Number(options.viewport?.pageTop) || 0;
    const requested = options.captureRegion ?? null;
    const base = normalizedCaptureRegion(requested) || { x: 0, y: 0, width: 1, height: 1 };
    const overlapPx = Number.isFinite(options.overlapPx) ? options.overlapPx : DEFAULT_BAND_OVERLAP_PX;
    const minBandPx = Number.isFinite(options.minBandPx) ? options.minBandPx : DEFAULT_MIN_BAND_PX;
    const maxBandRatio = Number.isFinite(options.maxBandRatio) ? options.maxBandRatio : DEFAULT_MAX_BAND_RATIO;
    const sameContext = options.sameContext !== false;

    const regionTop = pageTop + base.y * viewportHeight;
    const regionHeight = base.height * viewportHeight;
    const regionBottom = regionTop + regionHeight;
    const currentBand = { top: regionTop, bottom: regionBottom };
    const coverage = sameContext ? (Array.isArray(options.coverage) ? options.coverage : []) : [];
    // `band` is what this capture would read. The caller narrows it before
    // committing it to coverage, because a bubble clipped by the capture edge
    // must stay unread.
    const fullScan = {
      mode: "full",
      captureRegion: requested,
      band: currentBand,
      coverage: addCoverage(coverage, currentBand, options)
    };
    if (!coverage.length) return fullScan;

    const unread = uncoveredSpan(coverage, regionTop, regionBottom);
    if (!unread) return { mode: "none", captureRegion: requested, band: null, coverage };

    // Judge "is this worth a capture?" on the unread page alone. The seam
    // overlap below only exists so a bubble crossing the boundary is captured
    // whole; counting it would make every scroll nudge look scannable. Leaving
    // coverage untouched lets successive nudges accumulate into a real strip.
    if (unread.bottom - unread.top < minBandPx) {
      return { mode: "none", captureRegion: requested, band: null, coverage };
    }

    const strip = {
      top: Math.max(regionTop, unread.top - overlapPx),
      bottom: Math.min(regionBottom, unread.bottom + overlapPx)
    };
    const stripHeight = strip.bottom - strip.top;
    if (stripHeight >= regionHeight * maxBandRatio) return fullScan;

    return {
      mode: "band",
      captureRegion: {
        x: base.x,
        y: Math.min(Math.max((strip.top - pageTop) / viewportHeight, 0), 1),
        width: base.width,
        height: Math.min(Math.max(stripHeight / viewportHeight, 0), 1)
      },
      band: currentBand,
      coverage: addCoverage(coverage, currentBand, options)
    };
  }

  globalThis.WebtoonRegions = {
    DEFAULT_IOU_THRESHOLD,
    DEFAULT_TEXT_PROXIMITY_RATIO,
    DEFAULT_MAX_ENTRIES,
    DEFAULT_MAX_MISSES,
    DEFAULT_BAND_OVERLAP_PX,
    DEFAULT_MIN_BAND_PX,
    DEFAULT_MAX_BAND_RATIO,
    MAX_COVERAGE_RANGES,
    finiteRect,
    rectIoU,
    rectsIntersect,
    rectCenterDistance,
    normalizeRegionText,
    anchorRelRect,
    resolveAnchorRect,
    documentAnchor,
    elementAnchor,
    findMatchingEntry,
    mergeRegions,
    normalizedCaptureRegion,
    addCoverage,
    uncoveredSpan,
    trimBandForClipped,
    incrementalBand
  };
})();
