// Browser-free unit tests for the turn-flow classifier (#364). These assert the
// player-gating decisions that previously needed a live server and, for the cross-
// client cases, a SECOND browser context to reach (#326 player-specific panel, #333
// a computer/unclaimed seat must NOT hide Resolve, #347 an admin is not the AI's
// actor) — now plain-object assertions with no DOM.
import {test} from "node:test";
import assert from "node:assert/strict";
import {classifyControlState, needsTarget} from "../board/static/board/control_state.js";

// Build a ctx whose myTurnActor is true for figures on `mySides`, and whose
// isComputerSide is true for `computerSides`. plan/chosenOption/sel/openSeats/
// isHost default to the "nothing queued, not placing, no open seats, not the
// host" case.
function ctxFor({mySides = [], computerSides = [], plan = {}, chosenOption = null,
                 sel = null, openSeats = [], isHost = false} = {}) {
  return {
    myTurnActor: f => mySides.includes(f.side),
    isComputerSide: side => computerSides.includes(side),
    plan, chosenOption, sel, openSeats, isHost,
  };
}
const fig = (uid, side, extra = {}) => ({uid, side, name: uid, label: uid, ...extra});

test("victory short-circuits regardless of phase", () => {
  const out = classifyControlState({phase: "combat", victory: "red"}, ctxFor());
  assert.equal(out.kind, "victory");
});

test("select with no active figure is the resolving state", () => {
  const out = classifyControlState({phase: "select", active_uid: null, figures: []}, ctxFor());
  assert.equal(out.kind, "select_resolving");
});

test("select: another human's turn is a NAMED waiting state (#326)", () => {
  const state = {phase: "select", active_uid: "b1", figures: [fig("b1", "blue")]};
  const out = classifyControlState(state, ctxFor({mySides: ["red"]}));
  assert.equal(out.kind, "select_waiting_human");
  assert.equal(out.active.uid, "b1");
});

test("select: an AI turn is the computer state even for an admin (#347)", () => {
  // An admin can edit AI figures but is NOT the AI's actor, so myTurnActor is false
  // for the AI side and the classifier routes to the computer branch, not waiting.
  const state = {phase: "select", active_uid: "c1", figures: [fig("c1", "green")]};
  const out = classifyControlState(state,
    ctxFor({mySides: [], computerSides: ["green"]}));
  assert.equal(out.kind, "select_computer");
});

test("select: my active figure, not mid-placement", () => {
  const state = {phase: "select", active_uid: "r1", figures: [fig("r1", "red")]};
  const out = classifyControlState(state, ctxFor({mySides: ["red"]}));
  assert.equal(out.kind, "select_mine");
  assert.equal(out.placing, false);
});

test("select: my active figure mid-placement sets placing=true", () => {
  const state = {phase: "select", active_uid: "r1", figures: [fig("r1", "red")]};
  const out = classifyControlState(state,
    ctxFor({mySides: ["red"], chosenOption: "move", sel: "r1"}));
  assert.equal(out.kind, "select_mine");
  assert.equal(out.placing, true);
});

test("combat: server-resolved short-circuits to the End-turn state (#334)", () => {
  const state = {phase: "combat", combat_resolved: true,
                 figures: [fig("r1", "red")], combat_actionable: ["r1"]};
  const out = classifyControlState(state, ctxFor({mySides: ["red"]}));
  assert.equal(out.kind, "combat_resolved");
});

test("combat: all my actionable sides queued -> waiting on the other player (#334)", () => {
  const state = {phase: "combat", figures: [fig("r1", "red")],
                 combat_actionable: ["r1"], combat_ready: ["red"]};
  const out = classifyControlState(state, ctxFor({mySides: ["red"]}));
  assert.equal(out.kind, "combat_queued_waiting");
});

test("combat: no actors, a NAMED human other still owes an action -> waiting (#326)", () => {
  const state = {phase: "combat", combat_actionable: ["b1"], combat_ready: [],
                 figures: [fig("b1", "blue")]};
  const out = classifyControlState(state, ctxFor({mySides: ["red"]}));
  assert.equal(out.kind, "combat_waiting_human");
  assert.equal(out.humanOther.uid, "b1");
});

test("combat: a COMPUTER other must NOT hide Resolve — it renders (#333)", () => {
  const state = {phase: "combat", combat_actionable: ["c1"], combat_ready: [],
                 figures: [fig("c1", "green")]};
  const out = classifyControlState(state,
    ctxFor({mySides: ["red"], computerSides: ["green"]}));
  assert.equal(out.kind, "combat_render");   // NOT combat_waiting_human -> game can't brick
});

test("combat: an UNCLAIMED/open-seat other must NOT hide Resolve — it renders (#333)", () => {
  const state = {phase: "combat", combat_actionable: ["b1"], combat_ready: [],
                 figures: [fig("b1", "blue")]};
  const out = classifyControlState(state,
    ctxFor({mySides: ["red"], openSeats: ["blue"]}));
  assert.equal(out.kind, "combat_render");
});

