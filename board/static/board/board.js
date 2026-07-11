// Pure helpers extracted to sibling ES modules so they are unit-testable under
// `node --test` (js-tests/) instead of only through a live-server Playwright run.
// Served statically alongside board.js (non-manifest storage keeps the filename),
// so these relative imports resolve at /static/board/*.js in dev and production.
import {
  defaultModeFor, clampGeom, mergeLayout, snapGeom, fitGeom,
  maximizeGeom, resizeGeom, snapResizeGeom,
} from "./layout_geom.js";
import {classifyControlState, needsTarget} from "./control_state.js";

const SVG = "http://www.w3.org/2000/svg";
let GID = null, S = null, LAYOUT = null, PROFILE = null;
let GAME_ACTIVE = false;  // a match is running -> Game Control settings lock (#192)
// The Game Control players roster (#192 follow-up). Player 0 is always the local
// human and can't be removed; the rest are added, freely mixing "human" (same-
// screen) and "ai", up to MAX_PLAYERS. Each player maps to a side by index
// (ED_TEAMS[i]); the AI players' side ids become the game's `computer` list.
const MAX_PLAYERS = 5;
let PLAYERS = [{type: "human"}];
let sel = null;          // figure being placed (movement), if any
let optInfo = null;      // options payload for the active figure
let chosenOption = null; // move option mid-placement (needs a destination hex)
let pendingDest = null;  // hex label
let pendingFacing = null;// facing index
let pendingReady = null; // carried weapon to switch to
let PLAN = {};           // uid -> pending action for this phase (executed on Continue)
let warnKind = null;     // (legacy) kept for resetAll; the new flow warns inline
let combatResolvedTurn = -1; // the turn whose combat is resolved -> then we offer "End turn"
let lastPhase = null;    // detect phase changes to clear the plan
let frAdvance = {};      // "attackerUid>targetUid" -> follow-into-vacated-hex toggle
let YOU_CONTROL = [];    // sides this browser may act on (server-authoritative, #74/#85)
let OPEN_SEATS = [];     // sides currently open to claim (#85)
let IS_ADMIN = false;    // logged-in admin: may act on any figure (#86)
let IS_HOST = false;     // this browser created the game: drives the setup lobby (#399)
let SEATED = false;      // this game HAS seats (server-authoritative, #343) — a
                         // real multiplayer game; false only for seatless test
                         // fixtures / games built outside _start_game.
// A figure is yours iff its side is in YOU_CONTROL (admins control all). An empty
// YOU_CONTROL means one of two very different things, and they must not be
// conflated (#343): on a SEATED game it means "spectator — you own no seat", so
// you control NOTHING; only on a genuinely seatless game does it fall back to the
// same-screen rule (any non-computer side is yours). Mis-reading a spectator as
// same-screen let a watcher "control" every human side.
const myControlled = f => IS_ADMIN ? true
  : YOU_CONTROL.length ? YOU_CONTROL.includes(f.side)
  : SEATED ? false
  : (S.controllers || {})[f.side] !== "computer";
// A side driven by the AI (used by the Action panel's "computer is playing" state).
const isComputerSide = side => (S.controllers || {})[side] === "computer";
// #347: two SEPARATE gates. `myControlled` is EDIT/INSPECT reach — an admin may
// edit any figure outside the rules (#86/#180), so it stays true for every figure
// when IS_ADMIN. `myTurnActor` is the TURN-FLOW gate: "do I set this figure's
// action this turn?" A computer-controlled side is ALWAYS auto-played by the AI
// (server-side `_advance_computer`), so it is NEVER anyone's to act for — not even
// an admin's. The Action panel therefore routes AI figures to the "computer is
// playing" / auto-advance path while the admin keeps full edit reach. For a
// non-admin the two coincide (myControlled is already false for computer sides),
// so this only changes the admin case.
const myTurnActor = f => myControlled(f) && !isComputerSide(f.side);
function captureOwnership(data) {
  if ("you_control" in data) YOU_CONTROL = data.you_control || [];
  if ("open_seats" in data) OPEN_SEATS = data.open_seats || [];
  if ("is_admin" in data) IS_ADMIN = !!data.is_admin;
  if ("is_host" in data) IS_HOST = !!data.is_host;
  if ("seated" in data) SEATED = !!data.seated;
  pruneForeignPlan();
}
// Once the server has told this client which seats it holds (YOU_CONTROL
// non-empty), drop any queued PLAN entries for sides it does NOT control (#345).
// Before a seat is claimed, myControlled's fallback treats every non-computer
// side as "mine", so the client auto-queues attacks for BOTH sides; after
// claiming one seat the stale entry for the other side would POST on Resolve and
// be rejected server-side (a harmless 403). Clearing it on seat claim avoids that.
function pruneForeignPlan() {
  if (IS_ADMIN || !YOU_CONTROL.length || !S || !S.figures) return;
  for (const uid of Object.keys(PLAN)) {
    const fig = figByUid(uid);
    if (fig && !YOU_CONTROL.includes(fig.side)) delete PLAN[uid];
  }
}

const $ = id => document.getElementById(id);
const api = (path, opts) => fetch(path, opts).then(r => r.json());
const escapeHtml = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
  ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

