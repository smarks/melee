// Browser-free unit tests for the pure layout helpers (#366/#367). These assert the
// clamp/snap/merge/resize maths directly — the boundary cases that previously needed
// a live_server + Playwright pointer script to reach (#335 restore-clobber, #338
// stacked-measure, #343 maximize-refill), now three-line assertions.
import {test} from "node:test";
import assert from "node:assert/strict";
import {
  LAYOUT_SNAP_PX, LAYOUT_MIN_VISIBLE, defaultModeFor, numberOr,
  clampGeom, sanitizeRestore, mergeLayout, nearestSnapDelta, snapLines,
  snapGeom, fitGeom, maximizeGeom, resizeGeom, snapResizeGeom,
} from "../board/static/board/layout_geom.js";

const BOUNDS = {width: 1000, height: 800};

test("clampGeom keeps a normal panel put", () => {
  assert.deepEqual(clampGeom({x: 100, y: 100, w: 200, h: 150}, BOUNDS),
    {x: 100, y: 100, w: 200, h: 150});
});

test("clampGeom never pushes a panel above the wrap top", () => {
  assert.equal(clampGeom({x: 100, y: -50, w: 200, h: 150}, BOUNDS).y, 0);
});

test("clampGeom leaves MIN_VISIBLE px grabbable at each edge", () => {
  // Slid far right: at least MIN_VISIBLE px stays on-screen.
  assert.equal(clampGeom({x: 5000, y: 0, w: 200, h: 150}, BOUNDS).x,
    BOUNDS.width - LAYOUT_MIN_VISIBLE);
  // A panel WIDER than the wrap: minX = MIN_VISIBLE - w goes negative, so its left
  // edge may sit off-screen while a strip of its right stays grabbable.
  const wide = clampGeom({x: -9999, y: 0, w: 1200, h: 150}, BOUNDS);
  assert.equal(wide.x, LAYOUT_MIN_VISIBLE - 1200);
  assert.ok(wide.x + wide.w >= LAYOUT_MIN_VISIBLE);
});

test("clampGeom passes width/height through untouched", () => {
  const out = clampGeom({x: 5000, y: 5000, w: 321, h: 234}, BOUNDS);
  assert.equal(out.w, 321);
  assert.equal(out.h, 234);
});

test("numberOr falls back on non-finite / non-number", () => {
  assert.equal(numberOr(42, 7), 42);
  assert.equal(numberOr(0, 7), 0);            // 0 is a valid number, not a fallback
  assert.equal(numberOr(NaN, 7), 7);
  assert.equal(numberOr(Infinity, 7), 7);
  assert.equal(numberOr("50", 7), 7);
  assert.equal(numberOr(undefined, 7), 7);
});

test("defaultModeFor honours a pinned mode, else content", () => {
  assert.equal(defaultModeFor({key: "map"}), "content");
  assert.equal(defaultModeFor({key: "action", defaultMode: "manual"}), "manual");
});

test("sanitizeRestore drops malformed blobs to null", () => {
  assert.equal(sanitizeRestore(null), null);
  assert.equal(sanitizeRestore("nope"), null);
  assert.equal(sanitizeRestore({}), null);                       // no geom
  assert.equal(sanitizeRestore({geom: {x: 1, y: 2, w: 3}}), null); // missing h
  assert.equal(sanitizeRestore({geom: {x: 1, y: 2, w: 3, h: NaN}}), null);
});

test("sanitizeRestore keeps a valid blob and normalises the mode", () => {
  assert.deepEqual(
    sanitizeRestore({geom: {x: 1, y: 2, w: 3, h: 4}, mode: "maximized"}),
    {geom: {x: 1, y: 2, w: 3, h: 4}, mode: "maximized"});
  // An unknown mode falls back to "content".
  assert.equal(
    sanitizeRestore({geom: {x: 1, y: 2, w: 3, h: 4}, mode: "bogus"}).mode, "content");
});

const PANELS = [{key: "map"}, {key: "action", defaultMode: "manual"}];
const DEFAULTS = {
  map: {x: 0, y: 0, w: 100, h: 100},
  action: {x: 10, y: 20, w: 30, h: 40},
};

test("mergeLayout returns defaults when nothing is saved", () => {
  const merged = mergeLayout(DEFAULTS, null, PANELS);
  assert.deepEqual(merged.map,
    {x: 0, y: 0, w: 100, h: 100, mode: "content", restoreGeom: null});
  assert.equal(merged.action.mode, "manual");   // pinned default mode
});

