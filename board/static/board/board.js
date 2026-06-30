const SVG = "http://www.w3.org/2000/svg";
let GID = null, S = null, LAYOUT = null, PROFILE = null;
let GAME_ACTIVE = false;  // a match is running -> Game Control settings lock (#192)
let sel = null;          // figure being placed (movement), if any
let optInfo = null;      // options payload for the active figure
let chosenOption = null; // move option mid-placement (needs a destination hex)
let pendingDest = null;  // hex label
let pendingFacing = null;// facing index
let pendingReady = null; // carried weapon to switch to
let PLAN = {};           // uid -> pending action for this phase (executed on Continue)
let warnKind = null;     // (legacy) kept for resetAll; the new flow warns inline
let _rolling = false;    // guard so initiative auto-rolls exactly once
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
const csrftoken = () => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
let SAVED = [];   // the signed-in player's saved characters (for the editor)
function postJSON(path, body) {
  return fetch(path, {method: "POST", headers: {
    "Content-Type": "application/json", "X-CSRFToken": csrftoken()},
    body: JSON.stringify(body)}).then(r => r.json());
}

async function startGame(query) {
  const data = await api(`/api/game/new?${query}`);
  GID = data.gid; LAYOUT = data.layout; S = data.state; PROFILE = data.profile;
  captureOwnership(data); history.replaceState({}, "", `/game/${GID}`);
  optCache = {};
  resetSelection(); ensureGameCatalog(); render();
  GAME_ACTIVE = true; syncGameControl();
}
function bootGame() { startGame("teams=2&per_team=2&mode=pxai"); }  // default on load
// The setup controls are now an always-visible inline "Game Control" panel, so
// open/close are no-ops kept only for the callers that still reference them (the
// post-login ?setup deep link, the editor's Back button).
function openSetup() { const gc = $("gameControl"); if (gc) gc.scrollIntoView({block: "nearest"}); }
function closeSetup() {}
async function startSetup() {
  const p = encodeURIComponent($("profile").value);
  const practice = $("practiceMode") && $("practiceMode").checked ? 1 : 0;
  const q = `profile=${p}&mode=${$("mode").value}&teams=${$("teams").value}`
    + `&per_team=${$("perTeam").value}&practice=${practice}`;
  await startGame(q);
}
// Opponent type maps onto the engine's two-side model: the local player is side
// one; the opponent is side two -- either a same-screen human (pxp) or the AI
// (pxai). The two buttons set the hidden #mode the rest of the flow reads.
function setOpponent(mode) {
  if (GAME_ACTIVE) return;                 // settings are locked while a game runs
  $("mode").value = mode;
  $("oppComputer").classList.toggle("primary", mode === "pxai");
  $("oppHuman").classList.toggle("primary", mode === "pxp");
}
// New Game starts a match through the existing setup flow, then locks the panel.
async function newGame() { if (GAME_ACTIVE) return; await startSetup(); }
// End Game abandons the running match client-side (no backend endpoint needed):
// stop tracking the game, clear the board + tracker, and return Game Control to
// its editable state with New Game enabled again. (#192)
function endGame() {
  GID = null; S = null; LAYOUT = null; GAME_ACTIVE = false;
  _lastStateJSON = ""; resetAll(); closeMenu();
  history.replaceState({}, "", "/");
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
// Reflect the lock state: while a game runs every setting is read-only, New Game
// is disabled, and End Game is live; before/after a game the reverse holds. (#192)
function syncGameControl() {
  const locked = GAME_ACTIVE;
  ["profile", "mode", "teams", "perTeam", "practiceMode",
   "oppComputer", "oppHuman", "editCharBtn", "newGameBtn"].forEach(id => {
    const el = $(id); if (el) el.disabled = locked;
  });
  const end = $("endGameBtn"); if (end) end.disabled = !locked;
  const gc = $("gameControl"); if (gc) gc.classList.toggle("locked", locked);
}
async function refresh() {
  const data = await api(`/api/game/${GID}`);
  if (data.error) {                 // game gone (it ended or the dev server restarted)
    $("phaseBanner").textContent = "Game not found — it ended or the server restarted. Start a New game.";
    flash("This game is no longer available.");
    return;
  }
  LAYOUT = data.layout; S = data.state; captureOwnership(data); optCache = {};
  GAME_ACTIVE = true; syncGameControl(); render();
}
async function act(body) {
  const data = await api(`/api/game/${GID}/action`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  });
  if (data.error) { flash(data.error); return null; }
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
function inviteHtml() {   // no one to invite in a vs-computer game (#165)
  const vsComputer = S.controllers && Object.values(S.controllers).includes("computer");
  return vsComputer ? "" :
    `<button style="margin-top:8px;padding:1px 7px;cursor:pointer" onclick="copyLink()">Copy invite link</button>`;
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
const PHASE_LABEL = {initiative: "Initiative", move: "Movement", combat: "Combat"};
const OPTION_LABEL = {
  move: "Full move", half_move: "Half move", charge_attack: "⚔ Charge & Attack", dodge: "Dodge",
  ready_weapon: "Ready Weapon", missile_attack: "⚔ Missile Attack", stand_up: "Stand Up", crawl: "Crawl 2",
  shift_attack: "⚔ Attack (may shift 1)", shift_defend: "Shift & Defend",
  one_last_shot: "⚔ One Last Shot", change_weapons: "Change Weapons", disengage: "Disengage",
  hth_attack: "🤼 Grapple", pick_up: "Pick up weapon",
  go_prone: "Drop prone", kneel: "Kneel",
};
// missile_attack is here so its optional 1-hex move (option f: "move up to 1 hex
// and/or fire") gets a destination picker, not forced to hold position (#117).
const NEEDS_DEST = new Set(["move", "half_move", "charge_attack", "dodge", "disengage", "crawl", "missile_attack"]);
const WEAPON_CHANGE = new Set(["ready_weapon", "change_weapons"]);
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
  if (S.phase !== lastPhase) {           // new phase → fresh, empty plan
    lastPhase = S.phase; PLAN = {}; warnKind = null; resetSelection(); closeMenu();
  }
  drawArena();
  drawControls();
  drawSelInfo();
  drawRoster();
  drawLog();
  const vsComputer = S.controllers && Object.values(S.controllers).includes("computer");
  $("turnInfo").textContent = (PROFILE || "") + (vsComputer ? " · vs Computer" : " · same screen");
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
    if (f.dodging) cls += " dodge";
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

    if (PLAN[f.uid]) {                               // green ring = action set
      const ring = document.createElementNS(SVG, "circle");
      ring.setAttribute("cx", h.cx); ring.setAttribute("cy", h.cy);
      ring.setAttribute("r", LAYOUT.size * 0.82);
      ring.setAttribute("fill", "none");
      ring.setAttribute("stroke", "#7CFC8C"); ring.setAttribute("stroke-width", "2.5");
      g.appendChild(ring);
    }

    // A figure that chose dodge/defend this turn is attacked on FOUR dice — mark
    // it with a guard ring + shield glyph so everyone can see who's defending.
    if (f.dodging && !f.dead) {
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
    bigPrimary(c, "Start next round →", () => act({type: "end_turn"}).then(after));
    return;
  }

  if (phase === "initiative") {
    if (!S.winner) {                       // the roll is random -- no click to make
      if (!_rolling) {
        _rolling = true;
        setHint("Rolling initiative…");
        act({type: "roll_initiative"}).then(() => { _rolling = false; render(); });
      }
      return;
    }
    setHint(`🎲 <b>${sideName(S.winner)}</b> won initiative — who moves first?`);
    for (const side of S.sides)
      bigPrimary(c, `${sideName(side)} moves first`,
                 () => act({type: "choose_first", side}).then(after), side === S.winner);
    return;
  }

  if (phase === "move") {
    if (sel && chosenOption) { drawPlacement(c); return; }   // mid-placement keeps its UI
    const movers = S.figures.filter(f => f.side === S.moving_side && f.can_act && f.label);
    setHint(`<span class="chip ${S.moving_side}">${sideName(S.moving_side)}</span> — `
            + `set each figure's move, then press Done.`);
    figureChecklist(c, movers);
    const idle = movers.filter(f => !PLAN[f.uid]).length;
    if (idle) warnLine(c, `${idle} figure${idle > 1 ? "s" : ""} will hold position.`);
    bigPrimary(c, "Done moving →", () => executePlans("move"));
    return;
  }

  if (phase === "combat") {
    const actionable = new Set(S.combat_actionable || []);
    const actors = S.figures.filter(f => f.label && myControlled(f) && actionable.has(f.uid));
    if (combatResolvedTurn !== S.turn) {
      setHint("Choose each figure's attack, then resolve.");
      figureChecklist(c, actors);
      const idle = actors.filter(f => !PLAN[f.uid]).length;
      if (idle) warnLine(c, `${idle} figure${idle > 1 ? "s" : ""} will do nothing.`);
      bigPrimary(c, actors.length ? "Resolve attacks" : "Resolve combat", () => {
        combatResolvedTurn = S.turn;       // next render offers "End turn"
        executePlans("combat");
      });
    } else {
      setHint("Attacks resolved — push back any beaten foes, then end the turn.");
      drawForceRetreat(c);                 // post-combat shoves, if any
      bigPrimary(c, "End turn →", () => { resetAll(); act({type: "end_turn"}).then(after); });
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

// Placement: a move option that needs a destination hex + facing, then "Set action".
function drawPlacement(c) {
  const f = figByUid(sel);
  if (!f) { chosenOption = null; return; }
  const needHex = NEEDS_DEST.has(chosenOption);
  const swap = WEAPON_CHANGE.has(chosenOption) && (f.weapons || []).length > 1;
  if (swap && pendingReady == null) pendingReady = (f.weapons || []).find(w => w !== f.weapon) || f.weapon;
  const wrap = document.createElement("div"); wrap.style.marginTop = "8px";
  let html = `<div class="muted">Placing <b>${f.name}</b>: ${optLabel(chosenOption)}`
    + (needHex ? ` · dest ${pendingDest || "—"}` : "")
    + ` · facing ${pendingFacing === "auto" ? "→ enemy" : pendingFacing}</div>`;
  if (swap) html += `<div style="margin-top:6px">Ready: <select id="readySel">`
    + (f.weapons || []).map(w => `<option ${w === pendingReady ? "selected" : ""}>${w}</option>`).join("")
    + `</select></div>`;
  if (needHex && !pendingDest) html += `<div class="hint">Click a green hex to set the destination.</div>`;
  wrap.innerHTML = html; c.appendChild(wrap);
  if (swap) wrap.querySelector("#readySel").addEventListener("change", e => { pendingReady = e.target.value; });
  const facingNow = () => pendingFacing === "auto" ? f.facing : pendingFacing;
  addBtn(c, "⟲ turn", () => { pendingFacing = (facingNow() + 5) % 6; render(); });
  addBtn(c, "⟳ turn", () => { pendingFacing = (facingNow() + 1) % 6; render(); });
  addBtn(c, "Set action", () => {
    PLAN[sel] = {uid: sel, phase: "move", option: chosenOption, label: optLabel(chosenOption),
                 dest: pendingDest, facing: pendingFacing, ready: swap ? pendingReady : null};
    chosenOption = null; pendingDest = null; pendingReady = null; render();
  }, true, needHex && !pendingDest);
  addBtn(c, "Cancel", () => { chosenOption = null; pendingDest = null; pendingReady = null; render(); });
}

// ---- per-character pop-up options menu --------------------------------------
function closeMenu() { $("tokenMenu").style.display = "none"; }
function openMenu(f) {
  const rows = [];
  if (S.phase === "move") {
    // The full option set: available ones are clickable, unavailable ones are
    // shown disabled with their reason (issue #73), never hidden.
    for (const o of (optInfo.options || [])) {
      if (o.available === false)
        rows.push({label: optLabel(o.option), reason: o.reason, disabled: true});
      else
        rows.push({label: optLabel(o.option), act: () => chooseMoveOption(f, o.option)});
    }
    if (!rows.length) rows.push({label: "No moves available", muted: true});
  } else if (S.phase === "combat") {
    const grappling = (f.hth_opponents || []).length > 0;
    for (const uid of (optInfo._targets || [])) {
      const e = figByUid(uid);
      rows.push({label: `Attack ${e ? e.name : uid}`, act: () => setAttack(f, uid)});
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
      rows.push({label: `🤼 ${grappling ? "Strike" : "Grapple"} ${e ? e.name : uid}`,
                 act: () => setHth(f, uid)});
    }
    for (const uid of (optInfo.shield_rush_targets || [])) {
      const e = figByUid(uid);
      rows.push({label: `🛡 Shield-rush ${e ? e.name : uid}`,
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
  let html = `<div class="head">${f.name} <span class="chip ${f.side}">${f.side}</span></div>`;
  // A figure with no real choice (only "Do nothing" / disabled rows) is already
  // doing nothing — don't nag it as "uncommitted" (issue #117).
  const hasRealAction = rows.some(r => r.act && r.label !== "Do nothing");
  html += plan
    ? `<div class="commit">Committed: <b>${plan.label}</b>${plan.dest ? " → " + plan.dest : ""}</div>`
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
  sel = f.uid; pendingDest = null; pendingReady = null;
  // A weapon change with more than one carried weapon opens the placement panel so
  // the player explicitly picks which weapon to ready (#142) instead of auto-toggling.
  const pickWeapon = WEAPON_CHANGE.has(option) && (f.weapons || []).length > 1;
  if (NEEDS_DEST.has(option) || pickWeapon) {
    chosenOption = option; pendingFacing = "auto"; render();       // enter placement
  } else {
    const plan = {uid: f.uid, phase: "move", option, label: optLabel(option), facing: "auto"};
    if (WEAPON_CHANGE.has(option)) plan.ready = (f.weapons || []).find(w => w !== f.weapon) || f.weapon;
    if (option === "pick_up") {
      plan.ready = (optInfo.pickups || [])[0];
      plan.label = `Pick up ${plan.ready || "weapon"}`;
    }
    PLAN[f.uid] = plan; chosenOption = null; render();
  }
}
function setHth(f, target) {
  const e = figByUid(target);
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
function setAttack(f, target) {
  const e = figByUid(target);
  PLAN[f.uid] = {uid: f.uid, phase: "combat", target, label: `Attack ${e ? e.name : target}`};
  render();
}
function setDoNothing(f) {
  PLAN[f.uid] = {uid: f.uid, phase: "combat", none: true, label: "Do nothing"};
  render();
}
function clearPlan(f) {
  delete PLAN[f.uid];
  if (sel === f.uid) { chosenOption = null; pendingDest = null; }
  render();
}
function resetAll() { PLAN = {}; warnKind = null; resetSelection(); closeMenu(); }

async function executePlans(kind) {
  closeMenu();
  const plans = Object.values(PLAN).filter(p => p.phase === kind);
  if (kind === "move") {
    for (const p of plans)
      await act({type: "move", uid: p.uid, option: p.option, dest: p.dest,
                 facing: p.facing, ready: p.ready || null});
    resetAll(); await act({type: "end_side_move"}); after();
  } else {
    for (const p of plans) {
      if (p.disengage) await act({type: "hth_disengage", uid: p.uid});
      else if (p.disengageMove) await act({type: "disengage_move", uid: p.uid, dest: p.dest});
      else if (p.rush) await act({type: "shield_rush", uid: p.uid, target: p.target});
      else if (p.target) await act({type: p.hth ? "queue_hth" : "queue_attack",
                                    uid: p.uid, target: p.target});
    }
    resetAll(); await act({type: "resolve_combat"}); render();
  }
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
  // In combat, the relevant ranged-vs-melee target list rides ._targets.
  info._targets = (f.weapon && missileReady(f, info))
    ? info.missile_targets : info.melee_targets;
  optCache[f.uid] = info;
  return info;
}

async function onFigureClick(f) {
  flash("");
  if (S.phase === "move") {
    if (f.side !== S.moving_side) { flash(`${f.name} is on another side.`); return; }
    if (!f.can_act) { flash(`${f.name} can't move this turn.`); return; }
    sel = f.uid; chosenOption = null; pendingDest = null; pendingFacing = f.facing; pendingReady = null;
    optInfo = await loadOptions(f);
    render(); openMenu(f);
  } else if (S.phase === "combat") {
    if (!myControlled(f)) { flash(`${f.name} isn't yours to command.`); return; }
    sel = f.uid; pendingFacing = f.facing;
    optInfo = await loadOptions(f);
    render(); openMenu(f);
  } else {
    sel = f.uid; render();
  }
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
  if (S.phase === "move") return f.side === S.moving_side && !!f.can_act && myControlled(f);
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
  box.innerHTML = statusHeader(f) + planLine(f);
  if (!f.edit_spec) return;
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
  return `<div>${tokenBadge(f)} <b>${f.name}</b> <span class="chip ${f.side}">${f.side}</span></div>` +
    (f.model === "tarmar"
      ? `<div class="muted">Fatigue ${f.fatigue}/${f.max_fatigue} · Body ${f.body}/${f.max_body} · adjDX ${f.dx}</div>`
      : `<div class="muted">ST ${f.st}/${f.max_st} · adjDX ${f.dx}</div>`) +
    `<div class="muted">${f.posture}${f.engaged ? " · engaged" : ""}${f.dodging ? " · defending" : ""}` +
    `${(f.hth_opponents && f.hth_opponents.length) ? " · 🤼 grappling" : ""}</div>` +
    weaponsLine(f);
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
  if (p) return `<div style="margin-top:8px" class="muted">Action set: <b>${p.label}</b>`
    + `${p.dest ? " → " + p.dest : ""}</div>`;
  if ((S.phase === "move" && f.side === S.moving_side && f.can_act) ||
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
  const sides = (S.sides && S.sides.length)
    ? S.sides.slice() : Object.keys(S.controllers || {});
  const lead = S.winner && sides.includes(S.winner) ? S.winner
    : (S.moving_side && sides.includes(S.moving_side) ? S.moving_side : null);
  return lead ? [lead, ...sides.filter(s => s !== lead)] : sides;
}
function figActionHtml(f) {
  const plan = PLAN[f.uid];
  if (plan) return `<span class="action">${escapeHtml(plan.label)}`
    + `${plan.dest ? " → " + escapeHtml(plan.dest) : ""}</span>`;
  const canMove = S.phase === "move" && f.side === S.moving_side && f.can_act;
  const canFight = S.phase === "combat" && myControlled(f) && f.can_act;
  if ((canMove || canFight) && !f.dead) return `<span class="action todo">choose action</span>`;
  return `<span class="action idle">—</span>`;
}
function drawRoster() {
  const r = $("roster"); if (!r || !S) return;
  const byside = {};
  for (const f of S.figures) (byside[f.side] = byside[f.side] || []).push(f);
  let html = adminTagHtml();
  for (const side of orderedSides()) {
    html += `<div class="grouphd"><span class="chip ${side}">${escapeHtml(sideName(side))}</span>`
      + ` ${seatTag(side)}</div>`;
    for (const f of (byside[side] || [])) {
      const dead = f.dead || f.collapsed;
      const state = f.dead ? "dead" : f.collapsed ? "down"
        : `${hpCur(f)}/${hpMax(f)}` + (f.posture !== "standing" ? " · " + f.posture : "");
      html += `<div class="row${dead ? " dead" : ""}" data-uid="${escapeHtml(f.uid)}">`
        + `<span>${tokenBadge(f)} ${escapeHtml(f.name)} <span class="muted">${state}</span></span>`
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
  "Classic Melee": [
    {name:"Knight", strength:13, dexterity:11, weapon:"Broadsword", weapon2:"Mace", armor:"Plate", shield:"Large shield"},
    {name:"Swordsman", strength:12, dexterity:12, weapon:"Shortsword", weapon2:"Mace", armor:"Chainmail", shield:"Small shield"},
    {name:"Spearman", strength:13, dexterity:11, weapon:"Spear", weapon2:"Shortsword", armor:"Leather", shield:"None"},
  ],
  "Tarmar": [
    {name:"Knight", strength:13, dexterity:11, intelligence:10, wisdom:10, constitution:11, charisma:10, weapon:"Broadsword", weapon2:"Mace", armor:"Plate", shield:"Large shield", skill:3, skill2:1},
    {name:"Swordsman", strength:12, dexterity:12, intelligence:10, wisdom:10, constitution:11, charisma:10, weapon:"Shortsword", weapon2:"Mace", armor:"Chainmail", shield:"Small shield", skill:3, skill2:1},
    {name:"Spearman", strength:13, dexterity:11, intelligence:10, wisdom:10, constitution:10, charisma:10, weapon:"Spear", weapon2:"Shortsword", armor:"Leather", shield:"None", skill:2, skill2:1},
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
  const teams = parseInt($("teams").value, 10), perTeam = parseInt($("perTeam").value, 10);
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
    + SAVED.map(c => `<option value="${c.id}">${c.name}</option>`).join("");
}
function applySpecToCard(card, spec) {        // fill a card from a saved spec (keep its team)
  if (spec.name != null) card.querySelector("[data-name]").value = spec.name;
  card.querySelectorAll("[data-stat]").forEach(i => { if (spec[i.dataset.stat] != null) i.value = spec[i.dataset.stat]; });
  card.querySelectorAll("[data-eq]").forEach(s => { if (spec[s.dataset.eq] != null) s.value = spec[s.dataset.eq]; });
  card.querySelectorAll("[data-skillkey]").forEach(i => { if (spec[i.dataset.skillkey] != null) i.value = spec[i.dataset.skillkey]; });
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
    + `<input data-name value="${f.name}" style="width:130px"></div>`
    + `<div style="margin-top:6px">${stats} <span class="muted" data-budget></span></div>`
    + `<div style="margin-top:6px">Weapon <select data-eq="weapon">${optionTags(CAT.weapons, f.weapon)}</select> ${skillInput("skill", f.skill)}</div>`
    + `<div style="margin-top:6px">Weapon 2 <select data-eq="weapon2"><option ${!f.weapon2 || f.weapon2 === "None" ? "selected" : ""}>None</option>${optionTags(CAT.weapons, f.weapon2)}</select> ${skillInput("skill2", f.skill2)}</div>`
    + `<div style="margin-top:6px">Armour <select data-eq="armor">${optionTags(CAT.armors, f.armor || "None")}</select> `
    + `Shield <select data-eq="shield">${optionTags(CAT.shields, f.shield || "None")}</select></div>`
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
  return f;
}

function disableByStrength(select, strength, offset) {
  CAT.weapons.forEach((w, idx) => {
    const opt = select.options[idx + offset];
    if (opt) opt.disabled = (w.str_req || 0) > strength;
  });
}

function refreshCard(card) {
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
  const teams = [...new Set(fighters.map(f => f.side))];   // one AI team = the last
  const computer = ($("mode") && $("mode").value === "pxai") ? teams[teams.length - 1] : "";
  const practice = $("practiceMode") && $("practiceMode").checked;
  const body = {profile: $("profile").value, computer, fighters, practice};
  const data = await api("/api/game/new_custom", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)});
  if (data.error) { $("editorErr").textContent = "Can't start: " + data.error; return; }
  GID = data.gid; LAYOUT = data.layout; S = data.state; PROFILE = data.profile;
  captureOwnership(data); history.replaceState({}, "", `/game/${GID}`);
  closeEditor(); closeSetup(); closeLiveEdit(); resetSelection(); render();
}

// Configurable colours: each corner swatch drives one or more CSS variables and
// remembers the choice in localStorage. bg swatch also recolours the controls.
const THEME = { bgColor: ["--bg", "--panel"], textColor: ["--ink"], hexColor: ["--hex"] };
const RESET_VARS = ["--bg", "--panel", "--ink", "--hex"];
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
  }
  syncMuted();
}
function applyTheme() {
  for (const [id, vars] of Object.entries(THEME)) {
    const input = $(id);
    const saved = localStorage.getItem("melee.theme." + id);
    if (saved) vars.forEach(v => root().style.setProperty(v, saved));
    const current = cleanHex(saved) || cleanHex(getComputedStyle(root()).getPropertyValue(vars[0]));
    if (current) input.value = current;
    input.addEventListener("input", () => {
      vars.forEach(v => root().style.setProperty(v, input.value));
      localStorage.setItem("melee.theme." + id, input.value);
      ensureTextContrast();
    });
  }
  ensureTextContrast();
}
function resetTheme() {
  ["bgColor", "textColor", "hexColor"].forEach(id => localStorage.removeItem("melee.theme." + id));
  RESET_VARS.forEach(v => root().style.removeProperty(v));   // fall back to the CSS :root defaults
  const cs = getComputedStyle(root());
  for (const [id, vars] of Object.entries(THEME)) {
    const c = cleanHex(cs.getPropertyValue(vars[0]));
    if (c) $(id).value = c;
  }
  syncMuted();
}

applyTheme();
// Deep link: /game/<gid> joins or spectates an existing game; otherwise start fresh.
const urlGid = (location.pathname.match(/^\/game\/([^/]+)/) || [])[1];
if (urlGid) { GID = urlGid; refresh(); } else { bootGame(); }
// Shared view: poll so every browser on this game sees moves as they happen.
// Re-render only when the server state actually changed, to avoid flicker.
let _lastStateJSON = "";
const POLL = setInterval(async () => {
  if (!GID) return;
  const data = await api(`/api/game/${GID}`);
  if (data.error) { clearInterval(POLL); return; }   // game gone — stop polling
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
  newGame, endGame, setOpponent,
  openAdmin, closeAdmin, adminCreateUser, adminSelectUser, adminDeleteUser,
  adminDeleteChar, adminCreateCharFor,
  openEditor, closeEditor, startCustom,
  copyLink, seatAction, closeLiveEdit, resetTheme,
});