// Template-injected value, set on window.__MELEE_CONFIG__ by an inline <script>
// in board.html before this module loads (a module's top-level names are not
// readable from the template, and Django can't render tags into a static file).
const LOGGED_IN = !!(window.__MELEE_CONFIG__ && window.__MELEE_CONFIG__.loggedIn);
// The CSRF cookie is HTTPONLY (settings hardening), so document.cookie never
// contains it; the template injects the token into __MELEE_CONFIG__ instead.
// The cookie read stays as a fallback for any non-httponly deployment.
const csrftoken = () => (window.__MELEE_CONFIG__ && window.__MELEE_CONFIG__.csrfToken)
  || (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
let SAVED = [];   // the signed-in player's saved characters (for the editor)
function postJSON(path, body) {
  return fetch(path, {method: "POST", headers: {
    "Content-Type": "application/json", "X-CSRFToken": csrftoken()},
    body: JSON.stringify(body)}).then(r => r.json());
}

// ---- diagnostic event log (#222) --------------------------------------------
// A bounded in-memory ring buffer of structured events, SEPARATE from the
// in-game narrative "Game status" log. Each entry pairs a category + message
// with a compact snapshot of the client state that matters for reading bugs, so
// after reproducing one you can grab the log and see exactly what happened and
// why — no fresh instrumentation pass. The 🐞 Log button downloads + copies it;
// ?debug=1 additionally mirrors every dbg() call to console.debug.
const DBG_MAX = 500;              // ring-buffer cap: keep the last N events
const DBG = [];
const DBG_T0 = Date.now();        // base time so entries read as "+123ms"
let dbgSeq = 0;
const DBG_MIRROR = new URLSearchParams(location.search).get("debug") === "1";
window.__MELEE_DBG__ = DBG;       // reachable from the console and e2e tests
// The compact context snapshot captured with every entry (never throws — a bad
// snapshot must not break the very logging meant to diagnose a bug).
function dbgCtx() {
  try {
    return {
      phase: S ? S.phase : null,
      turn: S ? S.turn : null,
      active_uid: S ? S.active_uid : null,
      sel,
      plan: Object.keys(PLAN),
      must_attack: S ? (S.must_attack || []) : [],
    };
  } catch (err) { return {ctxError: String(err)}; }
}
function dbg(cat, msg, extra) {
  const entry = {t: Date.now(), seq: ++dbgSeq, cat, msg, ctx: dbgCtx()};
  if (extra !== undefined) entry.extra = extra;
  DBG.push(entry);
  if (DBG.length > DBG_MAX) DBG.shift();
  if (DBG_MIRROR) console.debug(`[#${entry.seq}] ${cat}: ${msg}`, entry.ctx, extra ?? "");
  return entry;
}
// Transition tracker: log phase / turn / active-figure changes exactly once,
// when they actually change (called from render, so it never spams per-render).
let _dbgPhase, _dbgTurn, _dbgActive;
function dbgTransitions() {
  if (!S) return;
  if (S.phase !== _dbgPhase) { dbg("TRANSITION", `phase ${_dbgPhase} → ${S.phase}`); _dbgPhase = S.phase; }
  if (S.turn !== _dbgTurn) { dbg("TRANSITION", `turn ${_dbgTurn} → ${S.turn}`); _dbgTurn = S.turn; }
  if (S.active_uid !== _dbgActive) {
    const active = S.active_uid ? figByUid(S.active_uid) : null;
    dbg("TRANSITION", `active ${_dbgActive} → ${S.active_uid}` + (active ? ` (${active.name})` : ""));
    _dbgActive = S.active_uid;
  }
}
let _dbgGateKey = null;   // dedupe the combat Resolve-gate log to state changes

// One entry -> one greppable line:
//   [+123ms #7] CAT: msg | phase=… active=… plan=[…] must_attack=[…] | extra
function dbgFormatEntry(entry) {
  const ctx = entry.ctx || {};
  const plan = (ctx.plan || []).join(",");
  const mustAttack = (ctx.must_attack || []).join(",");
  let line = `[+${entry.t - DBG_T0}ms #${entry.seq}] ${entry.cat}: ${entry.msg}`
    + ` | phase=${ctx.phase} turn=${ctx.turn} active=${ctx.active_uid}`
    + ` sel=${ctx.sel} plan=[${plan}] must_attack=[${mustAttack}]`;
  if (entry.extra !== undefined) line += " | " + JSON.stringify(entry.extra);
  return line;
}
function dbgText() {
  const header = `Melee diagnostic log — gid=${GID} — ${new Date().toISOString()}`
    + ` — ${DBG.length} entr${DBG.length === 1 ? "y" : "ies"}`;
  const lines = DBG.map(dbgFormatEntry);
  const snapshot = "\n\n---- current state snapshot ----\n" + JSON.stringify({
    gid: GID, profile: PROFILE, you_control: YOU_CONTROL, open_seats: OPEN_SEATS,
    is_admin: IS_ADMIN, plan: PLAN, combatResolvedTurn, sel,
    pollActive: POLL !== null,   // live polling armed? (#308 diagnostic)
    state: S,
  }, null, 2);
  return header + "\n" + lines.join("\n") + snapshot;
}
// 🐞 Log button: download the buffer as a readable, greppable text file (plus a
// trailing full-state JSON snapshot) AND copy the same text to the clipboard.
async function downloadDebugLog() {
  const text = dbgText();
  const blob = new Blob([text], {type: "text/plain"});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `melee-debug-${GID || "nogame"}-${Date.now()}.txt`;
  document.body.appendChild(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  let copied = false;
  if (navigator.clipboard) {
    try { await navigator.clipboard.writeText(text); copied = true; } catch (err) { /* clipboard blocked */ }
  }
  flash(copied ? "🐞 Debug log downloaded + copied to clipboard."
               : "🐞 Debug log downloaded.");
}
// Global JS error capture — a thrown error or a rejected promise lands in the
// same log, so a crash is readable alongside the interactions that led to it.
window.addEventListener("error", (event) =>
  dbg("ERROR", "window.onerror: " + (event.message || event.error),
      {filename: event.filename, lineno: event.lineno}));
window.addEventListener("unhandledrejection", (event) =>
  dbg("ERROR", "unhandledrejection: "
      + ((event.reason && event.reason.message) || String(event.reason))));

async function startGame(query) {
  const data = await api(`/api/game/new?${query}`);
  GID = data.gid; LAYOUT = data.layout; S = data.state; PROFILE = data.profile;
  captureOwnership(data); history.replaceState({}, "", `/game/${GID}`);
  optCache = {}; _lastStateJSON = ""; _lastBoardSig = null; _lastRev = null;
  resetAll(); resetGameLifecycle(); ensureGameCatalog(); render();
  startPolling();                 // re-arm live polling for the new game (#308)
  GAME_ACTIVE = true; syncGameControl();
}
// The setup controls are now an always-visible inline "Game Control" panel, so
// open/close are no-ops kept only for the callers that still reference them (the
// post-login ?setup deep link, the editor's Back button).
function openSetup() { const gc = $("gameControl"); if (gc) gc.scrollIntoView({block: "nearest"}); }
function closeSetup() {}
// The AI players' side ids (player i -> ED_TEAMS[i]), as the engine's comma-
// separated `computer` list.
function computerSides() {
  return PLAYERS.map((pl, index) => pl.type === "ai" ? ED_TEAMS[index] : null)
    .filter(Boolean).join(",");
}
// The Remote players' side ids, as the `open` list (#399): those seats are born
// open, so the game starts in the setup lobby where a remote player can claim
// its seat over the invite link and edit its characters before the host starts.
function openSides() {
  return PLAYERS.map((pl, index) => pl.type === "remote" ? ED_TEAMS[index] : null)
    .filter(Boolean).join(",");
}
// "Wizards" in the rule-set dropdown is a roster MODE played under Classic rules,
// not a rule set of its own. These two helpers resolve it everywhere the raw
// dropdown value would otherwise leak out as a profile name.
function wizardsMode() { return $("profile").value === "Wizards"; }
function chosenProfile() { return wizardsMode() ? "Classic Melee" : $("profile").value; }

async function startSetup() {
  // The direct-start (preset) path: seat the roster and drop into the game. In
  // Wizards mode this fields the preset wizard; the editable path (newGame ->
  // openEditor) is the one a player normally takes to pick each wizard's spells.
  const p = encodeURIComponent(chosenProfile());
  const practice = $("practiceMode") && $("practiceMode").checked ? 1 : 0;
  // teams = player count, per_team = characters per player (uniform), and the AI
  // players' sides drive the explicit `computer` list the endpoint now honours.
  const q = `profile=${p}&teams=${PLAYERS.length}&per_team=${$("perTeam").value}`
    + `&computer=${encodeURIComponent(computerSides())}`
    + `&open=${encodeURIComponent(openSides())}&practice=${practice}`
    + (wizardsMode() ? "&wizards=1" : "");
  await startGame(q);
}
// Add a player of the given type ("human" | "ai" | "remote") to the roster, up
// to the cap. "remote" (#399) is a human player joining over the invite link:
// their seat is born open and the game starts in the setup lobby.
function addPlayer(type) {
  if (GAME_ACTIVE || PLAYERS.length >= MAX_PLAYERS) return;
  PLAYERS.push({type: (type === "ai" || type === "remote") ? type : "human"});
  renderPlayers();
}
// Remove a player. The local human (index 0) is never removable.
function removePlayer(index) {
  if (GAME_ACTIVE || index <= 0 || index >= PLAYERS.length) return;
  PLAYERS.splice(index, 1);
  renderPlayers();
}
// Draw the roster rows and drive the state-based enablement of the add-player
// buttons (disabled at the 5-player cap or while locked) and New Game (live only
// with >= 2 players, with a short reason shown when it can't start). (#192)
function renderPlayers() {
  const locked = GAME_ACTIVE;
  const wrap = $("playerRoster");
  if (wrap) wrap.innerHTML = PLAYERS.map((pl, index) => {
    const local = index === 0;
    const kind = local ? "You (human)" : pl.type === "ai" ? "AI"
      : pl.type === "remote" ? "Remote" : "Human";
    const side = ED_TEAMS[index];
    const remove = (local || locked) ? ""
      : `<button class="pl-remove" onclick="removePlayer(${index})" title="Remove player">✕</button>`;
    return `<div class="pl-row"><span class="chip ${side}">${escapeHtml(sideName(side))}</span>`
      + `<span class="pl-type">${kind}</span>${remove}</div>`;
  }).join("");
  const count = PLAYERS.length;
  const countEl = $("playerCount"); if (countEl) countEl.textContent = count;
  const full = count >= MAX_PLAYERS;
  const addHuman = $("addHumanBtn"); if (addHuman) addHuman.disabled = locked || full;
  const addAi = $("addAiBtn"); if (addAi) addAi.disabled = locked || full;
  const addRemote = $("addRemoteBtn"); if (addRemote) addRemote.disabled = locked || full;
  const enoughPlayers = count >= 2;
  const newBtn = $("newGameBtn"); if (newBtn) newBtn.disabled = locked || !enoughPlayers;
  const reason = $("newGameReason");
  if (reason) reason.textContent = (locked || enoughPlayers) ? "" : "Add at least 2 players to start.";
}
// New Game starts a match through the existing setup flow, then locks the panel.
async function newGame() {
  if (GAME_ACTIVE || PLAYERS.length < 2) return;
  dbg("INTERACT", "New Game pressed", {players: PLAYERS.map(p => p.type)});
  // Wizards are personal — a wizard is defined by the spells it picks — so Wizards
  // mode opens the character editor (pre-seeded with a fighter + a wizard per side)
  // instead of dropping straight into a preset game. Start match launches from
  // there. Plain Melee keeps its fast preset start.
  if (wizardsMode()) { await openEditor(); return; }
  await startSetup();
}
// The editable pre-game state: no game tracked, Game Control unlocked with New
// Game live, the Map blank, and the Characters tracker empty. This is what a
// fresh load (no deep-link) shows, and where End Game returns to. (#192)
function showPreGame() {
  GID = null; S = null; LAYOUT = null; GAME_ACTIVE = false;
  PLAYERS = [{type: "human"}];         // fresh roster: just the local human (#192)
  _lastStateJSON = ""; _lastBoardSig = null; _lastRev = null;
  resetAll(); resetGameLifecycle(); closeMenu();
  $("svg").innerHTML = "";
  $("phaseBanner").textContent = "No game — set up the players and press New Game.";
  $("hint").textContent = "";
  $("controls").innerHTML = "";
  $("roster").innerHTML = `<span class="muted">No game in progress.</span>`;
  $("log").innerHTML = "";
  $("selInfo").innerHTML = `<span class="muted">No figure selected.</span>`;
  $("turnInfo").textContent = "";
  syncGameControl();
}
// End Game abandons the running match client-side (no backend endpoint needed)
// and drops back to the editable pre-game state. (#192)
function endGame() { history.replaceState({}, "", "/"); showPreGame(); }
// Reflect the lock state: while a game runs every setting is read-only, New Game
// is disabled, and End Game is live; before/after a game the reverse holds. (#192)
function syncGameControl() {
  const locked = GAME_ACTIVE;
  ["profile", "perTeam", "practiceMode", "editCharBtn"].forEach(id => {
    const el = $(id); if (el) el.disabled = locked;
  });
  const end = $("endGameBtn"); if (end) end.disabled = !locked;
  const gc = $("gameControl"); if (gc) gc.classList.toggle("locked", locked);
  // The players roster owns the add-player + New Game enablement (#192).
  renderPlayers();
}
async function refresh() {
  const data = await api(`/api/game/${GID}`);
  if (data.error) {                 // game gone (it ended or the dev server restarted)
    gameLost();
    return;
  }
  LAYOUT = data.layout; S = data.state; captureOwnership(data); optCache = {};
  GAME_ACTIVE = true; syncGameControl(); render();
  maybeAutoTarget();   // #299: sole-target auto-queue on a fresh (deep-link) load
}
// The server no longer holds this match (it was never persisted and a restart
// lost it, or the link is stale). Show ONE persistent notice — the banner is
// state, not a toast; it stays until the player starts or joins another game —
// instead of letting every button error forever with no explanation (#275: the
// 🐞 log showed five End-turn retries against a game that had vanished).
function gameLost() {
  $("phaseBanner").textContent =
    "Game not found — the server no longer has this match. Start a New game.";
  flash("This game is no longer on the server.");
  dbg("ERROR", "game lost server-side — the match is gone", {gid: GID});
}
async function act(body) {
  const data = await api(`/api/game/${GID}/action`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  });
  if (data.error) {
    dbg("ERROR", `act rejected: ${data.error}`, {sent: body});
    if (data.error === "unknown game") gameLost();
    else flash(data.error);
    return null;
  }
  S = data.state; LAYOUT = data.layout; captureOwnership(data); optCache = {};
  return data;
}
function flash(msg) { $("hint").textContent = msg; }
// Per-side seat state + claim/open controls, folded into the Characters tracker's
// group headers (the standalone Players panel was merged in here, #192).
function seatBtn(side, action, label) {
  return `<button style="margin-left:6px;padding:1px 7px;cursor:pointer" `
    + `onclick="seatAction('${action}','${side}')">${label}</button>`;
}
function seatTag(side) {
  const controllers = S.controllers || {};
  const computer = controllers[side] === "computer";
  const mine = YOU_CONTROL.includes(side), open = OPEN_SEATS.includes(side);
  let tag = "taken", btn = "";
  if (computer) tag = "computer";
  else if (mine) { tag = "you"; if (YOU_CONTROL.length > 1) btn = seatBtn(side, "open", "Open"); }
  else if (open) { tag = "open"; btn = seatBtn(side, "claim", "Claim"); }
  return `<span class="muted">— ${tag}</span>${btn}`;
}
function adminTagHtml() {
  return IS_ADMIN
    ? `<div style="margin-bottom:5px;color:#e6b800"><b>★ Admin</b> <span class="muted">— you control every figure and can edit them outside the rules.</span></div>`
    : "";
}
function inviteHtml() {   // show when another *human* seat needs filling, even in a
  // mixed human+computer game (#165 hid it whenever any computer was present, which
  // wrongly suppressed the invite the second human needs — #192).
  const humanSeats = Object.values(S.controllers || {}).filter(c => c === "human").length;
  return humanSeats > 1 ?
    `<button style="margin-top:8px;padding:1px 7px;cursor:pointer" onclick="copyLink()">Copy invite link</button>` : "";
}
async function seatAction(action, side) {
  const data = await postJSON(`/api/game/${GID}/seat`, {action, side});
  if (data && data.error) { flash(data.error); return; }
  await refresh();   // reload with the new ownership (a joiner's cookie is now set)
}
function copyLink() {
  if (navigator.clipboard) navigator.clipboard.writeText(location.href);
  flash("Invite link copied — send it to another player, who clicks Claim.");
}
function resetSelection() { sel = null; optInfo = null; chosenOption = null; pendingDest = null; pendingFacing = null; pendingReady = null; }

function figByUid(uid) { return S.figures.find(f => f.uid === uid); }

// human-readable labels + which options require a destination hex
const PHASE_LABEL = {setup: "Game setup", select: "Action selection", combat: "Combat"};
const OPTION_LABEL = {
  move: "Full move", half_move: "Half move", charge_attack: "⚔ Charge & Attack", dodge: "Dodge",
  ready_weapon: "Ready Weapon", missile_attack: "🏹 Missile Attack", stand_up: "Stand Up", crawl: "Crawl 2",
  attack: "⚔ Attack", shift_attack: "⚔ Shift & Attack", shift_defend: "Shift & Defend",
  one_last_shot: "🏹 One Last Shot", change_weapons: "Change Weapons", disengage: "Disengage",
  hth_attack: "🤼 Grapple", pick_up: "Pick up weapon",
  go_prone: "Drop prone", kneel: "Kneel",
  do_nothing: "Do nothing", pass: "Pass (choose last)",
};
// missile_attack is here so its optional 1-hex move (option f: "move up to 1 hex
// and/or fire") gets a destination picker, not forced to hold position (#117).
const NEEDS_DEST = new Set(["move", "half_move", "charge_attack", "dodge", "disengage", "crawl", "missile_attack"]);
// Options whose move is OPTIONAL: the destination picker is offered, but "Set
// action" is enabled even with no hex chosen, so the figure can fire from where
// it stands (option f is "move up to 1 hex AND/OR fire" — moving is optional).
// Without this, a missile attacker could never fire without first stepping a hex,
// which read as a movement placement that couldn't be completed (#204).
const DEST_OPTIONAL = new Set(["missile_attack"]);
const WEAPON_CHANGE = new Set(["ready_weapon", "change_weapons"]);
// The set of weapon names a "which weapon?" selector should offer for an option,
// or null when there's no real choice (0 or 1). A weapon change picks from the
// figure's carried weapons; Pick up weapon picks from the dropped weapons in
// reach (#269 — previously it silently grabbed pickups[0]).
function readyChoices(f, option) {
  if (WEAPON_CHANGE.has(option)) {
    const carried = f.weapons || [];
    return carried.length > 1 ? carried : null;
  }
  if (option === "pick_up") {
    const pickups = (optInfo && optInfo.pickups) || [];
    return pickups.length > 1 ? pickups : null;
  }
  return null;
}
// The default selection for readyChoices: a weapon change defaults to the OTHER
// carried weapon; a pick-up defaults to the first dropped weapon in reach.
function defaultReadyChoice(f, option, choices) {
  if (WEAPON_CHANGE.has(option))
    return choices.find(weapon => weapon !== f.weapon) || choices[0];
  return choices[0];
}
const TEAM_FILL = {red: "#d0524f", blue: "#4f86d0", green: "#57b894", gold: "#e0b13c", violet: "#b07ad8"};
const fillFor = side => TEAM_FILL[side] || "#888";
const optLabel = o => OPTION_LABEL[o] || o;

// health pool: Fatigue for Tarmar figures, ST for classic Melee
const hpCur = f => f.model === "tarmar" ? f.fatigue : f.st;
const hpMax = f => f.model === "tarmar" ? f.max_fatigue : f.max_st;
function svgRect(x, y, w, h, fill) {
  const r = document.createElementNS(SVG, "rect");
  r.setAttribute("x", x); r.setAttribute("y", y);
  r.setAttribute("width", Math.max(0, w)); r.setAttribute("height", h);
  r.setAttribute("rx", 1.5); r.setAttribute("fill", fill);
  return r;
}

// ---- megahex tiling (Melee p.16) --------------------------------------------
// A megahex is a 7-hex flower (a centre hex + its 6 neighbours). Their centres
// tile the plane on a sqrt(7) sublattice generated, in axial (q,r) coords, by
// u=(2,1) and v=(-1,3) (det 7 => 7 hexes per cell). This is a faithful port of
// engine/megahex.py so the drawn seams match the engine's range math exactly.
// FLAT-top, odd-q offset -> cube, matching hexarena.hex.HexLayout(flat, odd).
function hexToAxial(col, row) {
  const zeroCol = col - 1, zeroRow = row - 1;
  const parity = zeroCol & 1;                         // odd-q
  const cubeX = zeroCol;
  const cubeZ = zeroRow - ((zeroCol - parity) >> 1);
  return [cubeX, cubeZ];                              // axial q=cube_x, r=cube_z
}
function axialDistance(qa, ra, qb, rb) {
  const dq = qa - qb, dr = ra - rb;
  return (Math.abs(dq) + Math.abs(dr) + Math.abs(dq + dr)) / 2;
}
// Lattice coordinates (a,b) of the megahex containing (col,row).
function megahexCoord(col, row) {
  const [q, r] = hexToAxial(col, row);
  const guessA = Math.round((3 * q + r) / 7);
  const guessB = Math.round((-q + 2 * r) / 7);
  let best = [guessA, guessB], bestD = Infinity;
  for (let da = -1; da <= 1; da++)
    for (let db = -1; db <= 1; db++) {
      const ca = guessA + da, cb = guessB + db;
      const cq = 2 * ca - cb, cr = ca + 3 * cb;       // flower-centre axial
      const d = axialDistance(q, r, cq, cr);
      if (d < bestD) { bestD = d; best = [ca, cb]; }
    }
  return best[0] + "," + best[1];                     // stable string id
}

// Draw the seams between adjacent hexes that belong to different megahexes.
// Each hex carries its 6 corner points; an edge sits between consecutive
// corners. The neighbour across an edge is the hex whose centre is the edge
// midpoint reflected through this hex's centre, so we locate it geometrically
// (no reliance on a direction convention). Outer board edges are left to the
// normal hex stroke; only interior megahex seams are drawn, once each.
function drawMegahexBorders(svg) {
  const centerIndex = {};
  const key = (x, y) => Math.round(x) + "," + Math.round(y);
  for (const label in LAYOUT.hexes) {
    const h = LAYOUT.hexes[label];
    centerIndex[key(h.cx, h.cy)] = h;
  }
  const drawn = new Set();
  for (const label in LAYOUT.hexes) {
    const h = LAYOUT.hexes[label];
    const myMh = megahexCoord(h.col, h.row);
    const pts = h.points;
    for (let i = 0; i < pts.length; i++) {
      const a = pts[i], b = pts[(i + 1) % pts.length];
      const midX = (a[0] + b[0]) / 2, midY = (a[1] + b[1]) / 2;
      const neighbor = centerIndex[key(2 * midX - h.cx, 2 * midY - h.cy)];
      if (!neighbor) continue;                          // board boundary
      if (megahexCoord(neighbor.col, neighbor.row) === myMh) continue;
      const edgeKey = [key(a[0], a[1]), key(b[0], b[1])].sort().join("|");
      if (drawn.has(edgeKey)) continue;                 // shared seam, draw once
      drawn.add(edgeKey);
      const seam = document.createElementNS(SVG, "line");
      seam.setAttribute("x1", a[0]); seam.setAttribute("y1", a[1]);
      seam.setAttribute("x2", b[0]); seam.setAttribute("y2", b[1]);
      seam.setAttribute("class", "mhborder");
      svg.appendChild(seam);
    }
  }
}

// ---- rendering --------------------------------------------------------------
// Rebuilding the whole SVG board (every hex, every figure <g>, every listener) is
// by far the most expensive part of a render. render() runs on every poll whose
// signature changed — during an opponent's or the AI's turn that is essentially
// every 2s tick, even for an idle watcher whose figures did not move. So gate
// drawArena on a signature of ONLY the board-affecting state: if just the log,
// seats, or ownership changed, the SVG is left untouched (#343). Any change that
// alters a hex, a token, a ring, a highlight, or the placement/selection overlay
// is captured here, so the board never goes stale.
let _lastBoardSig = null;
// Test/telemetry hook: how many times render() ran, and of those, how many
// actually rebuilt the SVG board. An e2e test asserts a board-irrelevant state
// change (e.g. a seat/ownership update) bumps `renders` but NOT `arenaDraws`.
const RENDER_STATS = {renders: 0, arenaDraws: 0};
window.__MELEE_RENDER_STATS__ = RENDER_STATS;
function boardSig() {
  const reach = (chosenOption && optInfo)
    ? (optInfo.options.find(o => o.option === chosenOption)?.reach || []) : [];
  // Everything drawArena reads: the figures (position/posture/hp/facing/flags),
  // dropped weapons, phase + active figure (rings), and the local selection /
  // placement overlay (sel token, target set via sel, reach hexes, chosen hex,
  // the per-figure plan rings). validTargets()/isTarget derive from sel+figures+
  // phase, all already here, so the target highlight is covered too.
  return JSON.stringify([
    S.figures, S.dropped, S.phase, S.active_uid,
    reach, pendingDest, sel, PLAN,
  ]);
}

function render() {
  if (!S) return;
  RENDER_STATS.renders += 1;
  dbgTransitions();                      // log phase / turn / active changes
  if (S.phase !== lastPhase) {           // new phase → fresh, empty plan
    lastPhase = S.phase; PLAN = {}; warnKind = null; resetSelection(); closeMenu();
  }
  const bsig = boardSig();
  if (bsig !== _lastBoardSig) {          // only rebuild the SVG when the board changed
    _lastBoardSig = bsig;
    RENDER_STATS.arenaDraws += 1;
    drawArena();
  }
  drawControls();
  drawSelInfo();
  drawRoster();
  drawLog();
  const seatKinds = Object.values(S.controllers || {});
  const humans = seatKinds.filter(c => c === "human").length;
  const computers = seatKinds.filter(c => c === "computer").length;
  let seatLabel = "";
  if (humans && computers) seatLabel = ` · ${humans} human vs ${computers} computer`;
  else if (computers) seatLabel = " · vs Computer";
  else if (humans > 1) seatLabel = " · same screen";
  $("turnInfo").textContent = (PROFILE || "") + seatLabel;
}

function drawArena() {
  const svg = $("svg");
  svg.setAttribute("viewBox", `0 0 ${LAYOUT.width} ${LAYOUT.height}`);
  svg.setAttribute("width", LAYOUT.width);
  svg.setAttribute("height", LAYOUT.height);
  svg.innerHTML = "";

  const reach = new Set((chosenOption && optInfo)
    ? (optInfo.options.find(o => o.option === chosenOption)?.reach || []) : []);

  for (const label in LAYOUT.hexes) {
    const h = LAYOUT.hexes[label];
    const poly = document.createElementNS(SVG, "polygon");
    poly.setAttribute("points", h.points.map(p => p.join(",")).join(" "));
    poly.setAttribute("data-label", label);   // hex id, for hit-testing (and tests)
    poly.setAttribute("class", "hex" + (reach.has(label) ? " reach" : "")
      + (label === pendingDest ? " chosen" : ""));
    if (reach.has(label)) poly.addEventListener("click", () => onReachClick(label));
    svg.appendChild(poly);
  }

  drawMegahexBorders(svg);   // megahex seams, above the hexes but below tokens

  for (const d of (S.dropped || [])) {              // weapons lying on the ground
    const h = LAYOUT.hexes[d.label];
    if (!h) continue;
    const mark = document.createElementNS(SVG, "text");
    mark.setAttribute("x", h.cx + LAYOUT.size * 0.5);
    mark.setAttribute("y", h.cy + LAYOUT.size * 0.5);
    mark.setAttribute("font-size", LAYOUT.size * 0.5);
    mark.setAttribute("opacity", "0.75");
    mark.textContent = "🗡";
    const dtip = document.createElementNS(SVG, "title");
    dtip.textContent = `${d.name} (dropped)`;
    mark.appendChild(dtip);
    svg.appendChild(mark);
  }

  for (const f of S.figures) {
    if (!f.label) continue;
    const h = LAYOUT.hexes[f.label];
    const g = document.createElementNS(SVG, "g");
    let cls = "fig " + f.side;
    if (f.uid === sel) cls += " sel";
    if (isTarget(f.uid)) cls += " target";
    if (f.posture === "prone") cls += " prone";
    if (f.dodging || f.defending) cls += " dodge";
    g.setAttribute("class", cls);

    // Grapplers share one hex — fan them around its centre so each stays visible.
    if (f.hth_opponents && f.hth_opponents.length) {
      const ring = [f.uid, ...f.hth_opponents].sort();
      const ang = ring.indexOf(f.uid) * (Math.PI / 3), d = LAYOUT.size * 0.34;
      g.setAttribute("transform",
        `translate(${Math.cos(ang) * d},${Math.sin(ang) * d})`);
    }

    const tip = document.createElementNS(SVG, "title");   // native hover tooltip
    tip.textContent = `${f.name} (${f.side})`
      + (f.flying ? " — flying" : "")
      + ((f.size > 1) ? ` — ${f.size} hexes` : "");
    g.appendChild(tip);

    // A multi-hex figure (the giant) fills its whole footprint with a tinted
    // cluster; the token + label still sit on its anchor hex.
    if (f.footprint && f.footprint.length > 1) {
      for (const fpLabel of f.footprint) {
        const fh = LAYOUT.hexes[fpLabel];
        if (!fh) continue;
        const cell = document.createElementNS(SVG, "polygon");
        cell.setAttribute("points", fh.points.map(p => p.join(",")).join(" "));
        cell.setAttribute("fill", f.dead ? "#555" : fillFor(f.side));
        cell.setAttribute("fill-opacity", "0.35");
        cell.setAttribute("stroke", fillFor(f.side));
        cell.setAttribute("stroke-width", "2");
        g.appendChild(cell);
      }
    }

    // A flying figure casts a soft "shadow" ring so it reads as airborne.
    if (f.flying && !f.dead) {
      const shadow = document.createElementNS(SVG, "circle");
      shadow.setAttribute("cx", h.cx); shadow.setAttribute("cy", h.cy + LAYOUT.size * 0.3);
      shadow.setAttribute("rx", LAYOUT.size * 0.6);
      shadow.setAttribute("r", LAYOUT.size * 0.55);
      shadow.setAttribute("fill", "#0006");
      g.appendChild(shadow);
    }

    const body = document.createElementNS(SVG, "circle");
    body.setAttribute("class", "body");
    body.setAttribute("cx", h.cx); body.setAttribute("cy", h.cy);
    body.setAttribute("r", LAYOUT.size * 0.6);
    body.setAttribute("fill", f.dead ? "#555" : fillFor(f.side));
    g.appendChild(body);

    const txt = document.createElementNS(SVG, "text");
    txt.setAttribute("x", h.cx); txt.setAttribute("y", h.cy);
    txt.textContent = f.dead ? "✗" : hpCur(f);
    g.appendChild(txt);

    // facing arrow — drawn on top of the token, in white, so it reads clearly
    if (!f.dead && f.front_label && LAYOUT.hexes[f.front_label]) {
      const fh = LAYOUT.hexes[f.front_label];
      const len = Math.hypot(fh.cx - h.cx, fh.cy - h.cy) || 1;
      const ux = (fh.cx - h.cx) / len, uy = (fh.cy - h.cy) / len;
      const ox = -uy, oy = ux, s = LAYOUT.size;
      const rIn = s * 0.5, rOut = s * 0.92, w = s * 0.32;
      const arrow = document.createElementNS(SVG, "polygon");
      arrow.setAttribute("points",
        `${h.cx + ux*rOut},${h.cy + uy*rOut} `
        + `${h.cx + ux*rIn + ox*w},${h.cy + uy*rIn + oy*w} `
        + `${h.cx + ux*rIn - ox*w},${h.cy + uy*rIn - oy*w}`);
      arrow.setAttribute("fill", "#fff");
      arrow.setAttribute("stroke", "#0008");
      arrow.setAttribute("stroke-width", "0.8");
      g.appendChild(arrow);
    }

    if (!f.dead) {                                   // health bar beneath the token
      const bw = LAYOUT.size * 1.1, bh = 5;
      const bx = h.cx - bw / 2, by = h.cy + LAYOUT.size * 0.62;
      const frac = Math.max(0, Math.min(1, hpCur(f) / (hpMax(f) || 1)));
      g.appendChild(svgRect(bx, by, bw, bh, "#0009"));
      g.appendChild(svgRect(bx, by, bw * frac, bh,
        frac > 0.5 ? "#5fae74" : frac > 0.25 ? "#d8b54a" : "#d0524f"));
    }

    // Green ring = action set (a combat plan, or a committed selection action).
    if (PLAN[f.uid] || (S.phase === "select" && f.acted)) {
      const ring = document.createElementNS(SVG, "circle");
      ring.setAttribute("cx", h.cx); ring.setAttribute("cy", h.cy);
      ring.setAttribute("r", LAYOUT.size * 0.82);
      ring.setAttribute("fill", "none");
      ring.setAttribute("stroke", "#7CFC8C"); ring.setAttribute("stroke-width", "2.5");
      g.appendChild(ring);
    }
    // Amber highlight = the figure whose turn it is to act right now (#192; the
    // inline per-character controls key off the same active figure, #199). A bold
    // pulsing, glowing gold ring so it's unmistakable which counter is live.
    if (S.phase === "select" && f.uid === S.active_uid && !f.dead) {
      const ring = document.createElementNS(SVG, "circle");
      ring.setAttribute("cx", h.cx); ring.setAttribute("cy", h.cy);
      ring.setAttribute("r", LAYOUT.size * 0.92);
      ring.setAttribute("class", "activering");
      g.appendChild(ring);
    }

    // A figure that chose dodge (vs missiles) or Shift & Defend (vs melee) this
    // turn is attacked on FOUR dice — mark it with a guard ring + shield glyph so
    // everyone can see who's guarding (#247: defend used to be invisible).
    if ((f.dodging || f.defending) && !f.dead) {
      const guardRing = document.createElementNS(SVG, "circle");
      guardRing.setAttribute("cx", h.cx); guardRing.setAttribute("cy", h.cy);
      guardRing.setAttribute("r", LAYOUT.size * 0.74);
      guardRing.setAttribute("class", "guardring");
      g.appendChild(guardRing);
      const shield = document.createElementNS(SVG, "text");
      shield.setAttribute("x", h.cx - LAYOUT.size * 0.6);
      shield.setAttribute("y", h.cy - LAYOUT.size * 0.52);
      shield.setAttribute("font-size", LAYOUT.size * 0.72);
      shield.setAttribute("class", "guard");
      shield.textContent = "🛡";
      g.appendChild(shield);
    }

    if (f.flying && !f.dead) {                       // airborne badge (wings)
      const wings = document.createElementNS(SVG, "text");
      wings.setAttribute("x", h.cx + LAYOUT.size * 0.55);
      wings.setAttribute("y", h.cy - LAYOUT.size * 0.5);
      wings.setAttribute("font-size", LAYOUT.size * 0.7);
      wings.setAttribute("text-anchor", "middle");
      wings.textContent = "🕊";
      g.appendChild(wings);
    }

    g.addEventListener("click", (ev) => { ev.stopPropagation(); onFigureClick(f); });
    g.addEventListener("mouseenter", () => onFigureHover(f));
    g.addEventListener("mouseleave", scheduleHoverClose);
    svg.appendChild(g);
  }
}

function isTarget(uid) {
  if (S.phase !== "combat" || !sel) return false;
  const attacker = figByUid(sel);
  const me = figByUid(uid);
  return attacker && me && attacker.side !== me.side && validTargets().includes(uid);
}
function validTargets() {
  if (!optInfo) return [];
  const f = figByUid(sel);
  if (!f) return [];
  const w = f.weapon;
  // attacker must have chosen an attack option (reflected in optInfo at move time)
  return [...(optInfo._targets || []), ...(optInfo.hth_targets || [])];
}

// ---- controls ---------------------------------------------------------------
// One clear primary action per phase, a "what to do now" line, a per-figure
// checklist, and inline (non-blocking) warnings -- no double-confirm. (#176)
// drawControls is now a THIN RENDERER over classifyControlState (#364): the pure
// classifier (control_state.js) decides WHICH turn-flow state this client is in
// from plain state + the ownership predicates + the UI globals it needs, and this
// function switches on the returned `kind` to do the DOM. All the side effects that
// must stay in the renderer — ensureActiveOptions, the GATE dbg logging, the warn-
// line click handlers, figureChecklist, drawForceRetreat — live here, unchanged.
// See the classifier's comments for why each gate exists (#326/#333/#334/#347).
function drawControls() {
  const c = $("controls"); c.innerHTML = "";
  $("phaseBanner").textContent = bannerFor(S.phase);
  const state = classifyControlState(S, {
    myTurnActor, isComputerSide,
    plan: PLAN, chosenOption, sel, openSeats: OPEN_SEATS,
    isHost: IS_HOST || IS_ADMIN,
  });

  switch (state.kind) {
    case "setup_host":
      // The pre-game lobby, host's view (#399): a Start-game control, always
      // enabled — the host may start with seats still open (an unclaimed seat
      // stays claimable mid-game, as today).
      setHint("Game setup — waiting for players. Claim a seat, then edit your"
        + " characters. Start the game when everyone is ready.");
      bigPrimary(c, "Start game →", () => {
        dbg("INTERACT", "Start game pressed");
        act({type: "begin_game"}).then(after);
      });
      return;

    case "setup_waiting":
      // The lobby for everyone else: claim a seat, edit your fighters, and wait
      // for the host to start.
      setHint("Game setup — claim a seat, then edit your characters."
        + " Waiting for the host to start the game…");
      return;

    case "victory":
      // The match is decided. The old "Start next round →" here posted end_turn,
      // but #277 makes the server short-circuit turn-advancement once there's a
      // victor, so that action was a dead no-op (#387). A won game is over, so the
      // only useful affordance is to play again: start a FRESH game reusing the
      // existing setup machinery (the same startSetup() Game Control's New Game
      // runs), keeping the current roster. startSetup() has no GAME_ACTIVE guard,
      // so it works even though the finished game still holds GAME_ACTIVE (which
      // disables New Game in Game Control until End Game). This reuses the new-game
      // path wholesale — no rematch backend is invented.
      setHint(`🏆 <b>${sideName(S.victory)}</b> wins the field!`);
      bigPrimary(c, "New game →", () => startSetup());
      return;

    case "select_resolving":
      setHint("Resolving the action pass…");
      return;

    case "select_computer":
      setHint("🤖 Computer is playing…");
      return;

    case "select_waiting_human": {
      const active = state.active;
      setHint(`Waiting for <span class="chip ${active.side}">${sideName(active.side)}</span>`
              + ` to set <b>${escapeHtml(active.name)}</b>'s action…`);
      return;                                              // non-owning client: no controls
    }

    case "select_mine": {
      // Mine: name the active character, then render its action-selection block (the
      // option list, or the mid-placement confirm once a destination option is chosen).
      const active = state.active;
      setHint(state.placing
        ? `Placing <b>${escapeHtml(active.name)}</b> — click a green hex on the board, then press <b>Set action</b>.`
        : `<b>${escapeHtml(active.name)}</b> has initiative — choose its action below, on its counter, or the board.`);
      drawActionActor(c, active);
      ensureActiveOptions();   // load the active figure's real options, then re-draw
      return;
    }

    case "combat_resolved":
      setHint("Attacks resolved — push back any beaten foes, then end the turn.");
      drawForceRetreat(c);                 // post-combat shoves, if any
      bigPrimary(c, "End turn →", () => {
        dbg("INTERACT", "End turn pressed");
        // #242: carry the turn we mean to end so a double-click / retried POST
        // that lands after the first end_turn already advanced is a safe no-op.
        resetAll(); act({type: "end_turn", expected_turn: S.turn}).then(after);
      });
      return;

    case "combat_queued_waiting":
      setHint("Attacks queued — waiting for the other player to resolve…");
      return;

    case "combat_waiting_human": {
      const humanOther = state.humanOther;
      setHint(`Waiting for <span class="chip ${humanOther.side}">${sideName(humanOther.side)}</span>`
              + ` to set <b>${escapeHtml(humanOther.name)}</b>'s action…`);
      return;
    }

    case "combat_render": {
      const {actors, others, untargeted, idle} = state;
      if (actors.length) setHint("Choose each figure's attack, then resolve.");
      else if (others.length) setHint("🤖 Computer is playing… resolve when ready.");
      else setHint("Resolve combat to continue.");
      figureChecklist(c, actors);
      drawCastSliders(c, actors);         // ST/mana sliders for queued missile casts
      // #212: a figure that committed to an attack option AND has a valid target
      // (server's must_attack) would silently waste its shot if combat resolved
      // without a queued attack for it. The classifier names the untargeted set;
      // here we name each one and soft-warn the idle rest.
      if (idle) warnLine(c, `${idle} figure${idle > 1 ? "s" : ""} will do nothing.`);
      for (const f of untargeted) {
        const aimed = (f.option === "missile_attack" || f.option === "one_last_shot");
        const weapon = f.weapon ? ` ${f.weapon}` : "";
        const line = warnLine(c, `Pick a target for ${f.name} — it ${aimed ? "aimed" : "committed to attack with"}`
                    + `${aimed ? " a" + weapon : weapon}.`);
        // #220: the prompt names the *shooter*, so clicking it selects that
        // figure and opens its targeting menu — the player no longer has to hunt
        // for their own counter to clear the gate.
        line.classList.add("clickable");
        line.title = `Click to target ${f.name}`;
        line.addEventListener("click", () => onFigureClick(f));
        // #397/#398 escape hatch: a committed attacker the player can't or won't
        // target must not be able to hang the turn. A "Hold fire" button stands it
        // down (server: hold_fire), dropping it from the gate so Resolve can clear.
        const hold = document.createElement("button");
        hold.className = "holdfire";
        hold.textContent = `Hold fire — ${f.name} won't attack`;
        hold.title = `${f.name} stands down and does not attack this turn`;
        hold.addEventListener("click", () => holdFire(f));
        line.appendChild(hold);
      }
      const resolveBtn = bigPrimary(c, actors.length ? "Resolve attacks" : "Resolve combat", () => {
        dbg("INTERACT", "Resolve pressed", {queued: Object.keys(PLAN).length, actors: actors.length});
        // Local optimistic flag: I've committed this turn's combat, so stop
        // auto-targeting my own figures (queuePendingShotAt / maybeAutoTarget).
        // The End-turn screen itself is driven by server state (S.combat_resolved),
        // not this flag, so a networked client can't jump ahead of another human.
        combatResolvedTurn = S.turn;
        executePlans("combat");
      });
      if (untargeted.length) {
        resolveBtn.disabled = true;
        // GATE: why Resolve is disabled — which must_attack figures are still
        // untargeted. Deduped to when the untargeted set actually changes so a
        // stalled combat doesn't refill the log on every re-render.
        const gateKey = untargeted.map(f => f.uid).sort().join(",");
        if (gateKey !== _dbgGateKey) {
          _dbgGateKey = gateKey;
          dbg("GATE", `Resolve disabled — ${untargeted.length} must-attack figure(s) untargeted`,
              {untargeted: untargeted.map(f => ({uid: f.uid, name: f.name, option: f.option}))});
          // Anomaly self-check (#217/#221 signature): a must-attack figure with
          // NO queueable target offered at all is the resolve-gate deadlock —
          // Resolve can never clear. Cheap: only inspects already-cached options.
          for (const f of untargeted) {
            const info = optCache[f.uid];
            if (info && !(info._targets || []).length && !(info.hth_targets || []).length)
              dbg("WARN", `deadlock: ${f.name} must attack but has no queueable target offered`,
                  {uid: f.uid, option: f.option, weapon: f.weapon});
          }
        }
      } else {
        _dbgGateKey = null;
      }
      return;
    }
  }
}

function sideName(side) { return side ? side.charAt(0).toUpperCase() + side.slice(1) : ""; }
function setHint(html) { $("hint").innerHTML = html; }

function bigPrimary(c, text, fn, primary = true) {
  const b = document.createElement("button");
  b.textContent = text;
  b.className = "big" + (primary ? " primary" : "");
  b.addEventListener("click", fn);
  c.appendChild(b);
  return b;
}

function warnLine(c, text) {
  const w = document.createElement("div");
  w.className = "warnline";
  w.textContent = "⚠ " + text;
  c.appendChild(w);
  return w;
}

// The ST (mana) slider for each queued missile cast (Magic Fist): a wizard invests
// 1..max_st ST for a missile spell (1 die/ST, p.12). State-driven — it renders from
// PLAN on every pass, so it stays until the cast is cleared or resolved (no transient
// UI). Dragging updates PLAN[uid].stUsed in place (no re-render, so the drag holds).
function drawCastSliders(c, actors) {
  const mine = new Set(actors.map(f => f.uid));
  for (const plan of Object.values(PLAN)) {
    if (plan.phase !== "combat" || !plan.cast || !plan.isMissile) continue;
    if (!mine.has(plan.uid) || plan.stMax <= plan.stMin) continue;   // nothing to adjust
    const figure = figByUid(plan.uid);
    const wrap = document.createElement("div");
    wrap.className = "cast-st"; wrap.style.marginTop = "8px";
    const label = document.createElement("label");
    label.innerHTML = `🔮 ${escapeHtml(figure ? figure.name : plan.uid)} — `
      + `${escapeHtml(plan.spellName)} power (ST/mana): `;
    const value = document.createElement("b");
    value.className = "cast-st-val"; value.textContent = plan.stUsed;
    const range = document.createElement("input");
    range.type = "range"; range.className = "cast-st-range";
    range.min = plan.stMin; range.max = plan.stMax; range.value = plan.stUsed;
    range.addEventListener("input", () => {
      plan.stUsed = parseInt(range.value, 10);
      value.textContent = plan.stUsed;
      plan.label = castLabel(plan.spellName, plan.castWho, true, plan.stUsed);
    });
    label.appendChild(value);
    wrap.appendChild(label);
    wrap.appendChild(range);
    c.appendChild(wrap);
  }
}

// A per-figure status list: which of the active side's figures are set, and
// which still need you.
function figureChecklist(c, figs) {
  if (!figs.length) return;
  const list = document.createElement("div");
  list.className = "checklist";
  list.innerHTML = figs.map(f => {
    const plan = PLAN[f.uid];
    const status = plan ? `✓ ${escapeHtml(plan.label || "set")}` : "needs you";
    return `<div class="row"><span>${escapeHtml(f.name)}</span>`
      + `<span class="${plan ? "done" : "todo"}">${status}</span></div>`;
  }).join("");
  c.appendChild(list);
}

// Post-combat: an attacker that dealt ST damage and took none this turn may
// shove an adjacent, beaten foe back one hex (the server lists eligible pairs in
// force_retreat_options). Each control carries an "advance" toggle to follow
// into the vacated hex. State-driven: the controls stay until acted on or the
// turn ends — no transient prompt (project UI rule).
function drawForceRetreat(c) {
  const opts = (S.force_retreat_options || []).filter(o => {
    const attacker = figByUid(o.attacker);
    return attacker && myTurnActor(attacker);
  });
  if (!opts.length) return;
  const head = document.createElement("div");
  head.style.marginTop = "10px";
  head.innerHTML = `<span class="muted">Force retreat — push a beaten foe back a hex:</span>`;
  c.appendChild(head);
  for (const o of opts) {
    const attacker = figByUid(o.attacker), target = figByUid(o.target);
    const key = o.attacker + ">" + o.target;
    const row = document.createElement("div");
    row.className = "fr-row";
    addBtn(row, `↩ ${attacker ? attacker.name : o.attacker} pushes ${target ? target.name : o.target}`,
      () => act({type: "force_retreat", uid: o.attacker, target: o.target,
                 advance: !!frAdvance[key]}).then(after), true);
    const adv = document.createElement("label");
    adv.className = "fr-adv";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = !!frAdvance[key];
    box.addEventListener("change", () => { frAdvance[key] = box.checked; });
    adv.appendChild(box);
    adv.appendChild(document.createTextNode(" advance (follow up)"));
    row.appendChild(adv);
    c.appendChild(row);
  }
}

// ---- per-character pop-up options menu --------------------------------------
function closeMenu() { $("tokenMenu").style.display = "none"; }
function openMenu(f) {
  const rows = [];
  if (S.phase === "select") {
    // Only the active figure may act; everyone else is a read-only preview.
    if (!isActive(f)) rows.push({label: "Not this figure's turn yet", muted: true});
    else {
      // The full option set: available ones are clickable, unavailable ones are
      // shown disabled with their reason (issue #73), never hidden. do_nothing
      // and pass ride in the list from option_availability; render them as their
      // own dedicated rows below rather than inline.
      for (const o of (optInfo.options || [])) {
        if (o.option === "do_nothing" || o.option === "pass") continue;
        if (o.available === false)
          rows.push({label: optLabel(o.option), reason: o.reason, disabled: true});
        else
          rows.push({label: optLabel(o.option), act: () => chooseMoveOption(f, o.option)});
      }
      rows.push({label: "Do nothing (hold)", act: () => selectDoNothing(f)});
      if (canPass(f)) rows.push({label: "Pass — choose last", act: () => selectPass(f)});
      else rows.push({label: "Pass — choose last", reason: "already deferred", disabled: true});
    }
  } else if (S.phase === "combat") {
    const grappling = (f.hth_opponents || []).length > 0;
    // A readied bow/crossbow shoots; anything else strikes. Label the target rows
    // to match, so a missile attacker's combat step reads "🏹 Shoot <foe>" (#204).
    const shooting = !!(f.weapon && missileReady(f, optInfo));
    // Wizard: a Cast row group parallel to the attack rows (TFT: Wizard, p.11). Pick
    // a spell + target here; a missile spell's ST (mana) is then set with the slider
    // in the combat controls before Resolve. The engine's spell_targets (#362) is the
    // single source of legal targets, mirroring the attack targeting plumbing.
    for (const spell of (optInfo.castable_spells || [])) {
      for (const uid of ((optInfo.spell_targets || {})[spell.id] || [])) {
        const e = figByUid(uid);
        const who = spell.is_protection && uid === f.uid ? "self" : (e ? e.name : uid);
        rows.push({label: `🔮 Cast ${escapeHtml(spell.name)} → ${escapeHtml(who)}`,
                   act: () => setCast(f, spell, uid)});
      }
    }
    for (const uid of (optInfo._targets || [])) {
      const e = figByUid(uid);
      rows.push({label: `${shooting ? "🏹 Shoot" : "⚔ Attack"} ${escapeHtml(e ? e.name : uid)}`,
                 act: () => setAttack(f, uid)});
    }
    // #268: a two-shot bow (high-adjDX archer) may split its arrows between two
    // different foes (p.5, p.10). When the server reports missile_shots >= 2 and
    // there are at least two missile targets in the front arc, offer each unordered
    // pair as a split-fire row — no new picker widget, just one row per pair.
    if (shooting && (optInfo.missile_shots || 1) >= 2) {
      const foes = optInfo.missile_targets || [];
      for (let first = 0; first < foes.length; first++) {
        for (let second = first + 1; second < foes.length; second++) {
          const alpha = figByUid(foes[first]), beta = figByUid(foes[second]);
          rows.push({
            label: `🏹 Split shots: ${escapeHtml(alpha ? alpha.name : foes[first])}`
                 + ` + ${escapeHtml(beta ? beta.name : foes[second])}`,
            act: () => setAttack(f, foes[first], {secondTarget: foes[second]}),
          });
        }
      }
    }
    // #248: a fighter with a Main-Gauche in a free off-hand may add its extra
    // -4 DX jab to a melee attack (p.13). Offer it as a companion attack row for
    // each melee target when the server reports the jab is available.
    if (optInfo.main_gauche_jab) {
      for (const uid of (optInfo.melee_targets || [])) {
        const e = figByUid(uid);
        rows.push({label: `⚔ Attack ${escapeHtml(e ? e.name : uid)} + 🗡 main-gauche jab`,
                   act: () => setAttack(f, uid, {mainGauche: true})});
      }
    }
    // #141: when there's no weapon target, show Attack disabled with the reason
    // (matching the grapple/break-free rows) instead of silently omitting it. A
    // grappler's attack is the Strike row below, so skip it while grappling.
    if (!grappling && !(optInfo._targets || []).length) {
      const missile = !!(f.weapon && missileReady(f, optInfo));
      rows.push({label: missile ? "🏹 Shoot" : "⚔ Attack",
                 reason: missile ? "no target in range" : "no foe in reach", disabled: true});
    }
    for (const uid of (optInfo.hth_targets || [])) {
      const e = figByUid(uid);
      rows.push({label: `🤼 ${grappling ? "Strike" : "Grapple"} ${escapeHtml(e ? e.name : uid)}`,
                 act: () => setHth(f, uid)});
    }
    for (const uid of (optInfo.shield_rush_targets || [])) {
      const e = figByUid(uid);
      rows.push({label: `🛡 Shield-rush ${escapeHtml(e ? e.name : uid)}`,
                 act: () => setShieldRush(f, uid)});
    }
    // Option (n) general disengage: a figure that chose Disengage in the
    // movement phase steps one hex now instead of attacking (p.19).
    for (const dest of (optInfo.disengage_dests || [])) {
      rows.push({label: `💨 Disengage → ${dest}`,
                 act: () => setDisengageMove(f, dest)});
    }
    if (!grappling && !(optInfo.hth_targets || []).length)
      rows.push({label: "🤼 Grapple", reason: "no foe in reach to grapple", disabled: true});
    if (grappling)
      rows.push({label: "💨 Break free (roll)", act: () => setDisengage(f)});
    else
      rows.push({label: "💨 Break free", reason: "not in hand-to-hand", disabled: true});
    // Combat "do nothing" is a real server-side stand-down (hold_fire), not just a
    // local plan entry: only a persisted DO_NOTHING drops the figure from the
    // server's must-attack gate and multi-human resolve-sync, so a remote game can
    // actually resolve (#397/#398). A local-only plan would clear this browser's
    // gate while the server kept waiting on the side.
    rows.push({label: "Hold fire — don't attack", act: () => holdFire(f)});
  } else return;
  const menu = $("tokenMenu");
  const plan = PLAN[f.uid];
  // Header reflects commit state for this phase (issue #72): committed shows what
  // it committed to (+ Clear); uncommitted invites a choice from the options.
  let html = `<div class="head">${escapeHtml(f.name)}`
    + (f.char_class ? ` <span class="muted">— ${escapeHtml(f.char_class)}</span>` : "")
    + ` <span class="chip ${f.side}">${f.side}</span></div>`;
  // A figure with no real choice (only "Do nothing" / disabled rows) is already
  // doing nothing — don't nag it as "uncommitted" (issue #117).
  const hasRealAction = rows.some(r => r.act && r.label !== "Do nothing");
  html += plan
    ? `<div class="commit">Committed: <b>${escapeHtml(plan.label)}</b>${plan.dest ? " → " + escapeHtml(plan.dest) : ""}</div>`
    : hasRealAction
      ? `<div class="commit muted">Uncommitted — choose an action:</div>`
      : `<div class="commit muted">No action available — will do nothing.</div>`;
  rows.forEach((r, i) => {
    const cls = "row" + (r.muted ? " muted" : "") + (r.disabled ? " disabled" : "");
    const why = r.reason ? ` <span class="why">${r.reason}</span>` : "";
    html += `<div class="${cls}" data-i="${i}">${r.label}${why}</div>`;
  });
  if (plan) html += `<div class="sep"></div><div class="row clear" data-clear>Clear action</div>`;
  menu.innerHTML = html;
  menu.style.display = "block";
  const rect = $("svg").getBoundingClientRect(), hx = LAYOUT.hexes[f.label];
  const px = rect.left + (hx.cx / LAYOUT.width) * rect.width + 12;
  const py = rect.top + (hx.cy / LAYOUT.height) * rect.height - 10;
  menu.style.left = Math.min(px, window.innerWidth - menu.offsetWidth - 8) + "px";
  menu.style.top = Math.min(Math.max(8, py), window.innerHeight - menu.offsetHeight - 8) + "px";
  menu.querySelectorAll(".row[data-i]").forEach(el => {
    const r = rows[+el.dataset.i];
    if (r.act && !r.disabled) el.addEventListener("click", () => { closeMenu(); r.act(); });
  });
  const clearRow = menu.querySelector("[data-clear]");
  if (clearRow) clearRow.addEventListener("click", () => { closeMenu(); clearPlan(f); });
}

function chooseMoveOption(f, option) {
  dbg("INTERACT", `chose option ${option} for ${f.name}`, {uid: f.uid, option});
  sel = f.uid; pendingDest = null; pendingReady = null;
  // A weapon change with more than one carried weapon — or a pick-up with more than
  // one dropped weapon in reach (#269) — opens the placement panel so the player
  // explicitly picks which weapon (#142) instead of silently taking the first.
  const choices = readyChoices(f, option);
  if (NEEDS_DEST.has(option) || choices) {
    chosenOption = option; pendingFacing = "auto";
    if (choices) pendingReady = defaultReadyChoice(f, option, choices);
    render();       // enter placement
  } else {
    // Selection phase: a simple option needs no placement, so submit it now (#192).
    let ready = null;
    if (WEAPON_CHANGE.has(option)) ready = (f.weapons || []).find(w => w !== f.weapon) || f.weapon;
    if (option === "pick_up") ready = (optInfo.pickups || [])[0];
    submitMove(f, option, {facing: "auto", ready});
  }
}
function setHth(f, target) {
  const e = figByUid(target);
  dbg("INTERACT", `queue grapple ${f.name} → ${e ? e.name : target}`, {attacker: f.uid, foe: target});
  PLAN[f.uid] = {uid: f.uid, phase: "combat", target, hth: true,
                 label: `🤼 Grapple ${e ? e.name : target}`};
  render();
}
function setDisengage(f) {
  PLAN[f.uid] = {uid: f.uid, phase: "combat", disengage: true, label: "💨 Break free"};
  render();
}
function setShieldRush(f, target) {
  const e = figByUid(target);
  PLAN[f.uid] = {uid: f.uid, phase: "combat", target, rush: true,
                 label: `🛡 Shield-rush ${e ? e.name : target}`};
  render();
}
function setDisengageMove(f, dest) {
  PLAN[f.uid] = {uid: f.uid, phase: "combat", disengageMove: true, dest,
                 label: `💨 Disengage → ${dest}`};
  render();
}
function setAttack(f, target, {mainGauche = false, secondTarget = null} = {}) {
  const e = figByUid(target);
  dbg("INTERACT", `queue attack ${f.name} → ${e ? e.name : target}`,
      {attacker: f.uid, foe: target, mainGauche, secondTarget});
  // The off-hand main-gauche jab (p.13) is an extra -4 DX melee hit riding on the
  // same attack; carry the flag so executePlans can send main_gauche (#248).
  const jab = mainGauche ? " + 🗡 main-gauche jab" : "";
  // A two-shot bow may loose its second arrow at a different foe (p.5, p.10);
  // carry secondTarget so executePlans forwards it to the engine (#268).
  const second = figByUid(secondTarget);
  const split = secondTarget ? ` + 🏹 ${second ? second.name : secondTarget}` : "";
  PLAN[f.uid] = {uid: f.uid, phase: "combat", target, mainGauche, secondTarget,
                 label: `Attack ${e ? e.name : target}${jab}${split}`};
  render();
}
function castLabel(name, who, isMissile, st) {
  return `🔮 Cast ${name} → ${who}` + (isMissile ? ` (ST ${st})` : "");
}
// Queue a wizard's cast (the magic mirror of setAttack). A missile spell invests
// 1..max_st ST (mana) — default to the most it can afford for a strong shot, then
// let the player trim it on the ST slider in the combat controls. A protection/other
// spell is a flat cost, so no slider. A queued cast is NOT a must-attack (a wizard is
// never forced to attack); it satisfies the resolve gate like any set action.
function setCast(f, spell, target) {
  const e = figByUid(target);
  const who = spell.is_protection && target === f.uid ? "self" : (e ? e.name : target);
  const stMin = spell.st_cost;
  const stMax = spell.is_missile ? Math.min(spell.max_st, f.st) : spell.st_cost;
  const stUsed = stMax;
  dbg("INTERACT", `queue cast ${f.name}: ${spell.name} → ${who}`,
      {caster: f.uid, spell: spell.id, target, stUsed});
  PLAN[f.uid] = {uid: f.uid, phase: "combat", cast: true, spell: spell.id,
                 spellName: spell.name, isMissile: !!spell.is_missile,
                 target, castWho: who, stMin, stMax, stUsed,
                 label: castLabel(spell.name, who, spell.is_missile, stUsed)};
  render();
}
// ---- selection phase: immediate submission (#192) ---------------------------
// In the select phase there is no batch: each choice POSTs right away and the
// server lights up the next figure in initiative order.
function isActive(f) { return !!f && S.active_uid === f.uid; }
function hasPassed(f) { return !!f && (S.passed || []).includes(f.uid); }
function canPass(f) { return isActive(f) && !hasPassed(f); }
// #365: the phase-aware "is this figure mine to act on right now, and can it act?"
// predicate — select: the active figure, mine, that can act; combat: any figure of
// mine that can act. The ONE place the two can_act-consulting sites (hoverActionable's
// menu gate, planLine's "click this counter" hint) read the rule, so a phase/gating
// tweak lands once. Deliberately NOT unified with the tracker (figActionHtml gates on
// !dead, not can_act), the inline controls (figControlsHtml/onInlineOption gate on
// isActive&&myTurnActor only), or drawControls combat (consults the server
// combat_actionable set): those consult genuinely different signals (#365 is "risky"
// precisely because the seven sites are not equivalent), so they stay separate.
function figurePhaseActionable(f) {
  if (S.phase === "select") return isActive(f) && myTurnActor(f) && !!f.can_act;
  if (S.phase === "combat") return myTurnActor(f) && !!f.can_act;
  return false;
}
// #365: "the active figure, and mine to act for" — the select-phase gate the inline
// action controls share. figControlsHtml has already early-returned on dead/collapsed/
// acted and onInlineOption only ever fires from that same enabled block, so neither
// needs can_act/!dead here; both express exactly isActive&&myTurnActor.
const isActiveOwnActor = f => isActive(f) && myTurnActor(f);
function selectDoNothing(f) {
  dbg("INTERACT", `do-nothing ${f.name}`, {uid: f.uid});
  closeMenu();
  act({type: "do_nothing", uid: f.uid}).then(after);
}
// #397/#398: stand a committed attacker down in combat (it holds its fire), so a
// figure the player can't or won't target can never leave Resolve disabled forever.
function holdFire(f) {
  dbg("INTERACT", `hold fire ${f.name}`, {uid: f.uid});
  closeMenu();
  delete PLAN[f.uid];                 // drop any local plan for it too
  act({type: "hold_fire", uid: f.uid}).then(after);
}
function selectPass(f) {
  dbg("INTERACT", `pass ${f.name}`, {uid: f.uid});
  closeMenu();
  act({type: "pass", uid: f.uid}).then(after);
}
function submitMove(f, option, {dest = null, facing = "auto", ready = null} = {}) {
  dbg("INTERACT", `submit move ${f.name}: ${option}`, {uid: f.uid, option, dest, facing, ready});
  closeMenu(); chosenOption = null; pendingDest = null; pendingReady = null;
  act({type: "move", uid: f.uid, option, dest, facing, ready}).then(after);
}
function clearPlan(f) {
  delete PLAN[f.uid];
  if (sel === f.uid) { chosenOption = null; pendingDest = null; }
  render();
}
function resetAll() { PLAN = {}; warnKind = null; resetSelection(); closeMenu(); }
// Per-game bookkeeping that must be cleared at every GAME boundary (a new game
// or End Game) so nothing leaks into the next game in the same tab (no reload
// happens between them): a stale combatResolvedTurn makes a fresh game silently
// skip the Resolve step and discard queued attacks (#307), and a stale
// lastPhase/frAdvance carries the old game's phase-change and follow-into
// bookkeeping across. Kept OUT of resetAll(), which also runs mid-combat-turn
// (executePlans) where combatResolvedTurn must survive to offer "End turn".
function resetGameLifecycle() { combatResolvedTurn = -1; lastPhase = null; frAdvance = {}; }

async function executePlans(kind) {
  // The combat phase still batches its attack plans (the selection phase submits
  // each action immediately, so it never routes through here) (#192).
  closeMenu();
  const plans = Object.values(PLAN).filter(p => p.phase === kind);
  for (const p of plans) {
    if (p.disengage) await act({type: "hth_disengage", uid: p.uid});
    else if (p.disengageMove) await act({type: "disengage_move", uid: p.uid, dest: p.dest});
    else if (p.rush) await act({type: "shield_rush", uid: p.uid, target: p.target});
    else if (p.hth) await act({type: "queue_hth", uid: p.uid, target: p.target});
    else if (p.cast) await act({type: "cast_spell", uid: p.uid, spell: p.spell,
                                target: p.target, st: p.stUsed});
    else if (p.target) await act({type: "queue_attack", uid: p.uid, target: p.target,
                                  main_gauche: !!p.mainGauche,
                                  second_target: p.secondTarget || null});
  }
  resetAll(); await act({type: "resolve_combat"}); render();
}

function after() { render(); }

function bannerFor(phase) {
  if (S.victory) return `🏆 ${S.victory.toUpperCase()} wins the field!`;
  return `Turn ${S.turn} · ${PHASE_LABEL[phase] || phase}`;
}

// ---- interaction ------------------------------------------------------------
// Options for a figure, cached for the life of the current state so hovering
// across counters doesn't refetch on every pixel of movement. The cache is
// cleared whenever the state changes (see act/refresh/startGame).
let optCache = {};
// #372: the union of a figure's missile and melee target lists. A throwable melee
// weapon (spear/javelin/axe…) can both strike up close AND be hurled at range, so
// picking missile XOR melee dropped its thrown shot (#217). The two lists are
// disjoint, so the union is clean. ONE definition shared by loadOptions (which
// caches it as ._targets) and maybeAutoTarget (which probes without warming the
// cache) so the two can never diverge on what a committed shooter may aim at.
const combatTargetUnion = info =>
  [...new Set([...(info.missile_targets || []), ...(info.melee_targets || [])])];
async function loadOptions(f) {
  if (optCache[f.uid]) return optCache[f.uid];
  const info = await api(`/api/game/${GID}/options?uid=${f.uid}`);
  // In combat, ._targets holds EVERY foe this figure can attack with its ready
  // weapon: a bow/crossbow's missile targets, a throwable weapon's thrown targets
  // (distant foes), AND its melee targets (adjacent foes) — the union. A throwable
  // melee weapon (spear/javelin/axe…) can both strike up close and be hurled at
  // range, so picking missile XOR melee dropped its thrown shot: a committed
  // thrower then had NO clickable target, PLAN[uid] could never be set, and the
  // must-attack gate left Resolve disabled forever (#217). The two lists are
  // disjoint (thrown = foes out of melee reach), so the union is clean; for a pure
  // bow melee_targets is empty and for a pure melee weapon missile_targets is.
  info._targets = combatTargetUnion(info);
  optCache[f.uid] = info;
  return info;
}

async function onFigureClick(f) {
  flash("");
  // Selecting a figure to INSPECT it (its read-only sheet in the Selected panel)
  // is always allowed -- theirs or an enemy's (#214). ACTING stays gated: the
  // action menu only opens for your own actionable figure. So a figure you can't
  // command is simply inspected, not flashed away.
  const tag = {uid: f.uid, name: f.name, side: f.side, myControlled: myControlled(f),
               actFor: myTurnActor(f), phase: S.phase};
  if (S.phase === "select") {
    if (!isActive(f) || !myTurnActor(f)) { dbg("INTERACT", `figure click ${f.name} → inspect`, tag); inspectFigure(f); return; }
    dbg("INTERACT", `figure click ${f.name} → open-menu`, tag);
    sel = f.uid; chosenOption = null; pendingDest = null; pendingFacing = f.facing; pendingReady = null;
    optInfo = await loadOptions(f);
    render(); openMenu(f);
  } else if (S.phase === "combat") {
    if (!myTurnActor(f)) {
      // #220: a foe click is the natural "pick the target" gesture. If you have a
      // committed shooter still awaiting a target (the must-attack gate), aim its
      // shot at this foe so Resolve can clear — otherwise just inspect the foe.
      if (await queuePendingShotAt(f)) { dbg("INTERACT", `figure click ${f.name} → queue-shot`, tag); return; }
      dbg("INTERACT", `figure click ${f.name} → inspect`, tag);
      inspectFigure(f); return;
    }
    dbg("INTERACT", `figure click ${f.name} → open-menu`, tag);
    sel = f.uid; pendingFacing = f.facing;
    optInfo = await loadOptions(f);
    render(); openMenu(f);
  } else {
    dbg("INTERACT", `figure click ${f.name} → select`, tag);
    sel = f.uid; render();
  }
}

// #372: the resolve-gate's "committed shooter still needs a target" set — figures
// I control that the server flagged as must-attack and that have no queued PLAN yet
// (needsTarget is the same invariant the classifier's untargeted set uses). ONE
// definition shared by the click-to-aim (queuePendingShotAt) and auto-target
// (maybeAutoTarget) paths so they can never disagree on who still owes a shot.
function pendingShooters() {
  const mustAttack = new Set(S.must_attack || []);
  return S.figures.filter(f => myTurnActor(f) && needsTarget(f, mustAttack, PLAN));
}

// #220: queue a committed-but-untargeted shooter's attack at ``enemy`` when the
// player clicks that foe. Mirrors the Resolve gate's own "untargeted" set: a
// figure I control that the server flagged in must_attack and that has no PLAN
// yet. The first such shooter that can actually reach this foe (its combat
// _targets list includes it) takes the shot, so clicking a foe repeatedly assigns
// each pending shooter in turn. Returns true if a shot was queued (so the caller
// skips plain inspection), false if none applied (fall back to inspecting).
async function queuePendingShotAt(enemy) {
  if (!S || S.phase !== "combat" || combatResolvedTurn === S.turn) return false;
  const pending = pendingShooters();
  for (const shooter of pending) {
    const info = await loadOptions(shooter);
    if ((info._targets || []).includes(enemy.uid)) {
      optInfo = info; sel = shooter.uid;
      setAttack(shooter, enemy.uid);   // sets PLAN[shooter] + re-renders the gate
      return true;
    }
  }
  return false;
}

// #299: auto-queue the shot for a committed must-attack figure that has exactly
// ONE legal target, so the player needn't click it. This preserves the resolve
// gate's guarantees (#212/#217/#220): a figure with more than one target still
// needs an explicit pick (we skip it), a figure with no target never blocks (the
// server keeps it out of must_attack), and only the exactly-one case auto-fills.
// The queued target shows in the plan/checklist and stays clearable — no
// transient UI. It is enemies-only: _targets is already the server's enemy-only
// list, so the #229 friendly-fire guard is untouched. An off-hand main-gauche
// jab is a genuine second choice, so a figure that could add one is left for the
// player to decide rather than auto-committing the plain blow.
let _autoTargetBusy = false;
async function maybeAutoTarget() {
  if (_autoTargetBusy) return;
  if (!S || S.phase !== "combat" || combatResolvedTurn === S.turn) return;
  const pending = pendingShooters();
  if (!pending.length) return;
  _autoTargetBusy = true;
  let queued = false;
  try {
    for (const shooter of pending) {
      // Probe options WITHOUT warming optCache (loadOptions would): this is a
      // background pass, and a pre-warmed cache changes the timing of the hover/
      // click menu-open elsewhere. Reuse an already-cached entry when present, but
      // never write one. ``_targets`` mirrors loadOptions' union of the missile and
      // melee target lists (a throwable weapon can both strike and be hurled).
      const info = optCache[shooter.uid]
        || await api(`/api/game/${GID}/options?uid=${shooter.uid}`);
      const targets = combatTargetUnion(info);
      if (targets.length === 1 && !info.main_gauche_jab) {
        // Set the plan directly — this is a background pass, so it must NOT hijack
        // the player's current selection (sel) or cached options (optInfo); doing
        // so would yank an open token menu out from under them. setAttack reads
        // neither, so a bare call is safe and keeps one source of truth for the
        // plan shape/label.
        setAttack(shooter, targets[0]);   // sets PLAN[shooter] + re-renders the gate
        queued = true;
      }
    }
  } finally {
    _autoTargetBusy = false;
  }
  if (queued) render();
}

// Select a figure purely to view its sheet: no action menu, no placement flow.
// Clears any in-progress placement so the arena doesn't try to draw reach hexes
// for a figure that isn't the one you're acting with (#214).
function inspectFigure(f) {
  sel = f.uid; chosenOption = null; pendingDest = null; pendingReady = null;
  render();
}

// ---- hover popup (issue #72) -----------------------------------------------
// Hovering an actionable counter surfaces its action popup, reflecting commit
// state, without a click. A short grace timer keeps it from flickering as the
// pointer travels from the token to the popup.
let hoverCloseTimer = null;
function cancelHoverClose() {
  if (hoverCloseTimer) { clearTimeout(hoverCloseTimer); hoverCloseTimer = null; }
}
function scheduleHoverClose() {
  cancelHoverClose();
  hoverCloseTimer = setTimeout(closeMenu, 220);
}
function hoverActionable(f) {
  if (!S || S.victory || chosenOption) return false;   // not mid-placement
  return figurePhaseActionable(f);
}
async function onFigureHover(f) {
  cancelHoverClose();
  if (!hoverActionable(f)) return;
  const info = await loadOptions(f);
  if (!hoverActionable(f)) return;     // state may have changed during the await
  optInfo = info; sel = f.uid; pendingFacing = f.facing;
  openMenu(f);
}

function missileReady(f, info) {
  // A figure shoots (bow/crossbow) rather than strikes when the server reports
  // its readied weapon is a missile weapon (#272). Derive it from that flag, not
  // a client-side copy of the weapon taxonomy.
  info = info || optInfo;
  return !!(f && f.weapon && info && info.is_missile);
}

function onReachClick(label) {
  pendingDest = label; render();
}

// Click anywhere off the menu (and off a token, which stops propagation) closes it.
document.addEventListener("click", (e) => {
  const menu = $("tokenMenu");
  if (menu.style.display === "block" && !menu.contains(e.target)) closeMenu();
});

// Keep the hover popup open while the pointer is over it; close shortly after
// the pointer leaves both the token and the popup (issue #72).
$("tokenMenu").addEventListener("mouseenter", cancelHoverClose);
$("tokenMenu").addEventListener("mouseleave", scheduleHoverClose);

// ---- side panels ------------------------------------------------------------
// Who may edit a figure inline, mirroring the server's update_figure rule: an
// admin edits any live figure at any time (#323); during the pre-game setup
// lobby (#399) a seat owner edits the figures of sides they hold and the HOST
// edits ANY figure (including a computer side's). Regular players in a running
// game view a read-only sheet.
const canEditFigure = f => IS_ADMIN
  || (!!S && S.phase === "setup" && (IS_HOST || myControlled(f)));

function drawSelInfo() {
  const box = $("selInfo");
  const f = sel ? figByUid(sel) : null;
  if (!f) {
    INLINE_EDIT_FOR = null;
    box.innerHTML = `<span class="muted">No figure selected.</span>`;
    return;
  }
  // Poll-clobber guard (#323): while an admin has an edit card mounted for THIS
  // figure, the 2s poll must not rebuild the panel -- that would drop keystrokes
  // and focus. Refresh only the read-only header region and leave the live card be.
  if (INLINE_EDIT_FOR === f.uid && box.querySelector(".card")) {
    const header = box.querySelector("[data-selheader]");
    if (header) header.innerHTML = statusHeader(f) + charSheetHtml(f) + planLine(f);
    return;
  }
  // Poll-clobber guard for the save-character rename field (#339): a logged-in
  // NON-admin retyping a colliding name has a live <input> the admin guard above
  // doesn't cover (INLINE_EDIT_FOR is admin-only). While that input is focused,
  // skip the innerHTML rebuild and refresh only the read-only header, so a poll
  // tick during typing can't drop the caret/focus (mirrors the admin path).
  const renameField = box.querySelector(".savechar-name");
  if (renameField && document.activeElement === renameField) {
    const header = box.querySelector("[data-selheader]");
    if (header) header.innerHTML = statusHeader(f) + charSheetHtml(f) + planLine(f);
    return;
  }
  INLINE_EDIT_FOR = null;
  box.innerHTML = `<div data-selheader>`
    + statusHeader(f) + charSheetHtml(f) + planLine(f) + `</div>`;
  // A figure you don't command shows the read-only sheet above but no controls
  // (#214) — unless the setup lobby's edit rule grants you the card (#399: the
  // host edits any figure, e.g. a computer side's, which myControlled denies).
  if ((!myControlled(f) && !canEditFigure(f)) || !f.edit_spec) return;
  // Keep this fighter: a signed-in player may snapshot a fighter they control
  // into their saved characters, straight from the running game (#234). Stays
  // gated on control: the server's save_character authz is seat-based.
  if (LOGGED_IN && myControlled(f)) box.appendChild(saveCharacterUi(f));
  // Inline editing: admin always; seat owner/host during the setup lobby (#399).
  if (!canEditFigure(f)) return;
  if (!CAT || !RULES || CAT.profile !== PROFILE) { ensureGameCatalog(); return; }
  // The editor is built inline in this panel from the same chargen card the setup
  // wizard uses, so the #298 two-handed-shield handling is preserved; Apply posts
  // update_figure and re-renders. It replaced the old #liveEdit modal (#181/#323).
  box.appendChild(inlineEditCard(f));
}

// ---- keep a fighter: save it from the game to your account (#234) -----------
// Per-figure save status, keyed by uid. The panel is state-driven — it renders
// from this map on every pass, so nothing here is a transient toast: "saved"
// stays shown until the page reloads, and a name collision renders a persistent
// inline rename prompt that survives re-renders (the typed name lives here too).
const SAVE_CHAR_UI = {};   // uid -> {saved} | {renameTo, error}

function saveCharacterUi(f) {
  const ui = SAVE_CHAR_UI[f.uid];
  const wrap = document.createElement("div");
  wrap.className = "savechar"; wrap.style.marginTop = "10px";
  if (ui && ui.saved) {
    wrap.innerHTML = `<span class="muted">Saved to your characters as `
      + `“${escapeHtml(ui.saved)}” ✓</span>`;
    return wrap;
  }
  if (ui && ui.error) {
    const problem = document.createElement("div");
    problem.className = "muted"; problem.style.color = "var(--target)";
    problem.textContent = ui.error;
    const rename = document.createElement("input");
    rename.className = "savechar-name"; rename.maxLength = 80;
    rename.value = ui.renameTo; rename.style.marginRight = "6px";
    rename.addEventListener("input", () => { ui.renameTo = rename.value; });
    const retry = document.createElement("button");
    retry.textContent = "Save as";
    retry.addEventListener("click", () => saveCharacterFromGame(f, rename.value));
    problem.style.marginBottom = "4px";
    wrap.appendChild(problem); wrap.appendChild(rename); wrap.appendChild(retry);
    return wrap;
  }
  const save = document.createElement("button");
  save.textContent = "💾 Save character";
  save.addEventListener("click", () => saveCharacterFromGame(f, f.name));
  wrap.appendChild(save);
  return wrap;
}

async function saveCharacterFromGame(f, name) {
  const requestedName = String(name == null ? f.name : name).trim();
  if (!requestedName) return;
  const data = await postJSON(
    `/api/game/${GID}/figure/${f.uid}/save_character`, {name: requestedName});
  if (data.error) {
    dbg("SAVE", `save character ${f.name} refused`, {uid: f.uid, error: data.error});
    SAVE_CHAR_UI[f.uid] = {renameTo: requestedName, error: data.error};
  } else {
    dbg("SAVE", `saved character ${data.name}`, {uid: f.uid, id: data.id});
    SAVE_CHAR_UI[f.uid] = {saved: data.name};
    const idx = SAVED.findIndex(c => c.id === data.id);   // keep the wizard list fresh
    if (idx >= 0) SAVED[idx] = data; else SAVED.push(data);
  }
  render();
}

// The uid an inline edit card is currently mounted for, so the poll guard in
// drawSelInfo knows not to clobber a live card (#323, mirrors the old LIVE_EDIT_FOR).
let INLINE_EDIT_FOR = null;
function tokenBadge(f) {   // the same numbered disc the board draws, for matching
  return `<span class="tokenbadge" style="background:${fillFor(f.side)}">`
    + `${f.dead ? "✗" : hpCur(f)}</span>`;
}
function weaponsLine(f) {
  const ready = f.weapon || "—";
  const reserve = (f.weapons || []).filter(w => w !== f.weapon);
  const reloading = f.reloading > 0
    ? ` <span style="color:var(--target)">— reloading (${f.reloading})</span>` : "";
  return `<div class="muted">In hand: <b>${escapeHtml(ready)}</b>${reloading}`
    + (reserve.length ? ` · ready to switch: ${reserve.map(escapeHtml).join(", ")}` : "") + `</div>`;
}
function statusHeader(f) {
  const classLine = f.char_class
    ? `<div class="muted">${escapeHtml(f.char_class)}</div>` : "";
  return `<div>${tokenBadge(f)} <b>${escapeHtml(f.name)}</b> <span class="chip ${f.side}">${f.side}</span></div>` +
    classLine +
    (f.model === "tarmar"
      ? `<div class="muted">Fatigue ${f.fatigue}/${f.max_fatigue} · Body ${f.body}/${f.max_body} · adjDX ${f.dx}</div>`
      : `<div class="muted">ST ${f.st}/${f.max_st} · adjDX ${f.dx}</div>`) +
    `<div class="muted">${f.posture}${f.engaged ? " · engaged" : ""}${f.dodging ? " · dodging" : ""}${f.defending ? " · defending" : ""}` +
    `${(f.hth_opponents && f.hth_opponents.length) ? " · 🤼 grappling" : ""}</div>` +
    weaponsLine(f);
}

// The read-only character sheet shown for ANY selected figure (#214): ST/DX, the
// full carried kit with the readied weapon marked, armor, and shield. Every field
// here is in the state sent to all clients, so it works for enemies too -- it never
// reads edit_spec (the owner/admin-only editable spec).
function shieldState(f) {
  // The wire format sends the shield name only while it's up; slung or absent
  // both come through as null, so that's as fine as we can distinguish read-only.
  return f.shield ? `${escapeHtml(f.shield)} (up)` : "none / slung";
}
function charSheetHtml(f) {
  const isTarmar = f.model === "tarmar";
  const readied = f.weapon || null;
  const carried = f.weapons || [];
  // Readied weapon first and clearly marked, then the rest of the kit (Dagger etc.).
  const ordered = readied
    ? [readied, ...carried.filter(w => w !== readied)]
    : carried.slice();
  // Tarmar shows the trained skill (0-5) per carried weapon, read from the public
  // wire field (never edit_spec, per the #214 read-only contract).
  const weaponSkills = f.weapon_skills || {};
  const skillTag = name => (isTarmar && name in weaponSkills)
    ? ` <span class="muted">— skill ${weaponSkills[name]}</span>` : "";
  const weaponItems = ordered.length
    ? ordered.map(w => `<li>${escapeHtml(w)}`
        + (readied && w === readied ? ` <span class="readied">— readied</span>` : "")
        + skillTag(w) + `</li>`).join("")
    : `<li class="muted">unarmed</li>`;
  const vitals = isTarmar
    ? `Fatigue ${f.fatigue}/${f.max_fatigue} · Body ${f.body}/${f.max_body} · DX ${f.dx}`
    : `ST ${f.st}/${f.max_st} · DX ${f.dx}`;
  // Surface the full attribute spread for EVERY figure (#323). Classic already
  // shows its complete ST/DX set in the vitals; Tarmar adds its four extra
  // attributes here so opponents' sheets are as complete as your own.
  const attrs = isTarmar
    ? `<div class="sheet-sub">Attributes</div>`
      + `<div class="sheet-attrs">ST ${f.max_st} · DX ${f.dx} · IQ ${f.intelligence}`
      + ` · WIS ${f.wisdom} · CON ${f.constitution} · CHA ${f.charisma}</div>`
    : "";
  const armor = (f.armor && f.armor !== "None") ? escapeHtml(f.armor) : "none";
  return `<div class="charsheet">`
    + `<div class="sheet-vitals">${vitals}</div>`
    + attrs
    + wizardSheetHtml(f)
    + `<div class="sheet-sub">Weapons</div>`
    + `<ul class="sheet-weapons">${weaponItems}</ul>`
    + `<div class="sheet-gear">Armor: <b>${armor}</b> · Shield: <b>${shieldState(f)}</b></div>`
    + `</div>`;
}

// The wizard block of the character sheet (Classic magic): ST framed as the
// spell-power / mana gauge (ST doubles as mana, p.3-4), the spells known, and any
// active continuing spell (Stone Flesh) with the hit-stopping it grants. Empty for a
// non-wizard, so a fighter's sheet is unchanged.
function wizardSheetHtml(f) {
  if (!f.is_wizard) return "";
  const known = (f.spells_known || []).map(spellDisplayName);
  const active = Object.keys(f.active_spells || {});
  const activeLine = active.length
    ? `<div class="sheet-sub">Active spells</div><div class="muted">`
      + active.map(id => escapeHtml(spellDisplayName(id))).join(", ")
      + (f.spell_protection ? ` · stops ${f.spell_protection}/attack` : "")
      + `</div>`
    : "";
  return `<div class="sheet-sub">🔮 Mana (ST)</div>`
    + `<div class="mana-gauge">${f.mana}/${f.max_mana}`
    + ` <span class="muted">· IQ ${f.intelligence}</span></div>`
    + `<div class="sheet-sub">Spells known</div>`
    + `<div class="muted">${known.length ? known.map(escapeHtml).join(", ") : "none"}</div>`
    + activeLine;
}

// Catalog for the *running* game's profile (the editor may have loaded another).
let gameCatBusy = false;
async function ensureGameCatalog() {
  if (gameCatBusy || !PROFILE || (CAT && RULES && CAT.profile === PROFILE)) return;
  gameCatBusy = true;
  try {
    // A rejected fetch (network blip, 500) must not leave gameCatBusy stuck true
    // -- that permanently blocks every retry and hides the live-edit button
    // (#272). Reset the flag in finally; leave CAT/RULES unset so the next render
    // retries the load.
    const catalog = await api(`/api/catalog?profile=${encodeURIComponent(PROFILE)}`);
    CAT = catalog;
    RULES = catalog.stat_rules;
  } finally {
    gameCatBusy = false;
  }
  render();
}

function inlineEditCard(f) {
  const card = document.createElement("div"); card.className = "card";
  card.dataset.side = f.side;
  card.innerHTML = cardInner(f.edit_spec);
  card.addEventListener("input", () => refreshCard(card));
  card.addEventListener("change", () => refreshCard(card));
  const apply = document.createElement("button");
  apply.className = "primary"; apply.textContent = "Apply to game";
  apply.addEventListener("click", () => applyEdit(card, f.uid));
  card.appendChild(apply);
  INLINE_EDIT_FOR = f.uid;      // arm the poll guard while this card is mounted
  setTimeout(() => refreshCard(card), 0);
  return card;
}
async function applyEdit(card, uid) {
  const data = await act({type: "update_figure", uid, spec: readCard(card)});
  // Clear the guard so the re-render rebuilds the card from the applied spec.
  if (data) { flash("Applied changes."); INLINE_EDIT_FOR = null; render(); }
}
function planLine(f) {
  const p = PLAN[f.uid];
  if (p) return `<div style="margin-top:8px" class="muted">Action set: <b>${escapeHtml(p.label)}</b>`
    + `${p.dest ? " → " + escapeHtml(p.dest) : ""}</div>`;
  if (S.phase === "select" && f.acted)
    return `<div style="margin-top:8px" class="muted">Action set: <b>${optLabel(f.option)}</b></div>`;
  if (S.phase === "select" && hasPassed(f) && !f.acted)
    return `<div style="margin-top:8px" class="muted">Passed — waiting to choose last.</div>`;
  if (figurePhaseActionable(f))
    return `<div style="margin-top:8px" class="muted">Click this counter on the board for its options.</div>`;
  return "";
}

// The Characters tracker: every figure grouped by side (player), each row
// showing its name, condition, and the action it has chosen this phase. Sides
// list in initiative order when the engine exposes a winner/mover this phase,
// otherwise in their stable order (PR 1 reflects today's flow -- no reordering
// of the engine itself; that is PR 2). (#192)
function orderedSides() {
  // Group by player as in the mockup, ordering the players by their best figure's
  // place in the frozen initiative order (#192).
  const sides = (S.sides && S.sides.length)
    ? S.sides.slice() : Object.keys(S.controllers || {});
  const order = S.initiative_order || [];
  const rank = side => {
    const idx = order.findIndex(uid => {
      const fig = figByUid(uid);
      return fig && fig.side === side;
    });
    return idx < 0 ? order.length : idx;
  };
  return sides.slice().sort((left, right) => rank(left) - rank(right));
}
// Figures within a side's group, ordered by the frozen initiative order (#192).
function sideFiguresInInitiative(figs) {
  const order = S.initiative_order || [];
  const place = f => { const i = order.indexOf(f.uid); return i < 0 ? order.length : i; };
  return figs.slice().sort((left, right) => place(left) - place(right));
}
function figActionHtml(f) {
  const plan = PLAN[f.uid];                        // combat phase still batches
  if (plan) return `<span class="action">${escapeHtml(plan.label)}`
    + `${plan.dest ? " → " + escapeHtml(plan.dest) : ""}</span>`;
  if (S.phase === "select") {
    if (f.acted) return `<span class="action">${escapeHtml(optLabel(f.option))}</span>`;
    if (hasPassed(f)) return `<span class="action passed">Passed — waiting</span>`;
    if (isActive(f) && myTurnActor(f) && !f.dead)
      return `<span class="action todo">choose action</span>`;
    return `<span class="action idle">—</span>`;
  }
  const canFight = S.phase === "combat" && myTurnActor(f) && f.can_act;
  if (canFight && !f.dead) return `<span class="action todo">choose action</span>`;
  return `<span class="action idle">—</span>`;
}
// The action-selection control block for a character (#198/#199/#202), now rendered
// in the Action panel for the character whose turn it is (#326). Instead of a
// "Choose action → popup" indirection, the FULL list of this figure's actions is
// listed: the active, controllable figure's valid options are live (clicking one
// specifies it directly), and its invalid options are greyed with the server's
// reason. A figure that has already acted, passed (and isn't up last), or is dead
// yields no block. (drawActionActor only ever calls this for the active, owned
// figure, so the block is always the enabled variant.)
function figControlsHtml(f) {
  if (S.phase !== "select") return "";
  if (f.dead || f.collapsed || f.acted) return "";
  // A passer that isn't up yet shows its "Passed — waiting" badge, not a control
  // block; once it comes up last to choose, isActive is true and it gets the
  // enabled block again (with Pass disabled -- it's already deferred).
  if (hasPassed(f) && !isActive(f)) return "";
  const enabled = isActiveOwnActor(f);
  // Mid-placement: the active figure that chose a destination-requiring option
  // shows its placement confirm right here, under its own row -- reach hexes light
  // up on the board and Set action sits inches from the counter, not in a distant
  // panel (the #200 regression that made destination options look inert, #202).
  if (enabled && chosenOption && sel === f.uid)
    return `<div class="charctl enabled placing" data-ctl="${escapeHtml(f.uid)}">`
      + placementInnerHtml(f) + `</div>`;
  const info = enabled ? optCache[f.uid] : null;
  return `<div class="charctl ${enabled ? "enabled" : "disabled"}" data-ctl="${escapeHtml(f.uid)}">`
    + optionListHtml(f, info, enabled) + `</div>`;
}
// The greyed preview list shown under figures whose turn hasn't come up yet: we
// don't fetch every figure's live availability, so the block is a disabled
// stand-in. do_nothing/pass always tail the real list too (they ride in the
// server's option_availability, so an enabled list already carries them).
const PREVIEW_OPTIONS = ["move", "half_move", "charge_attack", "dodge",
  "ready_weapon", "missile_attack", "stand_up", "do_nothing", "pass"];
function optionListHtml(f, info, enabled) {
  let opts;
  if (enabled && info && info.options) {
    opts = info.options.map(o => ({option: o.option, available: o.available,
      reason: o.reason, attack: o.is_attack}));
  } else if (enabled) {                       // active but options still loading
    return `<div class="place-hint">Loading actions…</div>`;
  } else {                                    // greyed preview of the coming turn
    opts = PREVIEW_OPTIONS.map(o => ({option: o, available: false, reason: null, attack: false}));
  }
  return `<div class="opt-list">` + opts.map(o => {
    // An "illegal" option is one the ACTIVE figure genuinely can't take right now
    // (the server flagged available === false with a reason). Those get the ⊘ mark,
    // the visible reason pill, and a title= tooltip on top. A greyed preview block
    // (another figure's coming turn) is disabled wholesale but carries no reason.
    const illegal = enabled && o.available === false;
    const dis = (!enabled || o.available === false) ? " disabled" : "";
    const why = (illegal && o.reason)
      ? `<span class="why">${escapeHtml(o.reason)}</span>` : "";
    const mark = illegal ? `<span class="opt-mark" aria-hidden="true">⊘</span>` : "";
    const tip = illegal && o.reason ? ` title="${escapeHtml(o.reason)}"` : "";
    return `<button class="opt${o.attack ? " attack" : ""}${illegal ? " illegal" : ""}"`
      + ` data-opt="${escapeHtml(o.option)}"${dis}${tip}>`
      + `<span class="opt-label">${mark}${escapeHtml(optLabel(o.option))}</span>${why}</button>`;
  }).join("") + `</div>`;
}
// The inline placement confirm for a destination-requiring option: destination +
// facing summary, turn controls, and a Set action button gated until a reach hex
// is picked (a weapon-change instead offers a Ready-weapon selector). Mirrors the
// submit path the board popup used, kept inline so it is where the player is.
function placementInnerHtml(f) {
  const needHex = NEEDS_DEST.has(chosenOption);
  const destOptional = DEST_OPTIONAL.has(chosenOption);
  const choices = readyChoices(f, chosenOption);
  if (choices && pendingReady == null)
    pendingReady = defaultReadyChoice(f, chosenOption, choices);
  const facingTxt = pendingFacing === "auto" ? "→ enemy" : pendingFacing;
  const destTxt = pendingDest || (destOptional ? "none (fire in place)" : "—");
  let html = `<div class="place-head">Placing <b>${escapeHtml(optLabel(chosenOption))}</b>`
    + (needHex ? ` · dest ${escapeHtml(destTxt)}` : "")
    + ` · facing ${escapeHtml(String(facingTxt))}</div>`;
  if (choices) {
    const pickLabel = chosenOption === "pick_up" ? "Pick up" : "Ready";
    html += `<div style="margin:2px 0">${pickLabel}: <select data-ready>`
      + choices.map(weaponName =>
          `<option ${weaponName === pendingReady ? "selected" : ""}>${escapeHtml(weaponName)}</option>`).join("")
      + `</select></div>`;
  }
  if (needHex && !pendingDest && destOptional)
    // Option (f): the 1-hex step is optional, so make firing from here obvious.
    html += `<div class="place-hint">Optionally click a green hex to move up to 1 hex`
      + ` first, or press <b>Set action</b> to fire from where you stand. You'll pick`
      + ` the target in the next (combat) step.</div>`;
  else if (needHex && !pendingDest)
    html += `<div class="place-hint">Click a green hex on the board to set the destination.</div>`;
  // A required-move option stays gated until a hex is picked; an optional-move one
  // (missile fire) can be set straight away — firing without moving (#204).
  const setDis = (needHex && !pendingDest && !destOptional) ? " disabled" : "";
  return html + `<div class="place-btns">`
    + `<button data-act="turnccw">⟲ turn</button>`
    + `<button data-act="turncw">⟳ turn</button>`
    + `<button class="primary" data-act="setaction"${setDis}>Set action</button>`
    + `<button data-act="cancel">Cancel</button></div>`;
}
// A click on an inline option under the active character. Simple options submit at
// once (like the board popup's do_nothing/pass did); destination-requiring ones
// enter the inline placement step (reach hexes on the board + Set action here).
function onInlineOption(f, option) {
  if (!isActiveOwnActor(f)) return;
  optInfo = optCache[f.uid] || optInfo;      // reach data for placement + drawArena
  sel = f.uid;
  if (option === "do_nothing") selectDoNothing(f);
  else if (option === "pass") selectPass(f);
  else chooseMoveOption(f, option);
}
function onPlacementAct(f, action) {
  const facingNow = () => pendingFacing === "auto" ? f.facing : pendingFacing;
  if (action === "turnccw") { pendingFacing = (facingNow() + 5) % 6; render(); }
  else if (action === "turncw") { pendingFacing = (facingNow() + 1) % 6; render(); }
  else if (action === "cancel") { chosenOption = null; pendingDest = null; pendingReady = null; render(); }
  else if (action === "setaction") {
    const choices = readyChoices(f, chosenOption);
    submitMove(f, chosenOption,
               {dest: pendingDest, facing: pendingFacing, ready: choices ? pendingReady : null});
  }
}
// Ensure the active, controllable figure's options (availability + reach) are
// loaded so its inline list shows real enabled/disabled state; the load is async
// (options are cached for the life of the state, cleared on every act/refresh),
// so re-draw the roster once it arrives.
let activeOptsBusy = null;
async function ensureActiveOptions() {
  if (!S || S.phase !== "select" || S.victory) return;
  const active = S.active_uid ? figByUid(S.active_uid) : null;
  if (!active || active.dead || !myTurnActor(active)) return;
  if (optCache[active.uid] || activeOptsBusy === active.uid) return;
  activeOptsBusy = active.uid;
  await loadOptions(active);
  activeOptsBusy = null;
  if (S && S.active_uid === active.uid) drawControls();   // options feed the Action panel (#326)
}
// The active character's action-selection block, rendered into the Action panel's
// #controls (#326): a name header (side chip + token + name) over the option list
// or, mid-placement, the destination/facing confirm. Reuses figControlsHtml so the
// #202 inline option/placement flow is preserved verbatim -- only its home moved.
function drawActionActor(container, active) {
  const head = document.createElement("div");
  head.className = "action-actor";
  head.innerHTML = `<span class="chip ${active.side}">${escapeHtml(sideName(active.side))}</span> `
    + tokenBadge(active) + ` ${escapeHtml(active.name)}`;
  container.appendChild(head);
  const block = document.createElement("div");
  block.innerHTML = figControlsHtml(active);
  container.appendChild(block);
  wireCharCtl(block);
}
// Wire an action-control block's option buttons, placement buttons, and Ready-weapon
// selector. Shared so the block behaves identically wherever it is mounted (#326).
function wireCharCtl(scope) {
  scope.querySelectorAll(".charctl[data-ctl]").forEach(block => {
    const f = figByUid(block.dataset.ctl);
    if (!f) return;
    block.querySelectorAll("button[data-opt]").forEach(btn => {
      if (btn.disabled) return;
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation(); onInlineOption(f, btn.dataset.opt); });
    });
    block.querySelectorAll("button[data-act]").forEach(btn => {
      if (btn.disabled) return;
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation(); onPlacementAct(f, btn.dataset.act); });
    });
    const readySel = block.querySelector("select[data-ready]");
    if (readySel) readySel.addEventListener("change", (e) => { pendingReady = e.target.value; });
  });
}
function drawRoster() {
  const r = $("roster"); if (!r || !S) return;
  const byside = {};
  for (const f of S.figures) (byside[f.side] = byside[f.side] || []).push(f);
  let html = adminTagHtml();
  for (const side of orderedSides()) {
    html += `<div class="grouphd"><span class="chip ${side}">${escapeHtml(sideName(side))}</span>`
      + ` ${seatTag(side)}</div>`;
    for (const f of sideFiguresInInitiative(byside[side] || [])) {
      const dead = f.dead || f.collapsed;
      const state = f.dead ? "dead" : f.collapsed ? "down"
        : `${hpCur(f)}/${hpMax(f)}` + (f.posture !== "standing" ? " · " + f.posture : "");
      // Highlight the figure whose turn it is; dim the rest during selection.
      const active = S.phase === "select" && isActive(f);
      const waiting = S.phase === "select" && hasPassed(f) && !f.acted;
      const cls = "row" + (dead ? " dead" : "") + (active ? " active" : "")
        + (waiting ? " waiting" : "")
        + (S.phase === "select" && !active && !f.acted && !dead ? " disabled" : "");
      // At-a-glance kit (#214): each row shows its readied weapon and DX so every
      // character can be scanned without clicking. Compact -- wraps under the name.
      const kit = `<span class="kit muted">⚔ ${escapeHtml(f.weapon || "unarmed")} · DX ${f.dx}</span>`;
      const classTag = f.char_class
        ? `<span class="muted">— ${escapeHtml(f.char_class)}</span> ` : "";
      // The roster is list + selection only now (#326): the action-selection
      // controls moved to the Action panel. Each row still shows its chosen-action
      // status column (figActionHtml) so the tracker stays a full at-a-glance board.
      html += `<div class="${cls}" data-uid="${escapeHtml(f.uid)}">`
        + `<span class="rowmain">${tokenBadge(f)} ${escapeHtml(f.name)} ${classTag}`
        + `<span class="muted">${state}</span>${kit}</span>`
        + figActionHtml(f) + `</div>`;
    }
  }
  html += inviteHtml();
  r.innerHTML = html;
  r.querySelectorAll(".row[data-uid]").forEach(row => {
    const f = figByUid(row.dataset.uid);
    if (f) row.addEventListener("click", () => onFigureClick(f));
  });
}

