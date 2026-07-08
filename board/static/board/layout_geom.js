// Pure layout geometry / state helpers for the floating-panel UI (#366).
//
// These functions have NO DOM and NO I/O: they take plain geometry objects
// ({x,y,w,h}), bounds, and saved-layout blobs, and return new plain objects.
// They were extracted verbatim from board.js so they can be unit-tested directly
// under `node --test` (see js-tests/layout_geom.test.js) instead of only through a
// live-server Playwright run. board.js imports them; the DOM-touching wrappers
// (applyGeom / measureContent / getInlineGeom / fitPanel) stay in board.js.

export const LAYOUT_SNAP_PX = 12;      // snap when an edge is within this many px (#325)
export const LAYOUT_MIN_VISIBLE = 48;  // px of a panel that must stay grabbable
export const LAYOUT_MODES = new Set(["content", "manual", "maximized", "minimized"]);

// A panel's out-of-the-box sizing mode: "content" (auto-fit to content) unless the
// registry pins it to a fixed slot (the split tracker/action, #323).
export const defaultModeFor = panel => panel.defaultMode || "content";

export const numberOr = (value, fallback) =>
  (typeof value === "number" && isFinite(value)) ? value : fallback;

// Clamp a panel so a grabbable strip of its titlebar always stays on-screen: it
// can never be pushed above the wrap top, nor slid so far that < MIN_VISIBLE px
// remain horizontally. This is what guarantees a panel can never be lost.
export function clampGeom(geom, bounds) {
  const minX = LAYOUT_MIN_VISIBLE - geom.w;
  const maxX = bounds.width - LAYOUT_MIN_VISIBLE;
  const maxY = bounds.height - LAYOUT_MIN_VISIBLE;
  return {
    x: Math.max(minX, Math.min(geom.x, maxX)),
    y: Math.max(0, Math.min(geom.y, maxY)),
    w: geom.w, h: geom.h,
  };
}

// Per-field merge of a persisted layout over the measured defaults: a missing,
// non-numeric, or corrupt field falls back to its default, so partial/garbage
// saved data can never strand a panel.
export function sanitizeRestore(restore) {
  // A persisted restore target is {geom:{x,y,w,h}, mode}; anything malformed drops
  // to null so a corrupt field can never strand a panel on Expand/Restore.
  if (!restore || typeof restore !== "object") return null;
  const geom = restore.geom;
  if (!geom || typeof geom !== "object") return null;
  if (!["x", "y", "w", "h"].every(k => typeof geom[k] === "number" && isFinite(geom[k]))) return null;
  const mode = LAYOUT_MODES.has(restore.mode) ? restore.mode : "content";
  return {geom: {x: geom.x, y: geom.y, w: geom.w, h: geom.h}, mode};
}

// Merge a persisted layout over the measured defaults for each registered panel.
// `panels` is the panel registry ([{key, defaultMode?}, …]); passed in rather than
// read from a module global so this stays pure and unit-testable.
export function mergeLayout(defaults, saved, panels) {
  const merged = {};
  for (const panel of panels) {
    const base = defaults[panel.key];
    const over = (saved && saved[panel.key]) || {};
    merged[panel.key] = {
      x: numberOr(over.x, base.x), y: numberOr(over.y, base.y),
      w: numberOr(over.w, base.w), h: numberOr(over.h, base.h),
      mode: LAYOUT_MODES.has(over.mode) ? over.mode : defaultModeFor(panel),
      restoreGeom: sanitizeRestore(over.restoreGeom),
    };
  }
  return merged;
}

// The smallest shift (within the snap threshold) that lands one of `edges` onto
// one of the candidate `lines`; 0 when nothing is close enough.
export function nearestSnapDelta(edges, lines) {
  let bestDelta = 0;
  let bestDistance = LAYOUT_SNAP_PX + 1;
  for (const line of lines) {
    for (const edge of edges) {
      const distance = Math.abs(edge - line);
      if (distance < bestDistance) { bestDistance = distance; bestDelta = line - edge; }
    }
  }
  return bestDistance <= LAYOUT_SNAP_PX ? bestDelta : 0;
}

// The candidate snap lines a panel may align to: the viewport edges plus every
// other panel's four edges (#367). ONE builder so move-snap (snapGeom) and
// resize-snap (snapResizeGeom) can never diverge on what counts as a guide.
export function snapLines(others, bounds) {
  const xLines = [0, bounds.width];
  const yLines = [0, bounds.height];
  for (const other of others) {
    xLines.push(other.x, other.x + other.w);
    yLines.push(other.y, other.y + other.h);
  }
  return {xLines, yLines};
}

// Soft snap-assist: independently on each axis, nudge the dragged panel so its
// leading/trailing edge aligns with a viewport edge or another panel's edge when
// within the threshold. The user can still position/overlap freely away from an edge.
export function snapGeom(geom, others, bounds) {
  const {xLines, yLines} = snapLines(others, bounds);
  const dx = nearestSnapDelta([geom.x, geom.x + geom.w], xLines);
  const dy = nearestSnapDelta([geom.y, geom.y + geom.h], yLines);
  return {x: geom.x + dx, y: geom.y + dy, w: geom.w, h: geom.h};
}

// Cap a measured natural content size to what fits in the wrap from the panel's
// current top-left, keeping x/y put and never dropping below the grabbable floor.
export function fitGeom(current, natural, bounds, minW, minH) {
  const availW = Math.max(minW, bounds.width - current.x);
  const availH = Math.max(minH, bounds.height - current.y);
  return {
    x: current.x, y: current.y,
    w: Math.max(minW, Math.min(natural.w, availW)),
    h: Math.max(minH, Math.min(natural.h, availH)),
  };
}

// The geometry that fills the whole available wrap area (used by Maximize).
export function maximizeGeom(bounds) {
  return {x: 0, y: 0, w: bounds.width, h: bounds.height};
}

// New geometry for a drag on the `dir` edge/corner: only the pulled edges move,
// each clamped so the opposite edge stays put and the box keeps its min size and
// stays within the wrap. Pure so the resize maths is unit-reasoned and e2e-testable.
export function resizeGeom(start, dir, dx, dy, minW, minH, bounds) {
  let {x, y, w, h} = start;
  if (dir.includes("e")) w = Math.min(Math.max(minW, start.w + dx), bounds.width - start.x);
  if (dir.includes("s")) h = Math.min(Math.max(minH, start.h + dy), bounds.height - start.y);
  if (dir.includes("w")) {
    const right = start.x + start.w;
    x = Math.max(0, Math.min(start.x + dx, right - minW));
    w = right - x;
  }
  if (dir.includes("n")) {
    const bottom = start.y + start.h;
    y = Math.max(0, Math.min(start.y + dy, bottom - minH));
    h = bottom - y;
  }
  return {x, y, w, h};
}

// Snap only the edges the drag is moving to nearby viewport / other-panel lines,
// reusing the move-snap threshold and candidate-line logic (#367: shared snapLines).
export function snapResizeGeom(geom, dir, others, bounds) {
  const {xLines, yLines} = snapLines(others, bounds);
  const out = {...geom};
  if (dir.includes("e")) out.w += nearestSnapDelta([out.x + out.w], xLines);
  if (dir.includes("s")) out.h += nearestSnapDelta([out.y + out.h], yLines);
  if (dir.includes("w")) { const d = nearestSnapDelta([out.x], xLines); out.x += d; out.w -= d; }
  if (dir.includes("n")) { const d = nearestSnapDelta([out.y], yLines); out.y += d; out.h -= d; }
  return out;
}
