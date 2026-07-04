# Melee — project status & handoff

_Last updated: 2026-07-03._

A digital **The Fantasy Trip: Melee** that can also be played under a **Tarmar
d20** rule set — **live in production at <https://melee.origamisoftware.com>**.
Browser SVG arena; a New-game **setup wizard**; **2–5 teams** of **1–3
combatants**; a state-aware computer opponent; a validated **pick / generate /
edit** fighter editor; **multiplayer over a shared link** (claimable seats);
**optional accounts** with **saved characters**; **save/load of in-progress
games**; an **admin role**; and a fully narrated combat log with a diagnostic
🐞 trail behind it.

## The four repos (all public, under `~/dev`)

| Repo | Role |
|---|---|
| **hexarena** | shared hex-grid library (coords, **injectable dice incl. `dn(sides)` / d20**, pathfinding, layout). |
| **tarmar-rules** | shared **Tarmar d20 combat core** (weapon-class × armour-tier matrix, modifiers, crit, Hybrid armour), extracted byte-identical from `tarmar-studio`. Single source of truth for the d20 math. |
| **origami-auth** | shared **reusable Django auth app** (renamed and generalized from `tarmar-auth`): `AbstractOrigamiUser` + register/login/logout/profile/users-admin. **Pinned to a release tag** in `requirements.txt` (currently `v0.2.0`) — treat it as a library; upgrade the pin deliberately. Its own dependency `origami-common` must be listed explicitly (pip doesn't resolve a git dep's transitive git deps). |
| **melee** | the game. `engine/` = pure-Python rules; `board/` = Django SVG board + JSON API. Depends on the libs via git URLs in `requirements.txt` (CI) or `pip install -e ../hexarena ../tarmar-rules ../origami-auth` (local). |

> Sibling project: **`~/Documents/dev/tarmar-studio`** — the Django "second
> brain" where the Tarmar d20 combat system was first built (design spec:
> `reference/content/proposals/d20-combat-resolution-spec.md`). It keeps its
> own user model and was not modified for accounts.

## Architecture (melee)

- **Structure vs. policy split.** Arena, facing/engagement, turn sequence,
  movement, and the option catalog are structural and shared. *Mechanics* swap.
- **`engine/profile.py` — `RulesProfile = (figure stat model + Ruleset)`**, picked
  as a unit. `CLASSIC` (ST/DX, 3d6-under-adjDX) and `TARMAR` (six attributes →
  Fatigue/Body, d20 roll-over).
- **Combat resolution.** Classic in `engine/ruleset.py` + `engine/combat.py`;
  Tarmar in `engine/tarmar.py` reading the shared **`tarmar_rules`** package.