function drawLog() {
  const l = $("log"); l.innerHTML = "";
  for (const line of S.log.slice().reverse()) {
    const d = document.createElement("div"); d.textContent = line;
    // Emphasize blows/missiles that land. A crushing blow (crit) gets the
    // strongest treatment; a normal connecting hit a lighter one. Misses,
    // dodges, armour-absorbed hits, and movement stay at normal weight.
    if (line.includes("crushing blow")) d.className = "log-crit";
    else if (line.includes("connects for")) d.className = "log-hit";
    l.appendChild(d);
  }
}

function addBtn(parent, text, fn, primary, disabled) {
  const b = document.createElement("button");
  b.textContent = text; if (primary) b.className = "primary";
  if (disabled) b.disabled = true; else b.addEventListener("click", fn);
  parent.appendChild(b); return b;
}

// ---- pre-match fighter editor ----------------------------------------------
const ED_TEAMS = ["red", "blue", "green", "gold", "violet"];
const ARCHETYPES = {
  // A generated fighter starts with a hand weapon AND a missile weapon, matching
  // the engine archetypes (scenario.py) and best_weapons — not two hand weapons.
  // The MISSILE weapon is the primary (readied) one so the fighter can fire on
  // turn 1 without first switching weapons (#204); the melee weapon rides as
  // weapon2 and can be readied when the fight closes.
  "Classic Melee": [
    {name:"Knight", strength:13, dexterity:11, weapon:"Light crossbow", weapon2:"Broadsword", armor:"Plate", shield:"Large shield"},
    {name:"Swordsman", strength:12, dexterity:12, weapon:"Longbow", weapon2:"Shortsword", armor:"Chainmail", shield:"Small shield"},
    {name:"Spearman", strength:13, dexterity:11, weapon:"Longbow", weapon2:"Spear", armor:"Leather", shield:"None"},
  ],
  "Tarmar": [
    {name:"Knight", strength:13, dexterity:11, intelligence:10, wisdom:10, constitution:11, charisma:10, weapon:"Light crossbow", weapon2:"Broadsword", armor:"Plate", shield:"Large shield", skill:1, skill2:3},
    {name:"Swordsman", strength:12, dexterity:12, intelligence:10, wisdom:10, constitution:11, charisma:10, weapon:"Longbow", weapon2:"Shortsword", armor:"Chainmail", shield:"Small shield", skill:1, skill2:3},
    {name:"Spearman", strength:13, dexterity:11, intelligence:10, wisdom:10, constitution:10, charisma:10, weapon:"Longbow", weapon2:"Spear", armor:"Leather", shield:"None", skill:1, skill2:2},
  ],
};

