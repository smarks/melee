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
// A figure is yours iff its side is in YOU_CONTROL (admins control all). Fall back
// to the same screen rule (any non-computer side) only if the server sent no seats.
const myControlled = f => IS_ADMIN ? true
  : YOU_CONTROL.length ? YOU_CONTROL.includes(f.side)
  : (S.controllers || {})[f.side] !== "computer";
function captureOwnership(data) {
  if ("you_control" in data) YOU_CONTROL = data.you_control || [];
  if ("open_seats" in data) OPEN_SEATS = data.open_seats || [];
  if ("is_admin" in data) IS_ADMIN = !!data.is_admin;
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
    is_admin: IS_ADMIN, plan: PLAN, combatResolvedTurn, sel, state: S,
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
  optCache = {};
  resetSelection(); ensureGameCatalog(); render();
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
async function startSetup() {
  const p = encodeURIComponent($("profile").value);
  const practice = $("practiceMode") && $("practiceMode").checked ? 1 : 0;
  // teams = player count, per_team = characters per player (uniform), and the AI
  // players' sides drive the explicit `computer` list the endpoint now honours.
  const q = `profile=${p}&teams=${PLAYERS.length}&per_team=${$("perTeam").value}`
    + `&computer=${encodeURIComponent(computerSides())}&practice=${practice}`;
  await startGame(q);
}
// Add a player of the given type ("human" | "ai") to the roster, up to the cap.
function addPlayer(type) {
  if (GAME_ACTIVE || PLAYERS.length >= MAX_PLAYERS) return;
  PLAYERS.push({type: type === "ai" ? "ai" : "human"});
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
    const kind = local ? "You (human)" : pl.type === "ai" ? "AI" : "Human";
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
  const enoughPlayers = count >= 2;
  const newBtn = $("newGameBtn"); if (newBtn) newBtn.disabled = locked || !enoughPlayers;
  const reason = $("newGameReason");
  if (reason) reason.textContent = (locked || enoughPlayers) ? "" : "Add at least 2 players to start.";
}
// New Game starts a match through the existing setup flow, then locks the panel.
async function newGame() {
  if (GAME_ACTIVE || PLAYERS.length < 2) return;
  dbg("INTERACT", "New Game pressed", {players: PLAYERS.map(p => p.type)});
  await startSetup();
}
// The editable pre-game state: no game tracked, Game Control unlocked with New
// Game live, the Map blank, and the Characters tracker empty. This is what a
// fresh load (no deep-link) shows, and where End Game returns to. (#192)
function showPreGame() {
  GID = null; S = null; LAYOUT = null; GAME_ACTIVE = false;
  PLAYERS = [{type: "human"}];         // fresh roster: just the local human (#192)
  _lastStateJSON = ""; resetAll(); closeMenu();
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
const PHASE_LABEL = {select: "Action selection", combat: "Combat"};
const OPTION_LABEL = {
  move: "Full move", half_move: "Half move", charge_attack: "⚔ Charge & Attack", dodge: "Dodge",
  ready_weapon: "Ready Weapon", missile_attack: "🏹 Missile Attack", stand_up: "Stand Up", crawl: "Crawl 2",
  shift_attack: "⚔ Attack (may shift 1)", shift_defend: "Shift & Defend",
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
function render() {
  if (!S) return;
  dbgTransitions();                      // log phase / turn / active changes
  if (S.phase !== lastPhase) {           // new phase → fresh, empty plan
    lastPhase = S.phase; PLAN = {}; warnKind = null; resetSelection(); closeMenu();
  }
  drawArena();
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
function drawControls() {
  const c = $("controls"); c.innerHTML = "";
  const phase = S.phase;
  $("phaseBanner").textContent = bannerFor(phase);

  if (S.victory) {
    setHint(`🏆 <b>${sideName(S.victory)}</b> wins the field!`);
    bigPrimary(c, "Start next round →", () => act({type: "end_turn", expected_turn: S.turn}).then(after));
    return;
  }

  if (phase === "select") {
    // Per-character initiative selection (#192): only the active figure may act,
    // and each choice submits immediately, lighting up the next figure. The
    // action buttons themselves now live inline under each character in the
    // roster (drawRoster / figControlsHtml, #198/#199) instead of pinned here at
    // the bottom, so #controls just carries the "what to do now" hint.
    if (sel && chosenOption) {                               // mid-placement (inline, #202)
      const placing = figByUid(sel);
      setHint(`Placing <b>${escapeHtml(placing ? placing.name : "")}</b> — click a green`
              + ` hex on the board, then press <b>Set action</b> under its card.`);
      return;
    }
    const active = S.active_uid ? figByUid(S.active_uid) : null;
    if (!active) { setHint("Resolving the action pass…"); return; }
    if (!myControlled(active)) {
      setHint(`Waiting for <span class="chip ${active.side}">${sideName(active.side)}</span>`
              + ` to set <b>${escapeHtml(active.name)}</b>'s action…`);
      return;
    }
    setHint(`<b>${escapeHtml(active.name)}</b> has initiative — choose its action`
            + ` from its card above, on its counter, or the board. It submits`
            + ` immediately, then the next figure lights up.`);
    return;
  }

  if (phase === "combat") {
    const actionable = new Set(S.combat_actionable || []);
    const actors = S.figures.filter(f => f.label && myControlled(f) && actionable.has(f.uid));
    if (combatResolvedTurn !== S.turn) {
      setHint("Choose each figure's attack, then resolve.");
      figureChecklist(c, actors);
      // #212: a figure that committed to an attack option AND has a valid target
      // (server's must_attack) would silently waste its shot if combat resolved
      // without a queued attack for it. Force those to be targeted first: gate
      // Resolve on your own must-attack figures until each has a PLAN entry, and
      // name the ones still needing a target. Figures that did NOT commit to an
      // attack stay under the soft "will do nothing" warning, not this gate.
      const mustAttack = new Set(S.must_attack || []);
      const untargeted = actors.filter(f => mustAttack.has(f.uid) && !PLAN[f.uid]);
      const idle = actors.filter(f => !PLAN[f.uid] && !mustAttack.has(f.uid)).length;
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
      }
      const resolveBtn = bigPrimary(c, actors.length ? "Resolve attacks" : "Resolve combat", () => {
        dbg("INTERACT", "Resolve pressed", {queued: Object.keys(PLAN).length, actors: actors.length});
        combatResolvedTurn = S.turn;       // next render offers "End turn"
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
    } else {
      setHint("Attacks resolved — push back any beaten foes, then end the turn.");
      drawForceRetreat(c);                 // post-combat shoves, if any
      bigPrimary(c, "End turn →", () => {
        dbg("INTERACT", "End turn pressed");
        // #242: carry the turn we mean to end so a double-click / retried POST
        // that lands after the first end_turn already advanced is a safe no-op
        // server-side instead of silently skipping a whole turn.
        resetAll(); act({type: "end_turn", expected_turn: S.turn}).then(after);
      });
    }
    return;
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
    return attacker && myControlled(attacker);
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
    for (const uid of (optInfo._targets || [])) {
      const e = figByUid(uid);
      rows.push({label: `${shooting ? "🏹 Shoot" : "⚔ Attack"} ${escapeHtml(e ? e.name : uid)}`,
                 act: () => setAttack(f, uid)});
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
    rows.push({label: "Do nothing", act: () => setDoNothing(f)});
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
function setAttack(f, target, {mainGauche = false} = {}) {
  const e = figByUid(target);
  dbg("INTERACT", `queue attack ${f.name} → ${e ? e.name : target}`,
      {attacker: f.uid, foe: target, mainGauche});
  // The off-hand main-gauche jab (p.13) is an extra -4 DX melee hit riding on the
  // same attack; carry the flag so executePlans can send main_gauche (#248).
  const jab = mainGauche ? " + 🗡 main-gauche jab" : "";
  PLAN[f.uid] = {uid: f.uid, phase: "combat", target, mainGauche,
                 label: `Attack ${e ? e.name : target}${jab}`};
  render();
}
function setDoNothing(f) {
  PLAN[f.uid] = {uid: f.uid, phase: "combat", none: true, label: "Do nothing"};
  render();
}
// ---- selection phase: immediate submission (#192) ---------------------------
// In the select phase there is no batch: each choice POSTs right away and the
// server lights up the next figure in initiative order.
function isActive(f) { return !!f && S.active_uid === f.uid; }
function hasPassed(f) { return !!f && (S.passed || []).includes(f.uid); }
function canPass(f) { return isActive(f) && !hasPassed(f); }
function selectDoNothing(f) {
  dbg("INTERACT", `do-nothing ${f.name}`, {uid: f.uid});
  closeMenu();
  act({type: "do_nothing", uid: f.uid}).then(after);
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
    else if (p.target) await act({type: "queue_attack", uid: p.uid, target: p.target,
                                  main_gauche: !!p.mainGauche});
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
  info._targets = [...new Set([...(info.missile_targets || []),
                               ...(info.melee_targets || [])])];
  optCache[f.uid] = info;
  return info;
}

async function onFigureClick(f) {
  flash("");
  // Selecting a figure to INSPECT it (its read-only sheet in the Selected panel)
  // is always allowed -- theirs or an enemy's (#214). ACTING stays gated: the
  // action menu only opens for your own actionable figure. So a figure you can't
  // command is simply inspected, not flashed away.
  const tag = {uid: f.uid, name: f.name, side: f.side, myControlled: myControlled(f), phase: S.phase};
  if (S.phase === "select") {
    if (!isActive(f) || !myControlled(f)) { dbg("INTERACT", `figure click ${f.name} → inspect`, tag); inspectFigure(f); return; }
    dbg("INTERACT", `figure click ${f.name} → open-menu`, tag);
    sel = f.uid; chosenOption = null; pendingDest = null; pendingFacing = f.facing; pendingReady = null;
    optInfo = await loadOptions(f);
    render(); openMenu(f);
  } else if (S.phase === "combat") {
    if (!myControlled(f)) {
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

// #220: queue a committed-but-untargeted shooter's attack at ``enemy`` when the
// player clicks that foe. Mirrors the Resolve gate's own "untargeted" set: a
// figure I control that the server flagged in must_attack and that has no PLAN
// yet. The first such shooter that can actually reach this foe (its combat
// _targets list includes it) takes the shot, so clicking a foe repeatedly assigns
// each pending shooter in turn. Returns true if a shot was queued (so the caller
// skips plain inspection), false if none applied (fall back to inspecting).
async function queuePendingShotAt(enemy) {
  if (!S || S.phase !== "combat" || combatResolvedTurn === S.turn) return false;
  const mustAttack = new Set(S.must_attack || []);
  const pending = S.figures.filter(
    f => myControlled(f) && mustAttack.has(f.uid) && !PLAN[f.uid]);
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
  if (S.phase === "select") return isActive(f) && !!f.can_act && myControlled(f);
  if (S.phase === "combat") return myControlled(f) && !!f.can_act;
  return false;
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
  info = info || optInfo;
  return info && info.missile_targets && info.missile_targets.length >= 0
    && f.weapon && ["Longbow","Small bow","Horse bow","Sling","Thrown rock",
                    "Light crossbow","Heavy crossbow"].includes(f.weapon);
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
function drawSelInfo() {
  const box = $("selInfo");
  const f = sel ? figByUid(sel) : null;
  if (!f) { box.innerHTML = `<span class="muted">No figure selected.</span>`; return; }
  box.innerHTML = statusHeader(f) + charSheetHtml(f) + planLine(f);
  // The live editor is owner/admin-only: a figure you don't command shows the
  // read-only sheet above but gets no Edit button (#214).
  if (!myControlled(f) || !f.edit_spec) return;
  // Keep this fighter: a signed-in player may snapshot a fighter they control
  // into their saved characters, straight from the running game (#234).
  if (LOGGED_IN) box.appendChild(saveCharacterUi(f));
  if (!CAT || !RULES || CAT.profile !== PROFILE) { ensureGameCatalog(); return; }
  // The full editor opens in its own modal so the stats, gear, and Apply button
  // get a first-class, always-reachable surface instead of being crammed into
  // this corner panel where the Apply button was clipped (#181).
  const edit = document.createElement("button");
  edit.className = "primary"; edit.style.marginTop = "10px";
  edit.textContent = "✎ Edit this fighter…";
  edit.addEventListener("click", () => openLiveEdit(f.uid));
  box.appendChild(edit);
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

let LIVE_EDIT_FOR = null;            // uid the live-edit modal is open for, if any
function openLiveEdit(uid) {
  const f = figByUid(uid);
  if (!f || !f.edit_spec) return;
  if (!CAT || !RULES || CAT.profile !== PROFILE) { ensureGameCatalog(); return; }
  LIVE_EDIT_FOR = uid;
  $("liveEditSub").textContent =
    `Editing ${f.name} (${f.side}). Changes apply immediately to the running game.`;
  const slot = $("liveEditSlot"); slot.innerHTML = "";
  slot.appendChild(liveEditCard(f));
  $("liveEdit").style.display = "flex";
}
function closeLiveEdit() { $("liveEdit").style.display = "none"; LIVE_EDIT_FOR = null; }
function tokenBadge(f) {   // the same numbered disc the board draws, for matching
  return `<span class="tokenbadge" style="background:${fillFor(f.side)}">`
    + `${f.dead ? "✗" : hpCur(f)}</span>`;
}
function weaponsLine(f) {
  const ready = f.weapon || "—";
  const reserve = (f.weapons || []).filter(w => w !== f.weapon);
  const reloading = f.reloading > 0
    ? ` <span style="color:var(--target)">— reloading (${f.reloading})</span>` : "";
  return `<div class="muted">In hand: <b>${ready}</b>${reloading}`
    + (reserve.length ? ` · ready to switch: ${reserve.join(", ")}` : "") + `</div>`;
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
  const readied = f.weapon || null;
  const carried = f.weapons || [];
  // Readied weapon first and clearly marked, then the rest of the kit (Dagger etc.).
  const ordered = readied
    ? [readied, ...carried.filter(w => w !== readied)]
    : carried.slice();
  const weaponItems = ordered.length
    ? ordered.map(w => `<li>${escapeHtml(w)}`
        + (readied && w === readied ? ` <span class="readied">— readied</span>` : "")
        + `</li>`).join("")
    : `<li class="muted">unarmed</li>`;
  const vitals = f.model === "tarmar"
    ? `Fatigue ${f.fatigue}/${f.max_fatigue} · Body ${f.body}/${f.max_body} · DX ${f.dx}`
    : `ST ${f.st}/${f.max_st} · DX ${f.dx}`;
  const armor = (f.armor && f.armor !== "None") ? escapeHtml(f.armor) : "none";
  return `<div class="charsheet">`
    + `<div class="sheet-vitals">${vitals}</div>`
    + `<div class="sheet-sub">Weapons</div>`
    + `<ul class="sheet-weapons">${weaponItems}</ul>`
    + `<div class="sheet-gear">Armor: <b>${armor}</b> · Shield: <b>${shieldState(f)}</b></div>`
    + `</div>`;
}

// Catalog for the *running* game's profile (the editor may have loaded another).
let gameCatBusy = false;
async function ensureGameCatalog() {
  if (gameCatBusy || !PROFILE || (CAT && RULES && CAT.profile === PROFILE)) return;
  gameCatBusy = true;
  CAT = await api(`/api/catalog?profile=${encodeURIComponent(PROFILE)}`);
  RULES = CAT.stat_rules;
  gameCatBusy = false;
  render();
}

function liveEditCard(f) {
  const card = document.createElement("div"); card.className = "card";
  card.dataset.side = f.side;
  card.innerHTML = cardInner(f.edit_spec);
  card.addEventListener("input", () => refreshCard(card));
  card.addEventListener("change", () => refreshCard(card));
  const apply = document.createElement("button");
  apply.className = "primary"; apply.textContent = "Apply to game";
  apply.addEventListener("click", () => applyEdit(card, f.uid));
  card.appendChild(apply);
  setTimeout(() => refreshCard(card), 0);
  return card;
}
async function applyEdit(card, uid) {
  const data = await act({type: "update_figure", uid, spec: readCard(card)});
  if (data) { flash("Applied changes."); closeLiveEdit(); render(); }
}
function planLine(f) {
  const p = PLAN[f.uid];
  if (p) return `<div style="margin-top:8px" class="muted">Action set: <b>${escapeHtml(p.label)}</b>`
    + `${p.dest ? " → " + escapeHtml(p.dest) : ""}</div>`;
  if (S.phase === "select" && f.acted)
    return `<div style="margin-top:8px" class="muted">Action set: <b>${optLabel(f.option)}</b></div>`;
  if (S.phase === "select" && hasPassed(f) && !f.acted)
    return `<div style="margin-top:8px" class="muted">Passed — waiting to choose last.</div>`;
  if ((S.phase === "select" && isActive(f) && myControlled(f) && f.can_act) ||
      (S.phase === "combat" && myControlled(f) && f.can_act))
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
    if (isActive(f) && myControlled(f) && !f.dead)
      return `<span class="action todo">choose action</span>`;
    return `<span class="action idle">—</span>`;
  }
  const canFight = S.phase === "combat" && myControlled(f) && f.can_act;
  if (canFight && !f.dead) return `<span class="action todo">choose action</span>`;
  return `<span class="action idle">—</span>`;
}
// The inline per-character action controls shown UNDER each character row during
// the selection phase (#198/#199/#202). Instead of a "Choose action → popup"
// indirection, the FULL list of this figure's actions is listed inline: the
// active, controllable figure's valid options are live (clicking one specifies
// it directly), and its invalid options are greyed with the server's reason.
// Every other not-yet-acted figure shows a greyed preview list of the control
// that becomes theirs on their turn. A figure that has already acted, passed, or
// is dead shows no block (its chosen action / "Passed — waiting" badge stands).
function figControlsHtml(f) {
  if (S.phase !== "select") return "";
  if (f.dead || f.collapsed || f.acted) return "";
  // A passer that isn't up yet shows its "Passed — waiting" badge, not a control
  // block; once it comes up last to choose, isActive is true and it gets the
  // enabled block again (with Pass disabled -- it's already deferred).
  if (hasPassed(f) && !isActive(f)) return "";
  const enabled = isActive(f) && myControlled(f);
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
    const dis = (!enabled || o.available === false) ? " disabled" : "";
    const why = (enabled && o.available === false && o.reason)
      ? `<span class="why">${escapeHtml(o.reason)}</span>` : "";
    return `<button class="opt${o.attack ? " attack" : ""}" data-opt="${escapeHtml(o.option)}"${dis}>`
      + `<span>${escapeHtml(optLabel(o.option))}</span>${why}</button>`;
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
  if (!isActive(f) || !myControlled(f)) return;
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
  if (!active || active.dead || !myControlled(active)) return;
  if (optCache[active.uid] || activeOptsBusy === active.uid) return;
  activeOptsBusy = active.uid;
  await loadOptions(active);
  activeOptsBusy = null;
  if (S && S.active_uid === active.uid) drawRoster();   // state may have moved on
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
      html += `<div class="${cls}" data-uid="${escapeHtml(f.uid)}">`
        + `<span class="rowmain">${tokenBadge(f)} ${escapeHtml(f.name)} ${classTag}`
        + `<span class="muted">${state}</span>${kit}</span>`
        + figActionHtml(f) + `</div>`
        + figControlsHtml(f);
    }
  }
  html += inviteHtml();
  r.innerHTML = html;
  r.querySelectorAll(".row[data-uid]").forEach(row => {
    const f = figByUid(row.dataset.uid);
    if (f) row.addEventListener("click", () => onFigureClick(f));
  });
  // Wire the inline per-character action list (#202). Disabled buttons (invalid
  // options and every non-active figure's greyed preview) carry the `disabled`
  // attribute and get no handler. An enabled option specifies the action directly
  // (simple -> submit now; destination -> inline placement); the placement block's
  // data-act buttons turn/confirm/cancel it, and its weapon selector sets Ready.
  r.querySelectorAll(".charctl[data-ctl]").forEach(block => {
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
  ensureActiveOptions();   // load the active figure's real options, then re-draw
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
let CAT = null, RULES = null;

function buildRoster(profile, teams, perTeam) {
  const tmpl = ARCHETYPES[profile] || ARCHETYPES["Classic Melee"];
  const roster = [];
  for (let t = 0; t < teams; t++)
    for (let i = 0; i < perTeam; i++)
      roster.push(Object.assign({}, tmpl[i % tmpl.length], {side: ED_TEAMS[t]}));
  return roster;
}

const rint = (a, b) => a + Math.floor(Math.random() * (b - a + 1));
// Ask the server for the most *effective* melee + missile weapon (expected damage
// = hit-chance x damage, so a heavy/under-strength weapon is discounted in Tarmar).
async function setWeapons(card, strength, dexterity, skill) {
  const p = encodeURIComponent($("profile").value);
  const data = await api(`/api/best_weapons?profile=${p}&strength=${strength}`
    + `&dexterity=${dexterity}&skill=${skill}`);
  if (data.melee) card.querySelector('[data-eq="weapon"]').value = data.melee;
  if (data.missile) card.querySelector('[data-eq="weapon2"]').value = data.missile;
  refreshCard(card);
}
function generateInto(card) {       // randomize this fighter within the rules
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
  const profile = $("profile").value;
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
  refreshCard(card);
}
async function saveCharacter(card) {
  const spec = readCard(card);
  // An admin building a character for a player (#140) saves to that user's
  // collection; otherwise it's the signed-in player's own save.
  const url = EDIT_FOR_USER ? `/api/admin/users/${EDIT_FOR_USER.id}/characters` : "/api/characters";
  const data = await postJSON(url, {name: spec.name, profile: $("profile").value, spec});
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
  return f;
}

function disableByStrength(select, strength, offset) {
  CAT.weapons.forEach((w, idx) => {
    const opt = select.options[idx + offset];
    if (opt) opt.disabled = (w.str_req || 0) > strength;
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
  const practice = $("practiceMode") && $("practiceMode").checked;
  const body = {profile: $("profile").value, computer, fighters, practice};
  const data = await api("/api/game/new_custom", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)});
  if (data.error) { $("editorErr").textContent = "Can't start: " + data.error; return; }
  GID = data.gid; LAYOUT = data.layout; S = data.state; PROFILE = data.profile;
  captureOwnership(data); history.replaceState({}, "", `/game/${GID}`);
  closeEditor(); closeSetup(); closeLiveEdit(); resetSelection(); render();
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
// Shared view: poll so every browser on this game sees moves as they happen.
// Re-render only when the server state actually changed, to avoid flicker.
// (Declared before the boot dispatch below, which calls showPreGame() ->
// _lastStateJSON, so the reference isn't in the temporal dead zone.)
let _lastStateJSON = "";
// Deep link: /game/<gid> joins or spectates an existing game; a fresh load shows
// the editable pre-game Game Control (no auto-boot -- New Game starts a match).
const urlGid = (location.pathname.match(/^\/game\/([^/]+)/) || [])[1];
if (urlGid) { GID = urlGid; refresh(); } else { showPreGame(); }
const POLL = setInterval(async () => {
  if (!GID) return;
  const polledGid = GID;                             // pin the game we're polling for
  const data = await api(`/api/game/${GID}`);
  // The game we polled may have ended (End Game -> showPreGame nulls GID) or been
  // replaced (New Game) while this request was in flight. Its now-stale response
  // must NOT repopulate S/board/banner over the reset -- that clobbered End Game's
  // return to the pre-game state (#226). Drop the result unless it's still current.
  if (polledGid !== GID) return;
  if (data.error) {                                  // game gone — stop polling
    clearInterval(POLL);
    if (data.error === "unknown game") gameLost();   // and say so, persistently (#275)
    return;
  }
  // Include the seat/ownership fields: opening or claiming a seat changes these
  // but NOT data.state, so a state-only signature would miss seat updates (#85).
  const sig = JSON.stringify([data.state, data.you_control, data.open_seats, data.is_admin]);
  if (sig === _lastStateJSON) return;
  _lastStateJSON = sig;
  LAYOUT = data.layout; S = data.state; captureOwnership(data); optCache = {}; render();
}, 2000);
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
  copyLink, seatAction, closeLiveEdit, resetTheme,
  downloadDebugLog,
});