test("mergeLayout overlays a partial/corrupt saved blob field-by-field", () => {
  const saved = {map: {x: 250, w: "garbage", mode: "minimized"}};
  const merged = mergeLayout(DEFAULTS, saved, PANELS);
  assert.equal(merged.map.x, 250);      // valid override wins
  assert.equal(merged.map.w, 100);      // corrupt field falls back to default
  assert.equal(merged.map.y, 0);        // missing field falls back to default
  assert.equal(merged.map.mode, "minimized");
});

test("nearestSnapDelta snaps within threshold and tie-breaks to the first-seen line", () => {
  // edge at 5, line at 0 -> delta -5 (within 12).
  assert.equal(nearestSnapDelta([5], [0, 100]), -5);
  // Nothing close enough -> 0.
  assert.equal(nearestSnapDelta([500], [0, 100]), 0);
  // Exactly at the threshold still snaps.
  assert.equal(nearestSnapDelta([LAYOUT_SNAP_PX], [0]), -LAYOUT_SNAP_PX);
  assert.equal(nearestSnapDelta([LAYOUT_SNAP_PX + 1], [0]), 0);
});

test("snapLines builds viewport edges + every other panel's four edges", () => {
  const others = [{x: 200, y: 300, w: 50, h: 60}];
  const {xLines, yLines} = snapLines(others, BOUNDS);
  assert.deepEqual(xLines, [0, 1000, 200, 250]);
  assert.deepEqual(yLines, [0, 800, 300, 360]);
});

test("snapGeom nudges a near-edge panel onto the viewport edge", () => {
  // left edge at 4 -> snaps to 0.
  assert.deepEqual(snapGeom({x: 4, y: 400, w: 200, h: 100}, [], BOUNDS),
    {x: 0, y: 400, w: 200, h: 100});
});

test("snapGeom aligns to another panel's edge", () => {
  const others = [{x: 300, y: 0, w: 100, h: 100}];
  // our right edge at 402 -> nudged to another panel's left edge 300? no: nearest is
  // our left (398) to 400? Take a case where the trailing edge is 3px from x=300.
  const out = snapGeom({x: 100, y: 500, w: 197, h: 50}, others, BOUNDS);
  assert.equal(out.x, 103);   // right edge 100+197=297 -> snaps to 300 (delta +3)
});

test("fitGeom caps natural size to what fits from the current top-left", () => {
  const out = fitGeom({x: 900, y: 700, w: 0, h: 0}, {w: 500, h: 500}, BOUNDS, 96, 32);
  assert.equal(out.x, 900);
  assert.equal(out.w, 100);   // only 100px to the right edge
  assert.equal(out.h, 100);   // only 100px to the bottom
});

test("fitGeom never drops below the min floor", () => {
  const out = fitGeom({x: 990, y: 790, w: 0, h: 0}, {w: 10, h: 10}, BOUNDS, 96, 32);
  assert.equal(out.w, 96);
  assert.equal(out.h, 32);
});

test("maximizeGeom fills the wrap", () => {
  assert.deepEqual(maximizeGeom(BOUNDS), {x: 0, y: 0, w: 1000, h: 800});
});

test("resizeGeom moves only the pulled edge (east)", () => {
  assert.deepEqual(resizeGeom({x: 100, y: 100, w: 200, h: 150}, "e", 50, 0, 96, 32, BOUNDS),
    {x: 100, y: 100, w: 250, h: 150});
});

test("resizeGeom clamps the west edge so it can't cross the opposite edge", () => {
  // Pull the west edge far right; it stops minW short of the fixed right edge (300).
  const out = resizeGeom({x: 100, y: 100, w: 200, h: 150}, "w", 500, 0, 96, 32, BOUNDS);
  assert.equal(out.x, 300 - 96);   // right edge (300) minus minW
  assert.equal(out.w, 96);
});

test("resizeGeom keeps a resized box inside the wrap", () => {
  const out = resizeGeom({x: 900, y: 100, w: 50, h: 50}, "e", 1000, 0, 96, 32, BOUNDS);
  assert.equal(out.w, BOUNDS.width - 900);   // can't extend past the right wall
});

test("snapResizeGeom snaps only the moving edge and shares snapLines with move-snap", () => {
  // East resize whose right edge lands 3px shy of the viewport edge snaps out to it.
  const out = snapResizeGeom({x: 100, y: 100, w: 897, h: 100}, "e", [], BOUNDS);
  assert.equal(out.w, 900);   // right edge 997 -> 1000
  assert.equal(out.x, 100);   // west edge untouched on an east drag
});

test("snapResizeGeom west drag moves x and w together", () => {
  const out = snapResizeGeom({x: 5, y: 100, w: 200, h: 100}, "w", [], BOUNDS);
  assert.equal(out.x, 0);     // left edge 5 -> 0
  assert.equal(out.w, 205);   // width grows by the same 5
});