// The minimal demo wizard (Gate 2). ST + DX + IQ = 32, each >= 8 (the 3-attribute
// wizard spread, TFT: Wizard p.3-4). IQ 13 is chosen deliberately so the preset can
// field BOTH gate-2 spells: Magic Fist is IQ 8 (a starter wizard could cast it) but
// Stone Flesh is IQ 13. A default IQ-8 wizard could only field Magic Fist — this
// spell-tier point is flagged for Spencer. Casts bare-handed (no ready weapon/shield,
// p.23), so `cast_block_reason` clears it to cast from turn 1. The full live-gated
// spell-picker is Gate 3; this is the one-button minimal path to field a wizard.
const WIZARD_ARCHETYPE = {
  name: "Wizard", char_class: "Wizard",
  strength: 9, dexterity: 10, intelligence: 13,
  armor: "None", spells: ["magic_fist", "stone_flesh"],
};
let CAT = null, RULES = null;
const isWizardCard = card => !!card.querySelector("[data-spells]");
// A spell's display name from the running game's catalog, with a prettified-id
// fallback so a spectator without the catalog still sees a readable name.
function spellDisplayName(id) {
  const fromCat = CAT && CAT.spells && CAT.spells.find(s => s.id === id);
  return fromCat ? fromCat.name
    : String(id).replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}