test("combat_render names untargeted must-attack figures and counts idle ones", () => {
  const state = {
    phase: "combat",
    combat_actionable: ["r1", "r2", "r3"],
    must_attack: ["r1", "r2"],
    figures: [fig("r1", "red"), fig("r2", "red"), fig("r3", "red")],
  };
  // r2 already has a PLAN; r1 does not -> only r1 is untargeted. r3 is not
  // must-attack and has no PLAN -> idle.
  const out = classifyControlState(state,
    ctxFor({mySides: ["red"], plan: {r2: {uid: "r2"}}}));
  assert.equal(out.kind, "combat_render");
  assert.deepEqual(out.untargeted.map(f => f.uid), ["r1"]);
  assert.equal(out.idle, 1);
  assert.equal(out.actors.length, 3);
});

test("combat actors require label + mine + in the server actionable set", () => {
  const state = {
    phase: "combat",
    combat_actionable: ["r1", "b1"],
    figures: [fig("r1", "red"), fig("r2", "red", {label: ""}), fig("b1", "blue")],
  };
  const out = classifyControlState(state, ctxFor({mySides: ["red"]}));
  // r2 has no label; b1 is not mine; only r1 qualifies.
  assert.deepEqual(out.actors.map(f => f.uid), ["r1"]);
});

test("needsTarget is the must-attack ∧ no-plan invariant", () => {
  const mustAttack = new Set(["r1", "r2"]);
  assert.equal(needsTarget({uid: "r1"}, mustAttack, {}), true);
  assert.equal(needsTarget({uid: "r1"}, mustAttack, {r1: {}}), false);  // has a plan
  assert.equal(needsTarget({uid: "r3"}, mustAttack, {}), false);        // not must-attack
});

test("combat: a wizard with a queued cast is NOT flagged by the must-attack gate", () => {
  // Gate 2 (TFT: Wizard): a cast is not an attack, so a casting wizard is never in
  // the server's must_attack set — it is never forced to attack. With a queued cast
  // in PLAN it satisfies the resolve gate like any set action: not untargeted, not
  // idle, so Resolve is free. This is the control_state.js half of the #388 guard.
  const state = {
    phase: "combat",
    combat_actionable: ["w1", "r2"],
    must_attack: ["r2"],                 // only the fighter must attack; never the wizard
    figures: [fig("w1", "red", {is_wizard: true}), fig("r2", "red")],
  };
  const out = classifyControlState(state,
    ctxFor({mySides: ["red"], plan: {w1: {uid: "w1", cast: true}, r2: {uid: "r2"}}}));
  assert.equal(out.kind, "combat_render");
  // The wizard's queued cast is not treated as an un-set must-attack...
  assert.equal(out.untargeted.length, 0);
  // ...and a wizard that has queued its cast is not counted idle either.
  assert.equal(out.idle, 0);
  assert.ok(out.actors.some(f => f.uid === "w1"));
});

test("combat: a wizard with NO queued cast is idle, never a resolve-blocking must-attack", () => {
  // Casting classification: a wizard that has set nothing is merely idle (a soft
  // warning), never in must_attack — it does NOT gate Resolve (a wizard is not forced
  // to act). The must-attack fighter r2 with no plan is the only untargeted one.
  const state = {
    phase: "combat",
    combat_actionable: ["w1", "r2"],
    must_attack: ["r2"],
    figures: [fig("w1", "red", {is_wizard: true}), fig("r2", "red")],
  };
  const out = classifyControlState(state, ctxFor({mySides: ["red"], plan: {}}));
  assert.equal(out.kind, "combat_render");
  assert.deepEqual(out.untargeted.map(f => f.uid), ["r2"]);   // the fighter, not the wizard
  assert.equal(out.idle, 1);                                   // the wizard, un-set
});

test("an unknown phase is the inert 'none' state", () => {
  // ("setup" was the example unknown phase before #399 made it a real one.)
  assert.equal(classifyControlState({phase: "bogus"}, ctxFor()).kind, "none");
});

// ---- pre-game setup lobby (#399) --------------------------------------------

test("setup: the host gets the Start-game state", () => {
  const state = {phase: "setup", figures: [fig("r1", "red"), fig("b1", "blue")]};
  const out = classifyControlState(state, ctxFor({mySides: ["red"], isHost: true}));
  assert.equal(out.kind, "setup_host");
});

test("setup: the host state holds with seats still open (start is never gated)", () => {
  const state = {phase: "setup", figures: [fig("r1", "red"), fig("b1", "blue")]};
  const out = classifyControlState(state,
    ctxFor({mySides: ["red"], isHost: true, openSeats: ["blue"]}));
  assert.equal(out.kind, "setup_host");
});

test("setup: a non-host (joiner or spectator) waits on the host", () => {
  const state = {phase: "setup", figures: [fig("r1", "red"), fig("b1", "blue")]};
  const joiner = classifyControlState(state, ctxFor({mySides: ["blue"]}));
  assert.equal(joiner.kind, "setup_waiting");
  const spectator = classifyControlState(state, ctxFor({}));
  assert.equal(spectator.kind, "setup_waiting");
});

test("setup: victory still short-circuits the lobby states", () => {
  const out = classifyControlState({phase: "setup", victory: "red"},
    ctxFor({isHost: true}));
  assert.equal(out.kind, "victory");
});