- **`engine/state.py`** — the game state, decomposed into mixins after the
  god-class split (#156/#189); dispatch in `board/views.py` is a table of
  per-action handlers (#159/#182).
- **`engine/chargen.py`** — validated character builder; **`engine/names.py`** —
  generated fighter names with occasional titles/epithets (#224).
- **`engine/monsters.py` / `engine/megahex.py` / `engine/experience.py`** —
  nonhumans and multi-hex figures (giant, flying gargoyle), megahex-accurate
  missile range, Section IX experience & advancement.
- **`engine/ai.py`** — heuristic computer opponent (no LLM), rebuilt in #210 to
  manoeuvre and play a state-aware strategy instead of standing and shooting.
- **`engine/narrative.py`** — the play-by-play log. Separately, a **diagnostic
  trail** (#222) records every dispatched action + a state snapshot per game;
  read it back from `GET /api/game/<gid>/debug`.
- **`engine/invariants.py`** — the regression net's single source of truth for
  what must never happen in a fight (see Tests below).
- **Board UI** — a static ES module (`board/static/board/board.js`, extracted
  from the old inline SPA in #161/#185) rendering a **three-panel layout**
  (Map / Game Control / Characters, #192) with per-character
  initiative-ordered action controls + a Pass rule (#196), inline action lists,
  and **selectable light/dark/preset themes** (#194/#216).
- **Multi-team setup** — `board/scenario.py` places 2–5 colour-coded teams
  around a square arena (red/blue/green/gold/violet).
- **Multiplayer & ownership** — games are bound to their creating session and
  actions are authorized by seat (#74); the creator can open human seats that
  others claim over a shared link (#85). Vs-computer games hide the invite.
- **Admin role** (#86/#140) — Django admin for user/character CRUD, seat-
  ownership bypass, and out-of-rules character edits.
- **Persistence** — live games sit in a bounded in-memory store (`GAMES`,
  most-recently-touched 512); `board/persistence.py` gives lossless JSON
  save/load of in-progress games (#12), stored in `board/models.SavedGame`.
  `SavedCharacter` stores a player's fighters. The game is fully playable
  anonymously.

## What's shipped (all merged to `main`)

Summarized by era — ~230 issues are closed; read `gh issue list --state closed`
for the full record.

1. **Dual-ruleset foundation (→ #30):** Ruleset seam, the Tarmar d20 profile,
   the setup wizard, multi-team play, the fighter editor, accounts + saved
   characters.
2. **The deferred Melee mechanics (#1–#13) — all shipped:** hand-to-hand,
   thrown/missile line-of-flight, pole-weapon jab/charge, disengaging,
   megahex missile range, nonhumans/monsters (incl. multi-hex + flying),
   experience & advancement, figure builder, game persistence, posture flow.
3. **Audit rounds (#74–#158):** security (session ownership #74, hardened prod
   settings #75), rulebook-correctness sweeps against the physical rules, and
   structural refactors (god-class split, dispatch table, SPA extraction,
   engine dedupe).
4. **Multiplayer (#85) and the admin role (#86).**
5. **Deployment (#132):** blue-green to datahorde, plus the migration-ordering
   fix (#169). Live at <https://melee.origamisoftware.com>.
6. **UX rework (#176–#216):** the three-panel board redesign (#192),
   initiative-ordered per-character controls (#196), themes (#194/#216),
   readied-weapon choice at setup (#207).
7. **Playtest-hardening sweep (→ #232):** smarter AI (#210), missile-UI and
   resolve-gate deadlock fixes (#204/#217/#220), friendly-fire and truthful
   narration (#229), generated names (#224), the diagnostic log (#222),
   Playwright e2e in CI (#143/#164), and the **regression harness (#231)**.

## Run it

```bash
cd ~/dev/melee && git pull
pip install -e ../hexarena -e ../tarmar-rules -e ../origami-auth   # once
python manage.py migrate                                           # accounts + saved games
python manage.py runserver                                         # http://127.0.0.1:8000/
```

**New game** opens the wizard: **Rules** (Classic / Tarmar) → **Mode** (vs
Computer / same screen / shared link) → **Teams** (2–5) → **Combatants** (1–3)
→ optionally **Pick / generate / edit fighters…** (🎲 Generate; 💾 Save / Load
saved when logged in) → **Begin**. Header **Log in** link → `/accounts/`.

## Deploy & verify

- **Push to `main` deploys automatically** — `.github/workflows/deploy.yml`
  calls the shared `smarks/ops-workflows` blue-green workflow, which runs
  `deploy.sh` on datahorde: build the inactive env, health-check it (curl with
  `Host: melee.origamisoftware.com`), flip the nginx upstream. Ports **9072
  (blue) / 9073 (green)**; systemd units `melee-blue` / `melee-green`.
- **Manual control:** `gh workflow run Deploy` (blank = deploy), or with
  `command=rollback` / `command=status`. Check runs: `gh run list --workflow Deploy`.
- **Verify:** the workflow emails on failure (and on green-after-red);
  `/usr/bin/curl -sI https://melee.origamisoftware.com/` should answer.
- **Known gotcha (#169):** a fresh DB must apply `origami_auth.0001` before
  `board.0001` or migrate fails with `InconsistentMigrationHistory` — fixed in
  the migration graph, but remember it if migrations are ever squashed.

## Tests

```bash
python -m pytest -q     # in each repo
```

**397 (melee)** · 27 (hexarena) · 10 (tarmar-rules) · 11 (origami-auth) — all
green as of #232 (2026-07-03). Playwright e2e tests live in `e2e/` and run in
CI (see `e2e/README.md`). Gold standards: `engine/tests/test_combat_example.py`
(the rulebook Flavius-vs-Wulf fight); `test_tarmar.py`; `test_chargen.py`;
`test_scenario.py` (in `board/tests/`).

**The regression net (#231)** — see `engine/tests/README.md`:

- `engine/invariants.py` — `assert_state_invariants` / `assert_log_truthful`:
  no same-side damage, a truthful combat log, no double action, legal
  positions/pools, no dangling `_pending`, missile sanity.
- `engine/tests/test_soak.py` — randomized AI-vs-AI full games across both
  profiles, invariants checked after every action. CI runs a bounded count;
  `MELEE_SOAK=500 pytest` or `pytest -m slow` runs the larger sweep. On a
  break it prints the seed so the exact game replays.
- **Standing rule: every new bug adds either a new invariant or a failing-case
  test — reproduce it red first, then fix to green.** A fix without one is
  incomplete.

## Working conventions (important)

- **Do commit work in a throwaway git worktree**, never the shared `~/dev/melee`
  checkout: `git worktree add ~/dev/melee-claude-<issue> -b claude/<feature> origin/main`
  → build → `gh pr create` → merge → `git worktree remove`.
  **Stage explicit files; never `git add -A/-u`.**
- **Multi-Claude coordination** (see the repo `CLAUDE.md`): only take
  unassigned open issues; claim by assigning `@me` **and** posting a claim
  comment; release both if you stop.
- After a PR merges on GitHub the local `~/dev/melee` lags `origin/main` —
  `git pull` first.
- The shared libs are published: hexarena / tarmar-rules float on `@main`
  (bump + push before a dependent melee change so CI resolves it);
  **origami-auth is pinned to a release tag** — cut a release and bump the pin.

## Open threads / next ideas

- **#234 — save the current game's fighters as saved characters** (unclaimed).
- **#235 — accounts polish** (unclaimed): profile editing, password reset.
- **Mana / magic — the Wizard expansion.** The spell system exists in the
  Tarmar spec and tarmar-studio; bringing wizards to the arena is the natural
  next big milestone. No issue filed yet.
- Smaller, older idea (no issue): lift `chargen`'s Tarmar stat rules into
  `tarmar-rules` so tarmar-studio's character creator shares the validator.

See also `docs/tarmar-ruleset-integration.md` for the original dual-ruleset
design and the Melee↔Tarmar mapping, and `docs/reference/` for source specs.