// A spell's cost as the picker shows it: type letter + ST cost (a range when a
// missile spell can be powered up, 1..max_st), the spell analogue of a weapon's
// damage/str_req shown next to each weapon option.
function spellCostText(spell) {
  const cost = (spell.max_st && spell.max_st > spell.st_cost)
    ? `${spell.st_cost}–${spell.max_st} ST` : `${spell.st_cost} ST`;
  return `${spell.type}, ${cost}`;
}

function buildRoster(profile, teams, perTeam) {
  const tmpl = ARCHETYPES[profile] || ARCHETYPES["Classic Melee"];
  const wizards = wizardsMode();
  const roster = [];
  for (let t = 0; t < teams; t++)
    for (let i = 0; i < perTeam; i++) {
      // Wizards mode seeds the LAST seat on each side as a wizard (the rest
      // fighters), matching the engine's build_game roster so the editable start
      // and the preset start produce the same shape.
      const spec = (wizards && i === perTeam - 1)
        ? Object.assign({}, WIZARD_ARCHETYPE)
        : Object.assign({}, tmpl[i % tmpl.length]);
      spec.side = ED_TEAMS[t];
      roster.push(spec);
    }
  return roster;
}

const rint = (a, b) => a + Math.floor(Math.random() * (b - a + 1));
// Ask the server for the most *effective* melee + missile weapon (expected damage
// = hit-chance x damage, so a heavy/under-strength weapon is discounted in Tarmar).
async function setWeapons(card, strength, dexterity, skill) {
  const p = encodeURIComponent(chosenProfile());
  const data = await api(`/api/best_weapons?profile=${p}&strength=${strength}`
    + `&dexterity=${dexterity}&skill=${skill}`);
  if (data.melee) card.querySelector('[data-eq="weapon"]').value = data.melee;
  if (data.missile) card.querySelector('[data-eq="weapon2"]').value = data.missile;
  refreshCard(card);
}
// Replace an editor card in place with a wizard preset (keeps its side + name), so
// clicking "🔮 Wizard" turns any card into the demo wizard. Rebuilds the whole card
// (not just fields) because the wizard layout differs from the fighter one.
function makeWizardCard(card) {
  const current = readCard(card);
  const spec = Object.assign({}, WIZARD_ARCHETYPE,
    {side: card.dataset.side, name: current.name || "Wizard"});
  card.replaceWith(fighterCard(spec, 0));
}
function generateWizardInto(card) {   // randomize a wizard's ST/DX/IQ (sum 32, each >= 8)
  const fields = ["strength", "dexterity", "intelligence"];
  const vals = {strength: 8, dexterity: 8, intelligence: 8};
  let points = 32 - 24;               // 8 free points over the 3x8 base
  let guard = 0;
  while (points > 0 && guard++ < 500) {
    const field = fields[rint(0, 2)];
    if (vals[field] < 20) { vals[field]++; points--; }
  }
  fields.forEach(field => {
    const input = card.querySelector(`[data-stat="${field}"]`);
    if (input) input.value = vals[field];
  });
  refreshCard(card);
}
function generateInto(card) {       // randomize this fighter within the rules
  if (isWizardCard(card)) { generateWizardInto(card); return; }
  if (RULES.model === "tarmar") {
    let pts = RULES.budget - RULES.fields.length * RULES.min;
    const vals = {}; RULES.fields.forEach(f => vals[f] = RULES.min);
    let guard = 0;
    while (pts > 0 && guard++ < 2000 && !RULES.fields.every(x => vals[x] >= RULES.max)) {
      const f = RULES.fields[rint(0, RULES.fields.length - 1)];
      if (vals[f] < RULES.max) { vals[f]++; pts--; }
    }
    RULES.fields.forEach(f => card.querySelector(`[data-stat="${f}"]`).value = vals[f]);
    card.querySelectorAll("[data-skillkey]").forEach(i => i.value = rint(0, RULES.skill_max));
    const skill = parseInt(card.querySelector("[data-skillkey]")?.value || "0", 10);
    setWeapons(card, vals.strength || RULES.min, vals.dexterity || RULES.min, skill);
  } else {
    const st = rint(RULES.min, RULES.total - RULES.min);
    card.querySelector('[data-stat="strength"]').value = st;
    card.querySelector('[data-stat="dexterity"]').value = RULES.total - st;
    setWeapons(card, st, RULES.total - st, 0);
  }
  refreshCard(card);
}

