// Pure turn-flow state classifier for the Action panel (#364).
//
// classifyControlState() takes the game state plus a small context of the UI
// globals and ownership predicates it needs, and returns a plain descriptor of
// WHICH turn-flow state the local client is in — with no DOM mutation and no side
// effects. drawControls() in board.js switches on the returned `kind` and does all
// the rendering (setHint / bigPrimary / figureChecklist / drawForceRetreat / dbg
// GATE logging / warn-line click handlers stay in the renderer).
//
// Splitting the decision out makes the player-gating rules that caused #326/#333/
// #347 (a computer or unclaimed seat must NOT hide Resolve; an admin is not the
// AI's actor) unit-testable with plain state objects — no live server, no second
// browser context (see js-tests/control_state.test.js).

// The resolve-gate invariant, shared with pendingShooters() in board.js (#372):
// a figure the server flagged as must-attack that still has no queued PLAN.
export const needsTarget = (figure, mustAttack, plan) =>
  mustAttack.has(figure.uid) && !plan[figure.uid];

// ctx: {
//   myTurnActor(f)     -> is this figure mine to set an action for this turn (#347)
//   isComputerSide(s)  -> is this side AI-driven
//   plan               -> the PLAN map (uid -> queued action)
//   chosenOption       -> UI global: a move option mid-placement (needs a hex)
//   sel                -> UI global: the figure being placed
//   openSeats          -> OPEN_SEATS: sides currently open to claim (#85)
//   isHost             -> may this client start a lobby game (host or admin, #399)
// }
export function classifyControlState(state, ctx) {
  const {myTurnActor, isComputerSide, plan, chosenOption, sel, openSeats, isHost} = ctx;

  if (state.victory) return {kind: "victory"};

  // The pre-game setup lobby (#399): seats are claimable and characters editable,
  // but no turn is running yet. Only the host (or an admin) gets the Start-game
  // control; everyone else waits on the host. The host may start with seats still
  // open (an unclaimed seat stays claimable mid-game, as today), so setup_host
  // never gates on openSeats.
  if (state.phase === "setup") {
    return isHost ? {kind: "setup_host"} : {kind: "setup_waiting"};
  }

  if (state.phase === "select") {
    const active = state.active_uid
      ? (state.figures || []).find(f => f.uid === state.active_uid) || null
      : null;
    if (!active) return {kind: "select_resolving"};
    if (!myTurnActor(active)) {
      return isComputerSide(active.side)
        ? {kind: "select_computer", active}
        : {kind: "select_waiting_human", active};
    }
    const placing = !!chosenOption && sel === active.uid;
    return {kind: "select_mine", active, placing};
  }

  if (state.phase === "combat") {
    const actionable = new Set(state.combat_actionable || []);
    const actors = (state.figures || []).filter(
      f => f.label && myTurnActor(f) && actionable.has(f.uid));
    const ready = new Set(state.combat_ready || []);

    // Server-authoritative "attacks resolved -> end the turn" (#334): shown only
    // once the server has resolved the combined queue, never on a client-local flag.
    if (state.combat_resolved) return {kind: "combat_resolved"};

    // I've committed my side(s) but another human still has to resolve (#334):
    // detected from server state so it holds across clients until all resolve.
    const myActionableSides = [...new Set(actors.map(f => f.side))];
    if (myActionableSides.length && myActionableSides.every(side => ready.has(side)))
      return {kind: "combat_queued_waiting"};

    // A client with no actionable figures may be waiting on ANOTHER human side —
    // but only a named human whose seat someone actually holds. A computer or an
    // unclaimed/abandoned seat must NOT hide Resolve or the game bricks (#333); we
    // fall through to combat_render so this human can drive resolve_combat.
    const others = actors.length ? [] : (state.figures || []).filter(
      f => f.label && actionable.has(f.uid) && !myTurnActor(f));
    const humanOther = others.find(
      f => !isComputerSide(f.side) && !openSeats.includes(f.side) && !ready.has(f.side));
    if (humanOther) return {kind: "combat_waiting_human", humanOther};

    // #212/#217/#220: gate Resolve on your own must-attack figures until each has a
    // PLAN entry; name the ones still needing a target; soft-warn the idle rest.
    const mustAttack = new Set(state.must_attack || []);
    const untargeted = actors.filter(f => needsTarget(f, mustAttack, plan));
    const idle = actors.filter(f => !plan[f.uid] && !mustAttack.has(f.uid)).length;
    return {kind: "combat_render", actors, others, untargeted, idle};
  }

  return {kind: "none"};
}