async function openEditor() {
  const profile = chosenProfile();
  CAT = await api(`/api/catalog?profile=${encodeURIComponent(profile)}`);
  RULES = CAT.stat_rules;
  if (LOGGED_IN) {
    SAVED = (await api(`/api/characters?profile=${encodeURIComponent(profile)}`)).characters || [];
  }
  const teams = PLAYERS.length, perTeam = parseInt($("perTeam").value, 10);
  const wrap = $("editorRoster"); wrap.innerHTML = "";
  buildRoster(profile, teams, perTeam).forEach((f, i) => wrap.appendChild(fighterCard(f, i)));
  $("editorErr").textContent = LOGGED_IN ? "" : "Log in to save and reuse characters.";
  closeSetup();                       // the editor replaces the setup panel
  $("editor").style.display = "flex";
}
function closeEditor() { $("editor").style.display = "none"; EDIT_FOR_USER = null; }

// ---- admin powers: users + their saved characters (#140; staff only) -------
let ADMIN_USERS = [];        // last-loaded user list (for id -> name lookup)
let ADMIN_SEL = null;        // {id, username} whose characters are being managed
let EDIT_FOR_USER = null;    // when set, the fighter editor's Save targets this user

function openAdmin() {
  $("admin").style.display = "flex"; $("adminErr").textContent = "";
  $("adminChars").innerHTML = ""; ADMIN_SEL = null; adminLoadUsers();
}
function closeAdmin() { $("admin").style.display = "none"; }

async function adminLoadUsers() {
  const data = await api("/api/admin/users");
  ADMIN_USERS = data.users || [];
  $("adminUsers").innerHTML = ADMIN_USERS.map(u => `
    <div class="adminRow">
      <button class="link" onclick="adminSelectUser(${u.id})">${escapeHtml(u.username)}</button>
      <span class="muted">${u.is_staff ? "admin · " : ""}${u.character_count} character(s)</span>
      <button onclick="adminDeleteUser(${u.id})" title="Delete user">🗑</button>
    </div>`).join("") || `<div class="muted">No users.</div>`;
}

function _adminName(id) { const u = ADMIN_USERS.find(x => x.id === id); return u ? u.username : "user"; }

async function adminCreateUser() {
  const username = $("adminNewUser").value.trim();
  const password = $("adminNewPass").value;
  const is_staff = $("adminNewStaff").checked;
  const data = await postJSON("/api/admin/users", {username, password, is_staff});
  if (data.error) { $("adminErr").textContent = data.error; return; }
  $("adminErr").textContent = `Created “${data.username}”.`;
  $("adminNewUser").value = ""; $("adminNewPass").value = ""; $("adminNewStaff").checked = false;
  adminLoadUsers();
}

async function adminDeleteUser(id) {
  if (!confirm(`Delete “${_adminName(id)}”? This also removes their saved characters.`)) return;
  const data = await postJSON(`/api/admin/users/${id}/delete`, {});
  if (data.error) { $("adminErr").textContent = data.error; return; }
  if (ADMIN_SEL && ADMIN_SEL.id === id) { ADMIN_SEL = null; $("adminChars").innerHTML = ""; }
  adminLoadUsers();
}

async function adminSelectUser(id) {
  ADMIN_SEL = {id, username: _adminName(id)};
  const data = await api(`/api/admin/users/${id}/characters`);
  const rows = (data.characters || []).map(c => `
    <div class="adminRow">${escapeHtml(c.name)} <span class="muted">${escapeHtml(c.profile)}</span>
      <button onclick="adminDeleteChar(${c.id})" title="Delete character">🗑</button>
    </div>`).join("") || `<div class="muted">No saved characters.</div>`;
  $("adminChars").innerHTML =
    `<div style="margin-top:12px"><b>${escapeHtml(ADMIN_SEL.username)}’s characters</b>
       <button onclick="adminCreateCharFor()">＋ new character</button></div>${rows}`;
}

async function adminDeleteChar(pk) {
  const data = await postJSON(`/api/admin/characters/${pk}/delete`, {});
  if (data.error) { $("adminErr").textContent = data.error; return; }
  if (ADMIN_SEL) adminSelectUser(ADMIN_SEL.id);
  adminLoadUsers();                 // keep the per-user counts current
}

function adminCreateCharFor() {
  if (!ADMIN_SEL) return;
  EDIT_FOR_USER = ADMIN_SEL;        // the fighter editor's Save will target this user
  closeAdmin();
  openEditor();
}

function savedCharacterOptions() {   // was loadOptions — collided with the game
  return `<option value="">Load saved…</option>`   // options fetch (issue #115)
    + SAVED.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join("");
}
function applySpecToCard(card, spec) {        // fill a card from a saved spec (keep its team)
  if (spec.name != null) card.querySelector("[data-name]").value = spec.name;
  card.querySelectorAll("[data-stat]").forEach(i => { if (spec[i.dataset.stat] != null) i.value = spec[i.dataset.stat]; });
  card.querySelectorAll("[data-eq]").forEach(s => { if (spec[s.dataset.eq] != null) s.value = spec[s.dataset.eq]; });
  card.querySelectorAll("[data-skillkey]").forEach(i => { if (spec[i.dataset.skillkey] != null) i.value = spec[i.dataset.skillkey]; });
  const shieldReady = card.querySelector("[data-shieldready]");
  if (shieldReady && spec.shield_ready != null) shieldReady.checked = spec.shield_ready;
  // A saved wizard round-trips its spell picks into the checkbox list, the mirror
  // of filling the weapon selects above.
  if (Array.isArray(spec.spells)) {
    const known = new Set(spec.spells);
    card.querySelectorAll("[data-spell]").forEach(
      box => box.checked = known.has(box.dataset.spell));
  }
  refreshCard(card);
}
async function saveCharacter(card) {
  const spec = readCard(card);
  // An admin building a character for a player (#140) saves to that user's
  // collection; otherwise it's the signed-in player's own save.
  const url = EDIT_FOR_USER ? `/api/admin/users/${EDIT_FOR_USER.id}/characters` : "/api/characters";
  const data = await postJSON(url, {name: spec.name, profile: chosenProfile(), spec});
  if (data.error) { $("editorErr").textContent = "Save failed: " + data.error; return; }
  if (EDIT_FOR_USER) {
    $("editorErr").textContent = `Saved “${data.name}” to ${escapeHtml(EDIT_FOR_USER.username)}.`;
    return;
  }
  const idx = SAVED.findIndex(c => c.id === data.id);
  if (idx >= 0) SAVED[idx] = data; else SAVED.push(data);
  $("editorErr").textContent = `Saved “${data.name}”.`;
  $("editorRoster").querySelectorAll("select.loadsel").forEach(s => s.innerHTML = savedCharacterOptions());
}

function optionTags(list, chosen) {
  return list.map(o => `<option ${o.name === chosen ? "selected" : ""}>${o.name}</option>`).join("");
}
function skillInput(key, value) {
  return RULES.model === "tarmar"
    ? `<label>skill <input type="number" data-skillkey="${key}" value="${value || 0}" `
      + `min="0" max="${RULES.skill_max}" style="width:46px"></label>` : "";
}
function cardInner(f) {     // the editable fields shared by the editor and the live panel
  if (Array.isArray(f.spells) && f.spells.length) return wizardCardInner(f);
  const stats = RULES.fields.map(field =>
    `<label>${field.slice(0,3).toUpperCase()} <input type="number" data-stat="${field}" value="${f[field]}" `
    + `min="${RULES.min || 1}" max="${RULES.max || 30}" style="width:52px"></label>`).join(" ");
  return `<div><span class="chip ${f.side}">${f.side}</span> `
    + `<input data-name value="${escapeHtml(f.name)}" style="width:130px"></div>`
    + `<div style="margin-top:6px">${stats} <span class="muted" data-budget></span></div>`
    + `<div style="margin-top:6px">Carried weapon <select data-eq="weapon">${optionTags(CAT.weapons, f.weapon)}</select> ${skillInput("skill", f.skill)}</div>`
    + `<div style="margin-top:6px">Carried weapon 2 <select data-eq="weapon2"><option ${!f.weapon2 || f.weapon2 === "None" ? "selected" : ""}>None</option>${optionTags(CAT.weapons, f.weapon2)}</select> ${skillInput("skill2", f.skill2)}</div>`
    + `<div style="margin-top:6px">Readied weapon <select data-readied></select> `
    + `<span class="muted">— starts in hand</span></div>`
    + `<div style="margin-top:6px">Armour <select data-eq="armor">${optionTags(CAT.armors, f.armor || "None")}</select> `
    + `Shield <select data-eq="shield">${optionTags(CAT.shields, f.shield || "None")}</select></div>`
    + `<div style="margin-top:6px" data-shieldready-row>`
    + `<label><input type="checkbox" data-shieldready ${f.shield_ready === false ? "" : "checked"}> Shield readied</label> `
    + `<span class="muted" data-shieldready-note></span></div>`
    + `<div class="hint" data-err></div>`;
}
// A wizard's editor card: ST/DX/IQ spread + armour + a spell picker. Spells are
// picked exactly like a fighter's weapons — a catalog-driven control (one checkbox
// per spell in CAT.spells) whose legality is gated by an attribute: weapons gate on
// ST (disableByStrength), spells gate on IQ (disableSpellsByIq). No weapon/shield
// picker — a wizard casts bare-handed (p.23). The [data-spells] container is also
// the flag isWizardCard() keys on, and readCard reads the checked boxes from it.
function wizardCardInner(f) {
  const iq = f.intelligence || 8;
  const known = new Set(f.spells || []);
  const stat = (field, label, value) =>
    `<label>${label} <input type="number" data-stat="${field}" value="${value}" `
    + `min="8" max="30" style="width:52px"></label>`;
  const spellRows = (CAT.spells || []).map(spell =>
    `<label class="spellpick" style="display:block">`
    + `<input type="checkbox" data-spell="${spell.id}" `
    + `${known.has(spell.id) ? "checked" : ""}> ${escapeHtml(spell.name)} `
    + `<span class="muted">(IQ ${spell.iq_tier}, ${escapeHtml(spellCostText(spell))})</span>`
    + `</label>`).join("");
  return `<div><span class="chip ${f.side}">${f.side}</span> `
    + `<input data-name value="${escapeHtml(f.name)}" style="width:130px"> `
    + `<span class="muted">— 🔮 Wizard</span></div>`
    + `<div style="margin-top:6px">${stat("strength", "ST", f.strength)} `
    + `${stat("dexterity", "DX", f.dexterity)} ${stat("intelligence", "IQ", iq)} `
    + `<span class="muted" data-budget></span></div>`
    + `<div style="margin-top:6px">Armour <select data-eq="armor">`
    + `${optionTags(CAT.armors, f.armor || "None")}</select></div>`
    + `<div style="margin-top:6px" data-spells>`
    + `<div class="muted">Spells known <span data-spellcount></span> · casts bare-handed</div>`
    + spellRows + `</div>`
    + `<div class="hint" data-err></div>`;
}
function fighterCard(f, side_i) {
  const card = document.createElement("div"); card.className = "card";
  card.dataset.side = f.side;
  card.innerHTML = cardInner(f);
  card.addEventListener("input", () => refreshCard(card));
  card.addEventListener("change", () => refreshCard(card));
  const gen = document.createElement("button");
  gen.textContent = "🎲 Generate";
  gen.addEventListener("click", () => generateInto(card));
  card.appendChild(gen);
  // One-button minimal wizard fielding (Gate 2): swap this card for a wizard preset
  // so a wizard can be started from the setup editor and cast in the browser.
  const wiz = document.createElement("button");
  wiz.textContent = "🔮 Wizard";
  wiz.addEventListener("click", () => makeWizardCard(card));
  card.appendChild(wiz);
  if (LOGGED_IN) {
    const save = document.createElement("button");
    save.textContent = "💾 Save";
    save.addEventListener("click", () => saveCharacter(card));
    card.appendChild(save);
    const load = document.createElement("select");
    load.className = "loadsel";
    load.innerHTML = savedCharacterOptions();
    load.addEventListener("change", () => {
      const c = SAVED.find(x => String(x.id) === load.value);
      if (c) applySpecToCard(card, c.spec);
      load.value = "";
    });
    card.appendChild(load);
  }
  setTimeout(() => refreshCard(card), 0);
  return card;
}

function readCard(card) {
  const f = {side: card.dataset.side, name: card.querySelector("[data-name]").value};
  card.querySelectorAll("[data-stat]").forEach(i => f[i.dataset.stat] = parseInt(i.value || "0", 10));
  card.querySelectorAll("[data-eq]").forEach(s => f[s.dataset.eq] = s.value);
  card.querySelectorAll("[data-skillkey]").forEach(i => f[i.dataset.skillkey] = parseInt(i.value || "0", 10));
  // The spec's `weapon` is the readied weapon (== ready_weapon). The player picks
  // which carried weapon starts in hand (#207); when that's the second carried
  // weapon, swap the two slots (and their skills) so `weapon` is the readied one.
  const readied = card.querySelector("[data-readied]");
  if (readied && readied.value && readied.value === f.weapon2 && f.weapon2 !== "None") {
    [f.weapon, f.weapon2] = [f.weapon2, f.weapon];
    [f.skill, f.skill2] = [f.skill2, f.skill];
  }
  const shieldReady = card.querySelector("[data-shieldready]");
  if (shieldReady) f.shield_ready = shieldReady.checked;
  // A wizard card picks its spells from the [data-spells] checkbox list, exactly as
  // a fighter picks weapons from the [data-eq] selects (chargen keys "is this a
  // wizard?" on a non-empty spells list). A wizard casts bare-handed, so its
  // weapon/shield fields stay empty (chargen._validate_wizard).
  const spellsEl = card.querySelector("[data-spells]");
  if (spellsEl) {
    f.spells = Array.from(card.querySelectorAll("[data-spell]:checked"))
                    .map(box => box.dataset.spell);
    f.weapon = "None"; f.weapon2 = "None"; f.shield = "None";
  }
  return f;
}

function disableByStrength(select, strength, offset) {
  CAT.weapons.forEach((w, idx) => {
    const opt = select.options[idx + offset];
    if (opt) opt.disabled = (w.str_req || 0) > strength;
  });
}
// The spell analogue of disableByStrength: a spell whose IQ tier exceeds the
// wizard's IQ can't be known, so grey out its checkbox and drop it if it was
// somehow checked (an IQ that just dropped below a picked spell's tier).
function disableSpellsByIq(card, intelligence) {
  card.querySelectorAll("[data-spell]").forEach(box => {
    const spell = (CAT.spells || []).find(s => s.id === box.dataset.spell);
    const tooHigh = !!spell && (spell.iq_tier || 0) > intelligence;
    box.disabled = tooHigh;
    if (tooHigh) box.checked = false;
  });
}

function syncEquipControls(card) {
  // Keep the "Readied weapon" dropdown in step with the two carried-weapon
  // selects, and show "Shield readied" only when it's meaningful (#207).
  const readiedSel = card.querySelector("[data-readied]");
  if (!readiedSel) return;
  const primary = card.querySelector('[data-eq="weapon"]');
  const secondary = card.querySelector('[data-eq="weapon2"]');
  const carried = primary ? [primary.value] : [];
  if (secondary && secondary.value && secondary.value !== "None"
      && secondary.value !== (primary && primary.value)) {
    carried.push(secondary.value);
  }
  // Default the readied weapon to the primary carried one (the current default);
  // keep the player's pick when it's still a carried weapon.
  const want = carried.includes(readiedSel.value) ? readiedSel.value : carried[0];
  readiedSel.innerHTML = carried.map(
    name => `<option ${name === want ? "selected" : ""}>${escapeHtml(name)}</option>`).join("");

  const shieldSel = card.querySelector('[data-eq="shield"]');
  const row = card.querySelector("[data-shieldready-row]");
  const box = card.querySelector("[data-shieldready]");
  const shieldNote = card.querySelector("[data-shieldready-note]");
  if (row && box) {
    const hasShield = !!(shieldSel && shieldSel.value && shieldSel.value !== "None");
    row.style.display = hasShield ? "" : "none";
    const readiedWeapon = CAT.weapons.find(w => w.name === want);
    const twoHanded = !!(readiedWeapon && readiedWeapon.two_handed);
    // A two-handed weapon in hand needs both hands, so the shield can't be up.
    if (twoHanded) {
      box.checked = false; box.disabled = true;
      if (shieldNote) shieldNote.textContent = "two-handed weapon — shield slung";
    } else {
      box.disabled = false;
      if (shieldNote) shieldNote.textContent = "";
    }
  }
}

function refreshCard(card) {
  syncEquipControls(card);
  const f = readCard(card);
  let note = "", err = "";
  if (isWizardCard(card)) {
    // A wizard spends ST + DX + IQ = 32, each >= 8 (the 3-attribute spread) and
    // picks spells like weapons: gate the checkboxes by IQ (the spell analogue of
    // gating weapons by ST), then read the surviving picks and enforce the same
    // rules chargen._validate_wizard does — at most IQ spells, at least one.
    const iq = f.intelligence || 0;
    disableSpellsByIq(card, iq);
    const spells = Array.from(card.querySelectorAll("[data-spell]:checked"))
                        .map(box => box.dataset.spell);
    for (const id of spells) {
      const spell = (CAT.spells || []).find(s => s.id === id);
      if (spell && (spell.iq_tier || 0) > iq) err = `${spell.name} needs IQ ${spell.iq_tier}`;
    }
    if (spells.length > iq) err = `a wizard may know at most IQ (${iq}) spells`;
    if (spells.length === 0) err = err || "pick at least one spell";
    const count = card.querySelector("[data-spellcount]");
    if (count) count.textContent = `${spells.length}/${iq}`;
    const total = (f.strength || 0) + (f.dexterity || 0) + (f.intelligence || 0);
    note = `ST+DX+IQ ${total}/32` + (total !== 32 ? " — must equal 32" : "");
    card.querySelector("[data-budget]").textContent = note;
    card.querySelector("[data-err]").textContent = err;
    return;
  }
  if (RULES.model === "tarmar") {
    const total = RULES.fields.reduce((s, k) => s + (f[k] || 0), 0);
    note = `points ${total}/${RULES.budget}` + (total > RULES.budget ? " — over budget" : "");
  } else {
    const total = (f.strength || 0) + (f.dexterity || 0);
    const st = f.strength || 0;
    note = `ST+DX ${total}/${RULES.total}` + (total !== RULES.total ? ` — must equal ${RULES.total}` : "");
    disableByStrength(card.querySelector('[data-eq="weapon"]'), st, 0);
    disableByStrength(card.querySelector('[data-eq="weapon2"]'), st, 1);  // None is option 0
    for (const name of [f.weapon, f.weapon2]) {
      if (name && name !== "None") {
        const w = CAT.weapons.find(x => x.name === name);
        if (w && (w.str_req || 0) > st) err = `${name} needs ST ${w.str_req}`;
      }
    }
  }
  card.querySelector("[data-budget]").textContent = note;
  card.querySelector("[data-err]").textContent = err;
}

async function startCustom() {
  const fighters = Array.from($("editorRoster").children).map(readCard);
  const computer = computerSides();   // the AI players' sides, from the roster (#192)
  const open = openSides();           // the Remote players' sides -> lobby (#399)
  const practice = $("practiceMode") && $("practiceMode").checked;
  const body = {profile: chosenProfile(), computer, open, fighters, practice};
  const data = await api("/api/game/new_custom", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)});
  if (data.error) { $("editorErr").textContent = "Can't start: " + data.error; return; }
  GID = data.gid; LAYOUT = data.layout; S = data.state; PROFILE = data.profile;
  captureOwnership(data); history.replaceState({}, "", `/game/${GID}`);
  optCache = {}; _lastStateJSON = ""; _lastBoardSig = null; _lastRev = null;
  closeEditor(); closeSetup(); resetAll(); resetGameLifecycle(); render();
  startPolling();                 // re-arm live polling for the new game (#308)
  // Lock Game Control just like startGame does (#255): a custom match is a live
  // game, so profile/roster edits and New Game must be disabled and End Game live.
  GAME_ACTIVE = true; syncGameControl();
}

// Theming: a chosen PRESET (see window.MELEE_THEMES in board.html) sets every
// design token at once; on top of that, each corner swatch can override one or
// more tokens as a per-user "Custom" tweak. Both the active preset name and any
// swatch overrides are remembered in localStorage and re-applied on load (the
// preset before first paint, in board.html's <head>). The swatch->token map is
// the single source shared with that pre-paint script.
const THEME = window.MELEE_SWATCH_VARS;
const cleanHex = v => { v = (v || "").trim(); return /^#[0-9a-f]{6}$/i.test(v) ? v : null; };
function luminance(hex) {
  const m = cleanHex(hex); if (!m) return 0;
  const c = [1, 3, 5].map(i => parseInt(m.slice(i, i + 2), 16) / 255)
    .map(x => x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}
function contrast(a, b) {
  const la = luminance(a), lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}
const root = () => document.documentElement;
function syncMuted() {  // grey text tracks the text colour so it stays visible
  root().style.setProperty("--muted", getComputedStyle(root()).getPropertyValue("--ink").trim());
}
function ensureTextContrast() {  // keep text readable against the chosen background
  const cs = getComputedStyle(root());
  const bg = cs.getPropertyValue("--bg").trim(), ink = cs.getPropertyValue("--ink").trim();
  if (contrast(ink, bg) < 4) {
    const good = luminance(bg) > 0.45 ? "#181818" : "#f4f4f4";
    root().style.setProperty("--ink", good);
    localStorage.setItem("melee.theme.textColor", good);
    $("textColor").value = good;
    syncMuted();                                     // a forced ink change drags muted along
  } else if (localStorage.getItem("melee.theme.textColor")) {
    syncMuted();                                     // a custom text colour keeps muted matched to it
  }
  // With no custom text override the preset's own --muted is respected as-is.
}
function syncSwatchInputs() {  // point each colour input at the value now in effect
  const cs = getComputedStyle(root());
  for (const [id, vars] of Object.entries(THEME)) {
    const saved = localStorage.getItem("melee.theme." + id);
    const current = cleanHex(saved) || cleanHex(cs.getPropertyValue(vars[0]));
    if (current) $(id).value = current;
  }
}
function populateThemePicker() {
  const picker = $("themePicker");
  if (!picker) return;
  for (const name of Object.keys(window.MELEE_THEMES)) {
    const option = document.createElement("option");
    option.value = name; option.textContent = name;
    picker.appendChild(option);
  }
  picker.value = window.meleeActivePreset();
  picker.addEventListener("change", () => {
    localStorage.setItem("melee.theme.preset", picker.value);
    window.meleeApplyPreset(picker.value);   // re-layers any saved swatch overrides on top
    syncSwatchInputs();
    ensureTextContrast();
  });
}
function applyTheme() {
  // The preset (+ any saved swatches) is already applied pre-paint by the <head>
  // script; here we sync the inputs and wire live editing of the custom swatches.
  syncSwatchInputs();
  for (const [id, vars] of Object.entries(THEME)) {
    $(id).addEventListener("input", () => {
      const value = $(id).value;
      vars.forEach(v => root().style.setProperty(v, value));
      localStorage.setItem("melee.theme." + id, value);
      ensureTextContrast();
    });
  }
  populateThemePicker();
  ensureTextContrast();
}
function resetTheme() {
  // Drop the custom swatch tweaks and fall back to the ACTIVE preset (not the CSS
  // :root default) -- reset returns to the chosen theme, minus per-user overrides.
  ["bgColor", "textColor", "hexColor"].forEach(id => localStorage.removeItem("melee.theme." + id));
  window.meleeApplyPreset(window.meleeActivePreset());
  syncSwatchInputs();
}

applyTheme();

// ---- draggable panels (#319 Stage 1 move+snap; #321 Stage 2 resize+controls) --
// The four UI panels start as flex children (see .wrap in board.html). At load we
// measure their current geometry -- those measurements ARE the default layout, so
// nothing shifts -- then flip .wrap into .floating (each panel position:absolute)
// and let the user drag any panel by its .panel-titlebar. Positions/sizes persist
// to localStorage and survive reloads; Reset layout restores the defaults.
// Below 1100px we stay in the stacked flex layout (no floating, no persistence).
//
// Stage 2 adds, per panel: drag-to-resize (edge + corner handles) and four
// titlebar controls -- Fit-to-content, Minimize/Expand, Maximize/Restore -- driven
// by a per-panel sizing-mode state machine:
//   content    (default) auto-fit to the inner content's natural size, re-fitting
//              whenever the content changes (a MutationObserver on each panel).
//   manual     a drag-resize froze the size; auto-fit stops until Fit is pressed.
//   maximized  filling the available .wrap area; Restore returns to the saved geom.
//   minimized  collapsed to the titlebar; Expand returns to the saved geom.
// The pure helpers (clampGeom / mergeLayout / snapGeom / fitGeom / resizeGeom /
// snapResizeGeom) live in layout_geom.js so they are unit-testable in isolation (#366).
const LAYOUT_KEY = "melee.layout.v3";  // {key:{x,y,w,h,mode,restoreGeom}} (#326: fighter->action panel split)
const LAYOUT_KEY_V2 = "melee.layout.v2";  // prior shape (map/log/control/tracker/fighter) -- cleared on reset
const LAYOUT_KEY_V1 = "melee.layout.v1";  // Stage 1 {key:{x,y,w,h}}
// LAYOUT_SNAP_PX / LAYOUT_MIN_VISIBLE / LAYOUT_MODES now live in layout_geom.js
// alongside the pure helpers that use them (#366).
const LAYOUT_RESIZE_MIN_W = 96;        // smallest width a drag-resize allows
const LAYOUT_Z_BASE = 10;              // bring-to-front band: 10..40, below overlays@50
const LAYOUT_Z_MAX = 40;
const LAYOUT_RESIZE_DIRS = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];
// Registry of the draggable panels. All drag/resize/persist/reset/snap/fit logic
// iterates this, so behaviour is uniform rather than special-cased per panel.
const LAYOUT_PANELS = [
  {key: "map",     selector: ".arena",     label: "Map"},
  {key: "log",     selector: ".logcol",    label: "Game status"},
  {key: "control", selector: "#gameControl", label: "Game Control"},
  // The Character panel (roster list + selected sheet) and the Action panel
  // (phase prompt + the active character's action controls) share one column,
  // split top/bottom (#323/#326). They default to "manual" (a fixed, bounded slot
  // that scrolls internally) rather than "content": a content-mode roster auto-
  // grows to its full height and, stacked above the Action panel, would overflow
  // and cover its click targets (and vice-versa). Bounding both keeps the split
  // stable. The `tracker` KEY is kept (its CSS class + saved-layout slot) though
  // the panel is now labelled "Character".
  {key: "tracker", selector: ".tracker",   label: "Character", defaultMode: "manual"},
  {key: "action",  selector: ".action",    label: "Action", defaultMode: "manual"},
];
// defaultModeFor now lives in layout_geom.js (imported above) alongside mergeLayout,
// its only cross-module consumer (#366).
const LAYOUT_NARROW = window.matchMedia("(max-width: 1100px)");
let DEFAULT_LAYOUT = null;             // {key: {x,y,w,h}} measured from the wide flex flow
let defaultsMeasuredWide = false;      // true once DEFAULT_LAYOUT reflects a real wide measurement (#338)
let layoutZTop = LAYOUT_Z_BASE;        // monotonic front-most z within the band
let layoutSaveTimer = null;
const resizeMinH = panel => (panel.handle ? Math.round(panel.handle.offsetHeight) : 32);

const layoutStacked = () => LAYOUT_NARROW.matches;
const layoutWrap = () => document.querySelector(".wrap");

function wrapBounds() {
  const wrap = layoutWrap();
  return {width: wrap.clientWidth, height: wrap.clientHeight};
}

// The pure layout geometry/state helpers — clampGeom, sanitizeRestore, mergeLayout,
// nearestSnapDelta, snapLines, snapGeom, fitGeom, maximizeGeom, resizeGeom,
// snapResizeGeom — moved to layout_geom.js (imported at the top of this file) so
// they can be unit-tested directly under `node --test` (#366/#367). Their behaviour
// is unchanged; only mergeLayout gains an explicit `panels` argument (was the
// module-global LAYOUT_PANELS) to keep it pure.

function measureDefaults(wrap) {
  const wrapRect = wrap.getBoundingClientRect();
  const defaults = {};
  for (const panel of LAYOUT_PANELS) {
    const rect = panel.el.getBoundingClientRect();
    defaults[panel.key] = {
      x: Math.round(rect.left - wrapRect.left),
      y: Math.round(rect.top - wrapRect.top),
      w: Math.round(rect.width),
      h: Math.round(rect.height),
    };
  }
  return defaults;
}

function getInlineGeom(panel) {
  const parsePx = value => parseFloat(value) || 0;
  const style = panel.el.style;
  const fallback = DEFAULT_LAYOUT[panel.key];
  return {
    x: style.left ? parsePx(style.left) : fallback.x,
    y: style.top ? parsePx(style.top) : fallback.y,
    w: style.width ? parsePx(style.width) : fallback.w,
    h: style.height ? parsePx(style.height) : fallback.h,
  };
}

function applyGeom(panel, geom) {
  const style = panel.el.style;
  style.left = Math.round(geom.x) + "px";
  style.top = Math.round(geom.y) + "px";
  style.width = Math.round(geom.w) + "px";
  style.height = Math.round(geom.h) + "px";
}

function clearInlineGeom(panel) {
  const style = panel.el.style;
  style.left = style.top = style.width = style.height = style.zIndex = "";
}

function parseStoredLayout(raw) {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    return (parsed && typeof parsed === "object") ? parsed : null;
  } catch (parseError) {
    // Corrupt JSON -> fall back to defaults (documented behaviour), and note it
    // so a real storage problem isn't silently invisible.
    dbg("LAYOUT", "ignoring corrupt saved layout: " + parseError.message);
    return null;
  }
}

// Load the v3 layout. The Character/Action split (#326) reshaped the panel set
// (`fighter` became `action`), so there is no clean field-by-field migration from
// v2/v1 -- following the #323 precedent, a fresh version key means a ONE-TIME reset
// to measured defaults. Return {} when no v3 layout exists; the next save writes the
// new five-panel shape (with `action`), so this only fires once per browser.
function loadSavedLayout() {
  return parseStoredLayout(localStorage.getItem(LAYOUT_KEY)) || {};
}

function saveLayout() {
  if (layoutStacked()) return;   // never persist positions while stacked
  clearTimeout(layoutSaveTimer);
  layoutSaveTimer = setTimeout(() => {
    const out = {};
    for (const panel of LAYOUT_PANELS) {
      out[panel.key] = {...getInlineGeom(panel), mode: panel.mode, restoreGeom: panel.restore || null};
    }
    localStorage.setItem(LAYOUT_KEY, JSON.stringify(out));
  }, 120);
}

function bringToFront(panel) {
  layoutZTop += 1;
  if (layoutZTop > LAYOUT_Z_MAX) {
    // Renormalise back into the band by current stacking order so we never drift
    // above the overlay/menu layers.
    const ordered = LAYOUT_PANELS.slice().sort((a, b) =>
      (parseInt(a.el.style.zIndex || LAYOUT_Z_BASE, 10)) -
      (parseInt(b.el.style.zIndex || LAYOUT_Z_BASE, 10)));
    layoutZTop = LAYOUT_Z_BASE;
    for (const other of ordered) other.el.style.zIndex = ++layoutZTop;
  }
  panel.el.style.zIndex = layoutZTop;
}

function reclampAll() {
  const bounds = wrapBounds();
  for (const panel of LAYOUT_PANELS) {
    // A maximized panel must keep FILLING the wrap across a viewport resize.
    // clampGeom only slides x/y (it passes w/h through untouched), so re-clamping
    // the stale geometry captured when the panel was maximized would leave it
    // under/overflowing the resized wrap. Re-derive the fill geometry from the
    // CURRENT bounds instead (#343). This also covers applyResponsiveLayout, which
    // ends by calling reclampAll — so a maximized panel restored from localStorage
    // is re-fitted to the current wrap too.
    const geom = panel.mode === "maximized" ? maximizeGeom(bounds) : getInlineGeom(panel);
    applyGeom(panel, clampGeom(geom, bounds));
  }
}

// Measure a panel's natural content size for fit-to-content.
//   MAP: the SVG has a server-driven intrinsic size (drawArena is out of scope), so
//        fit = the board's LAYOUT.width/height + the scroll padding + titlebar; before
//        a board exists we keep the measured default so it isn't a sliver.
//   OTHERS: keep the panel's design width (prose wraps there, so scrollWidth ≈ the
//        design width anyway) and measure the natural HEIGHT at that width -- this is
//        the auto-shrink axis ("a short log stays small, a full roster grows") and is
//        stable, unlike a max-content width that jumps with the longest single line.
function measureContent(panel) {
  const base = DEFAULT_LAYOUT[panel.key];
  if (panel.key === "map") {
    if (LAYOUT && LAYOUT.width && LAYOUT.height) {
      const pad = 32;                                   // .arena-scroll padding (16px x2)
      return {w: Math.ceil(LAYOUT.width) + pad, h: Math.ceil(LAYOUT.height) + pad + resizeMinH(panel)};
    }
    return {w: base.w, h: base.h};
  }
  const style = panel.el.style;
  const savedW = style.width, savedH = style.height;
  style.width = base.w + "px";                           // measure height at the width we'll apply
  style.height = "auto";
  const naturalH = Math.ceil(panel.el.getBoundingClientRect().height);
  style.width = savedW;
  style.height = savedH;
  return {w: base.w, h: naturalH};
}

// Size a content-mode panel to its content, keeping its top-left and staying on-screen.
function fitPanel(panel) {
  if (layoutStacked()) return;
  const bounds = wrapBounds();
  const geom = fitGeom(getInlineGeom(panel), measureContent(panel), bounds, LAYOUT_RESIZE_MIN_W, resizeMinH(panel));
  applyGeom(panel, clampGeom(geom, bounds));
}

// A content change fired the observer: re-fit, but only if this panel is still
// auto-fitting. Debounced so a burst of DOM writes (a full render) fits once.
function scheduleFit(panel) {
  if (layoutStacked() || panel.mode !== "content") return;
  clearTimeout(panel.fitTimer);
  panel.fitTimer = setTimeout(() => {
    if (!layoutStacked() && panel.mode === "content") fitPanel(panel);
  }, 140);
}

function setCtl(button, symbol, label) {
  button.textContent = symbol;
  button.title = label;
  button.setAttribute("aria-label", label);
}

// The Minimize/Expand and Maximize/Restore buttons flip label + glyph with mode,
// so the toggle's restore side is always spelled out (accessible name + tooltip).
function updateCtlButtons(panel) {
  if (!panel.btnMin) return;
  if (panel.mode === "minimized") setCtl(panel.btnMin, "▢", "Expand " + panel.label);
  else setCtl(panel.btnMin, "–", "Minimize " + panel.label);
  if (panel.mode === "maximized") setCtl(panel.btnMax, "❐", "Restore " + panel.label);
  else setCtl(panel.btnMax, "◻", "Maximize " + panel.label);
}

function setMode(panel, mode) {
  panel.mode = mode;
  panel.el.classList.toggle("minimized", mode === "minimized");
  updateCtlButtons(panel);
}

// Fit-to-content: re-enter content mode and size to content now.
function fitToContent(panel) {
  panel.restore = null;
  setMode(panel, "content");
  fitPanel(panel);
  saveLayout();
}

// Snapshot the geometry+mode a toggle should return to. Only capture when the
// panel is in a NON-transient mode (content/manual); if it is already maximized
// or minimized, keep the existing restore instead of overwriting it with the
// transient geom/mode (#335). Otherwise chaining Maximize<->Minimize would clobber
// the user's real manual size and Restore/Expand could no longer return to it.
function captureRestore(panel) {
  if (panel.mode === "maximized" || panel.mode === "minimized") return panel.restore;
  return {geom: getInlineGeom(panel), mode: panel.mode};
}

// Toggle Minimize <-> Expand. Minimize saves the pre-collapse {geom,mode} and
// shrinks to the titlebar; Expand returns to it.
function toggleMinimize(panel) {
  if (panel.mode === "minimized") { revertPanel(panel); return; }
  panel.restore = captureRestore(panel);
  setMode(panel, "minimized");
  const geom = getInlineGeom(panel);
  applyGeom(panel, {x: geom.x, y: geom.y, w: geom.w, h: resizeMinH(panel)});
  saveLayout();
}

// Toggle Maximize <-> Restore. Maximize saves the pre-maximize {geom,mode} and
// fills the wrap; Restore returns to it.
function toggleMaximize(panel) {
  if (panel.mode === "maximized") { revertPanel(panel); return; }
  panel.restore = captureRestore(panel);
  setMode(panel, "maximized");
  bringToFront(panel);
  const bounds = wrapBounds();
  applyGeom(panel, clampGeom(maximizeGeom(bounds), bounds));
  saveLayout();
}

// The restore side of both toggles: return to the exact saved previous
// geometry+mode. We do NOT re-fit here even when the restored mode is "content"
// -- restore means "put it back how it was"; content-mode auto-fit simply resumes
// on the next content change (the observer), so the restore itself is deterministic.
function revertPanel(panel) {
  const saved = panel.restore || {geom: DEFAULT_LAYOUT[panel.key], mode: "content"};
  panel.restore = null;
  setMode(panel, saved.mode);
  const bounds = wrapBounds();
  applyGeom(panel, clampGeom(saved.geom, bounds));
  saveLayout();
}

// #372: the shared pointer-drag lifecycle for the move and resize handlers. The
// ONLY per-interaction difference is `computeGeom(start, dx, dy, others, bounds)`
// — the already-clamped geometry for this pointer delta (move: snap->clamp; resize:
// resizeGeom->snapResize->min->clamp) — plus an optional `onSettle` run once on
// release before the layout is persisted. Capture, the others-map, move/up wiring,
// capture-release and save-on-release all live here, so a lifecycle fix (listener
// cleanup, release, persist) can't reach one interaction and silently miss the other.
function startPanelDrag(panel, downEvent, captureEl, computeGeom, onSettle) {
  bringToFront(panel);
  const start = getInlineGeom(panel);
  const startClientX = downEvent.clientX;
  const startClientY = downEvent.clientY;
  const pointerId = downEvent.pointerId;
  captureEl.setPointerCapture(pointerId);

  const onMove = (moveEvent) => {
    const bounds = wrapBounds();
    const others = LAYOUT_PANELS.filter(other => other !== panel).map(getInlineGeom);
    applyGeom(panel, computeGeom(start, moveEvent.clientX - startClientX,
      moveEvent.clientY - startClientY, others, bounds));
  };
  const onUp = () => {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    if (captureEl.hasPointerCapture(pointerId)) captureEl.releasePointerCapture(pointerId);
    if (onSettle) onSettle();
    saveLayout();
  };
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
}

function onResizePointerDown(panel, dir, downEvent) {
  if (layoutStacked() || downEvent.button !== 0) return;
  downEvent.preventDefault();
  downEvent.stopPropagation();          // never let a handle also start a titlebar drag
  const handle = downEvent.currentTarget;
  const minH = resizeMinH(panel);
  startPanelDrag(panel, downEvent, handle, (start, dx, dy, others, bounds) => {
    let geom = resizeGeom(start, dir, dx, dy, LAYOUT_RESIZE_MIN_W, minH, bounds);
    geom = snapResizeGeom(geom, dir, others, bounds);
    geom.w = Math.max(LAYOUT_RESIZE_MIN_W, geom.w);
    geom.h = Math.max(minH, geom.h);
    return clampGeom(geom, bounds);
  }, () => setMode(panel, "manual"));   // a drag-resize freezes auto-fit
}

function onPanelPointerDown(panel, downEvent) {
  if (layoutStacked() || downEvent.button !== 0) return;
  downEvent.preventDefault();
  startPanelDrag(panel, downEvent, panel.handle, (start, dx, dy, others, bounds) => {
    let geom = {x: start.x + dx, y: start.y + dy, w: start.w, h: start.h};
    geom = snapGeom(geom, others, bounds);
    return clampGeom(geom, bounds);
  });
}

// Measure the floating defaults from the wide flex flow. MUST run with the wrap
// un-floated and the viewport actually wide (getBoundingClientRect then reflects
// the real side-by-side geometry, not the <1100px stacked column). Also derives
// the Character/Action column split (#326): the wide pre-float flow collapses
// .action to zero width, so its default comes from the tracker's full-height
// column (Character ~62% on top, Action ~38% below) rather than the sliver.
function captureDefaults() {
  const wrap = layoutWrap();
  wrap.classList.remove("floating");
  for (const panel of LAYOUT_PANELS) clearInlineGeom(panel);
  DEFAULT_LAYOUT = measureDefaults(wrap);
  const trackerDefault = DEFAULT_LAYOUT.tracker;
  const characterHeight = Math.round(trackerDefault.h * 0.62);
  DEFAULT_LAYOUT.tracker = {...trackerDefault, h: characterHeight};
  DEFAULT_LAYOUT.action = {
    x: trackerDefault.x, y: trackerDefault.y + characterHeight,
    w: trackerDefault.w, h: trackerDefault.h - characterHeight,
  };
  defaultsMeasuredWide = true;
}

// Apply the right layout for the current width: floating (measured defaults ⊕
// persisted) when wide, or the plain stacked flex flow when narrow.
function applyResponsiveLayout() {
  const wrap = layoutWrap();
  if (layoutStacked()) {
    wrap.classList.remove("floating");
    for (const panel of LAYOUT_PANELS) {
      panel.el.classList.remove("minimized");
      clearInlineGeom(panel);
    }
    return;
  }
  // First entry into wide/floating after a narrow load: measure the real wide
  // defaults now (deferred from initLayout because they can't be measured while
  // the stacked media query is in force) -- #338.
  if (!defaultsMeasuredWide) captureDefaults();
  const merged = mergeLayout(DEFAULT_LAYOUT, loadSavedLayout(), LAYOUT_PANELS);
  layoutZTop = LAYOUT_Z_BASE;
  wrap.classList.add("floating");
  for (const panel of LAYOUT_PANELS) {
    const record = merged[panel.key];
    placePanel(panel, {
      restore: record.restoreGeom,
      mode: record.mode,
      geom: {x: record.x, y: record.y, w: record.w, h: record.h},
    });
  }
  reclampAll();
}

// #372: the shared per-panel placement sequence — set the restore target, set the
// sizing mode, apply geometry, drop z to the base band, then content-fit if the
// panel is in content mode. Both the reload (applyResponsiveLayout, merged saved-
// over-defaults) and the Reset-layout (raw defaults) paths flow through this ONE
// chokepoint, so a future placement step can't be added to one and forgotten in the
// other.
function placePanel(panel, {restore, mode, geom}) {
  panel.restore = restore;
  setMode(panel, mode);
  applyGeom(panel, geom);
  panel.el.style.zIndex = LAYOUT_Z_BASE;
  if (panel.mode === "content") fitPanel(panel);
}

function resetLayout() {
  localStorage.removeItem(LAYOUT_KEY);
  localStorage.removeItem(LAYOUT_KEY_V2);   // clear the pre-#326 shape too
  localStorage.removeItem(LAYOUT_KEY_V1);
  layoutZTop = LAYOUT_Z_BASE;
  if (layoutStacked()) return;   // stacked flow IS the default; nothing to place
  for (const panel of LAYOUT_PANELS) {
    placePanel(panel, {                                 // default mode, per #321/#323
      restore: null,
      mode: defaultModeFor(panel),
      geom: DEFAULT_LAYOUT[panel.key],
    });
  }
}

// ---- "Panels" header menu: bring any panel back into view (#325) ------------
// A lighter recovery than Reset layout: pick a panel to un-minimize it, raise it to
// the front, and re-centre it into view so a panel you minimized or shoved off the
// edge is one click away. When stacked (narrow), there are no floating windows, so
// the same pick just scrolls that section into view.
function showPanel(key) {
  const panel = LAYOUT_PANELS.find(p => p.key === key);
  if (!panel || !panel.el) return;
  closePanelsMenu();
  if (layoutStacked()) {
    panel.el.scrollIntoView({block: "start", behavior: "smooth"});
    return;
  }
  if (panel.mode === "minimized") revertPanel(panel);   // un-minimize to its saved geom
  bringToFront(panel);
  const bounds = wrapBounds();
  const geom = getInlineGeom(panel);
  const centered = {
    x: Math.round((bounds.width - geom.w) / 2),
    y: Math.round((bounds.height - geom.h) / 2),
    w: geom.w, h: geom.h,
  };
  applyGeom(panel, clampGeom(centered, bounds));
  saveLayout();
}

// Rebuilt on every open so a panel's live minimized state shows in the list.
function buildPanelsMenu() {
  const menu = $("panelsMenu");
  if (!menu) return;
  menu.innerHTML = "";
  for (const panel of LAYOUT_PANELS) {
    const button = document.createElement("button");
    button.type = "button";
    const minimized = !layoutStacked() && panel.mode === "minimized";
    button.innerHTML = escapeHtml(panel.label)
      + (minimized ? ` <span class="pm-state">minimized</span>` : "");
    button.addEventListener("click", () => showPanel(panel.key));
    menu.appendChild(button);
  }
}

function closePanelsMenu() { const menu = $("panelsMenu"); if (menu) menu.style.display = "none"; }
function togglePanelsMenu() {
  const menu = $("panelsMenu");
  if (!menu) return;
  if (menu.style.display === "block") { closePanelsMenu(); return; }
  buildPanelsMenu();
  menu.style.display = "block";
}
// Click-away / Escape closes the menu (state-driven: it stays open until dismissed,
// never on a timer -- project UI rule: no auto-dismissing chrome).
document.addEventListener("pointerdown", (event) => {
  const dd = document.querySelector(".panels-dd");
  if (dd && !dd.contains(event.target)) closePanelsMenu();
});
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closePanelsMenu(); });

// Build a titlebar control button. pointerdown is stopped so a click on a control
// never also starts a titlebar drag; the <button> stays keyboard-focusable/clickable.
function makeCtlButton(symbol, label, onActivate) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "panel-ctl";
  setCtl(button, symbol, label);
  button.addEventListener("pointerdown", event => event.stopPropagation());
  button.addEventListener("click", event => { event.preventDefault(); onActivate(); });
  return button;
}

function buildPanelChrome(panel) {
  // Titlebar controls cluster (Fit / Minimize / Maximize). It goes on the LEFT of
  // the titlebar on purpose: floating panels are laid out left-to-right with the
  // right neighbour stacked on top, so right-edge controls would be covered by that
  // neighbour once panels overlap (e.g. the map growing to the board's width). A
  // panel's LEFT edge sits over its lower-z left neighbour, so left controls stay
  // clickable. Activating a control also raises the panel, for good measure.
  const controls = document.createElement("span");
  controls.className = "panel-ctls";
  panel.btnFit = makeCtlButton("⤢", "Fit " + panel.label + " to content",
    () => { bringToFront(panel); fitToContent(panel); });
  panel.btnMin = makeCtlButton("–", "Minimize " + panel.label,
    () => { bringToFront(panel); toggleMinimize(panel); });
  panel.btnMax = makeCtlButton("◻", "Maximize " + panel.label, () => toggleMaximize(panel));
  controls.append(panel.btnFit, panel.btnMin, panel.btnMax);
  panel.handle.insertBefore(controls, panel.handle.firstChild);
  // Eight edge/corner resize handles (shown only in floating mode via CSS).
  for (const dir of LAYOUT_RESIZE_DIRS) {
    const handle = document.createElement("div");
    handle.className = "rz rz-" + dir;
    handle.addEventListener("pointerdown", event => onResizePointerDown(panel, dir, event));
    panel.el.appendChild(handle);
  }
}

function initLayout() {
  const wrap = layoutWrap();
  if (!wrap) return;
  for (const panel of LAYOUT_PANELS) {
    panel.el = document.querySelector(panel.selector);
    panel.handle = panel.el && panel.el.querySelector(".panel-titlebar");
  }
  if (LAYOUT_PANELS.some(panel => !panel.el || !panel.handle)) return;  // markup missing
  // Measure defaults only when the viewport is actually wide -- the flex geometry
  // is real only then. If loaded narrow (stacked), defer: applyResponsiveLayout
  // re-measures the first time we enter floating (#338), so a narrow-then-widen
  // load can't bake stacked (full-width, x=0, tall-stacked) geometry into the
  // floating defaults and produce an unrecoverable overlapping layout.
  if (!layoutStacked()) captureDefaults();   // measures BEFORE floating (flex geometry)
  for (const panel of LAYOUT_PANELS) {
    panel.mode = defaultModeFor(panel);
    panel.restore = null;
    buildPanelChrome(panel);
    panel.handle.addEventListener("pointerdown", event => onPanelPointerDown(panel, event));
  }
  applyResponsiveLayout();
  // Auto-fit: watch each panel's content for changes and re-fit while in content
  // mode. Observing childList/subtree/characterData -- NOT attributes -- means the
  // inline style writes our own resizes make never re-trigger it (no feedback loop).
  for (const panel of LAYOUT_PANELS) {
    panel.observer = new MutationObserver(() => scheduleFit(panel));
    panel.observer.observe(panel.el, {childList: true, subtree: true, characterData: true});
  }
  // Crossing the breakpoint re-applies the correct mode; a resize within floating
  // re-clamps so a panel can't end up stranded off the smaller viewport.
  LAYOUT_NARROW.addEventListener("change", applyResponsiveLayout);
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (!layoutStacked()) reclampAll(); }, 100);
  });
}
initLayout();

// Shared view: poll so every browser on this game sees moves as they happen.
// Re-render only when the server state actually changed, to avoid flicker.
// (Declared before the boot dispatch below, which calls showPreGame() ->
// _lastStateJSON, so the reference isn't in the temporal dead zone.)
let _lastStateJSON = "";
// #343: the last server change token seen. The server bumps a monotonic `rev` on
// every persisted mutation, so the poll can decide "did anything change?" in O(1)
// instead of re-JSON.stringifying the whole just-parsed state every 2s tick.
let _lastRev = null;
// Deep link: /game/<gid> joins or spectates an existing game; a fresh load shows
// the editable pre-game Game Control (no auto-boot -- New Game starts a match).
const urlGid = (location.pathname.match(/^\/game\/([^/]+)/) || [])[1];
if (urlGid) { GID = urlGid; refresh(); } else { showPreGame(); }
let pollBusy = false;                                // a poll's fetch is in flight
// Live polling is (re)startable: a game-gone tick clears it (#275), and every
// new-game entry point re-arms it (#308) so a match started in the same tab keeps
// syncing. Clearing before setting guarantees we never stack overlapping timers.
let POLL = null;
function startPolling() {
  if (POLL) clearInterval(POLL);
  POLL = setInterval(async () => {
  if (!GID) return;
  // Skip this tick if the previous poll's fetch has not resolved yet: on a slow
  // connection setInterval would otherwise stack overlapping requests (#272).
  if (pollBusy) return;
  pollBusy = true;
  try {
  const polledGid = GID;                             // pin the game we're polling for
  // The hex layout is immutable after game creation, so once we have it, ask the
  // server to skip re-shipping it on every 2s poll (#256). If we somehow don't
  // have it yet, request the full payload so the board can still render.
  const data = await api(`/api/game/${GID}${LAYOUT ? "?layout=0" : ""}`);
  // The game we polled may have ended (End Game -> showPreGame nulls GID) or been
  // replaced (New Game) while this request was in flight. Its now-stale response
  // must NOT repopulate S/board/banner over the reset -- that clobbered End Game's
  // return to the pre-game state (#226). Drop the result unless it's still current.
  if (polledGid !== GID) return;
  if (data.error) {                                  // game gone — stop polling
    clearInterval(POLL); POLL = null;                // null so startPolling() can re-arm (#308)
    if (data.error === "unknown game") gameLost();   // and say so, persistently (#275)
    return;
  }
  // #343: when the server sends a change token, skip on it in O(1) rather than
  // re-stringifying the entire state each tick. rev is bumped on every persisted
  // mutation (including seat changes, which also persist), so it subsumes the
  // seat/ownership fields the state signature had to include for #85. Fall back
  // to the full state signature for a server (or reload path) that sends no rev.
  if (data.rev != null) {
    if (data.rev === _lastRev) return;
    _lastRev = data.rev;
  } else {
    // Include the seat/ownership fields: opening or claiming a seat changes these
    // but NOT data.state, so a state-only signature would miss seat updates (#85).
    const sig = JSON.stringify([data.state, data.you_control, data.open_seats, data.is_admin]);
    if (sig === _lastStateJSON) return;
    _lastStateJSON = sig;
  }
  // Keep the cached layout when the poll omitted it (?layout=0); only replace it
  // when the server actually sent one (first load / reconnect) (#256).
  if (data.layout) LAYOUT = data.layout;
  S = data.state; captureOwnership(data); optCache = {}; render();
  maybeAutoTarget();   // #299: sole-target auto-queue when new state arrives
  } finally {
    pollBusy = false;
  }
  }, 2000);
}
startPolling();
// Arriving from login (LOGIN_REDIRECT_URL = "/?setup") opens the wizard straight away.
if (new URLSearchParams(location.search).has("setup")) openSetup();

// This file loads as an ES module, so its top-level functions are module-scoped,
// not global. The board.html markup wires buttons through inline handlers
// (onclick="openSetup()", onclick="seatAction('open','red')", etc.) -- including
// handlers in HTML this script builds via innerHTML -- and those attributes
// resolve names against `window`. Expose every inline-referenced handler so the
// markup keeps working unchanged.
Object.assign(window, {
  openSetup, closeSetup, startSetup,
  newGame, endGame, addPlayer, removePlayer,
  openAdmin, closeAdmin, adminCreateUser, adminSelectUser, adminDeleteUser,
  adminDeleteChar, adminCreateCharFor,
  openEditor, closeEditor, startCustom,
  copyLink, seatAction, resetTheme,
  downloadDebugLog, resetLayout, togglePanelsMenu, showPanel,
  // render is the poll's re-render entry (the 2s tick calls it on a state
  // change). Exposed so e2e can simulate a poll tick deterministically and prove
  // the poll-clobber guards (#323/#339) leave a focused input alone.
  render,
});
