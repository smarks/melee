# Melee — project status & handoff

_Last updated: 2026-07-10._

A digital **The Fantasy Trip: Melee** that can also be played under a **Tarmar
d20** rule set — **live in production at <https://melee.origamisoftware.com>**.
Browser SVG arena; a New-game **setup wizard**; **2–5 teams** of **1–3
combatants**; a **Wizards** game mode with **Classic spellcasting**; a
state-aware computer opponent; a validated **pick / generate / edit** fighter
(and wizard) editor; **multiplayer over a shared link** (claimable seats);
**optional accounts** with **saved characters**; **save/load of in-progress
games**; an **admin role**; and a fully narrated combat log with a diagnostic
🐞 trail behind it.

## The four repos (all public, under `~/dev`)

| Repo | Role |
|---|---|
| **hexarena** | shared hex-grid library (coords, **injectable dice incl. `dn(sides)` / d20**, pathfinding, layout). |
| **tarmar-rules** | shared **Tarmar d20 combat core** (weapon-class × armour-tier matrix, modifiers, crit, Hybrid armour), extracted byte-identical from `tarmar-studio`. Single source of truth for the d20 math. |
| **origami-auth** | shared **reusable Django auth app** (renamed and generalized from `tarmar-auth`): `AbstractOrigamiUser` + register/login/logout/profile/users-admin. **Pinned to a release tag** in `requirements.txt` (currently `v0.3.2`) — treat it as a library; upgrade the pin deliberately. Its own dependency `origami-common` (pinned `v0.2.1`) must be listed explicitly (pip doesn't resolve a git dep's transitive git deps). |
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
- **`engine/chargen.py`** — validated character (and **wizard**) builder;
  **`engine/names.py`** — generated fighter names with occasional titles/epithets
  (#224).
- **`engine/spells.py` + Classic magic** — the **Wizard** milestone (Classic
  ruleset only; Tarmar mana deferred). ST doubles as the mana pool; the `Ruleset`
  seam gained `resolve_spell`, and `absorbed()` handles spell protection. Ships
  **Magic Fist** (missile, 1d/ST) and **Stone Flesh** (protection). A wizard is a
  `Figure` with raised IQ + a spell list, built bare-handed so it can cast; casting
  is a combat-phase action queued like an attack, with an ST/mana slider for missile
  spells. A **Wizards game mode** seats one fighter + one wizard per side.
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
  from the old inline SPA in #161/#185) rendering a **five-panel
  draggable/resizable windowed layout** (Map / Game Control / Characters /
  Selected-character / Action, #319–#330) with per-character initiative-ordered
  action controls + a Pass rule (#196), inline action lists, clear
  enabled/disabled controls with reasons (#331), and **selectable
  light/dark/preset themes** (#194/#216). Pure decision logic lives in unit-tested
  ES modules **`control_state.js`** (which control state to show) and
  **`layout_geom.js`** (panel geometry), run by a `node --test` **js-unit** CI job
  (#364/#366).
- **Multi-team setup** — `board/scenario.py` places 2–5 colour-coded teams
  around a square arena (red/blue/green/gold/violet).
- **Multiplayer & ownership** — games are bound to their creating session and
  actions are authorized by seat (#74); the creator can open human seats that
  others claim over a shared link (#85). Vs-computer games hide the invite. In a
  networked combat, Resolve is **server-coordinated**: it waits until every human
  side has committed, so the first player's Resolve never discards another's
  queued attacks (#334). A committed attacker with no shot the player wants can
  **Hold fire** (stand down) so a turn can never deadlock (#397/#398). The whole
  two-browser remote path is guarded by `e2e/test_multiplayer.py`.
- **Admin role** (#86/#140) — Django admin for user/character CRUD, seat-
  ownership bypass, and out-of-rules character edits.
- **Persistence** — live games sit in a bounded in-memory store (`GAMES`,
  most-recently-touched 512); `board/persistence.py` gives lossless JSON
  save/load of in-progress games (#12), stored in `board/models.SavedGame`.
  `SavedCharacter` stores a player's fighters. The game is fully playable
  anonymously.

## What's shipped (all merged to `main`)

Summarized by era — ~400 issues/PRs are closed; read `gh issue list --state closed`
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
8. **Deep audit rounds 1–3 + fixes (#239–#343):** three multi-agent audits,
   each finding adversarially verified, then cleared. Stored-XSS on fighter names
   (#243), seat-auth bypass on combat actions (#244), a per-game mutation lock
   (#253), autosave so a worker restart can't orphan a live game (#275/#276),
   networked-combat coordination (#333 deadlock, #334 resolve-sync), and many
   rules-correctness fixes. Deploy became CI-gated on e2e (#246).
9. **DRY / testability refactor + coverage (#357–#385):** `pytest-cov` in CI
   (report-only, ~95%); single-source chokepoints for figure state, targeting
   (#362), and the per-game lock; the client decision logic extracted into the
   unit-tested `control_state.js` / `layout_geom.js` modules with a new js-unit CI
   job. One real latent bug found and fixed (mid-fight edit stripping monster
   traits, #359).
10. **Windowed panel UI (#319–#332):** the board became five draggable, resizable,
    persisted panels (move/snap/resize/minimize/maximize), a narrow-screen stacked
    fallback, a character/action split, and unmistakable enabled-vs-disabled
    controls carrying their reason.
11. **UI-coverage guarantee (#387/#388):** every interactive control is asserted to
    produce its effect, backed by a **dead-control guard** e2e that fails if any
    enabled control is a silent no-op.
12. **Wizard / mana milestone (2026-07-10):** Classic spellcasting — the Wizards
    game mode, Magic Fist + Stone Flesh, an IQ-gated spell picker that works like
    weapon selection, and editable wizards. Merged + deployed.
13. **Two-human hang fix + multiplayer test net (#397/#398, 2026-07-10):** the
    Hold-fire escape hatch, plus a deep **two-browser** multiplayer e2e suite
    (`e2e/test_multiplayer.py`) covering seat claim, ownership isolation, the
    resolve-sync, the networked hang escape, and networked wizard casting.

## Run it

```bash
cd ~/dev/melee && git pull
pip install -e ../hexarena -e ../tarmar-rules -e ../origami-auth   # once
python manage.py migrate                                           # accounts + saved games
python manage.py runserver                                         # http://127.0.0.1:8000/
```

**New game** opens the wizard: **Rules** (Classic / **Wizards** / Tarmar) →
**Mode** (vs Computer / same screen / shared link) → **Teams** (2–5) →
**Combatants** (1–3) → optionally **Pick / generate / edit fighters…** (🎲
Generate; 🔮 Wizard; 💾 Save / Load saved when logged in) → **Begin**. Picking
**Wizards** opens the editor pre-seeded with a fighter + a wizard per side so you
choose each wizard's spells before **Start match**. Header **Log in** link →
`/accounts/`.

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

melee runs three CI jobs: **`test`** (~590 unit, `pytest -q` with `--cov`,
report-only), **`e2e`** (~137 Playwright tests in `e2e/`, incl. the two-browser
`test_multiplayer.py`), and **`js-unit`** (39 `node --test` cases over the pure
`control_state.js` / `layout_geom.js` modules). Libs: 27 (hexarena) · 10
(tarmar-rules) · 11 (origami-auth). All green on `main`. Gold standards:
`engine/tests/test_combat_example.py` (the rulebook Flavius-vs-Wulf fight);
`test_tarmar.py`; `test_chargen.py`; `test_scenario.py` + `test_spells.py`.

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
  checkout: `git worktree add ~/dev/melee-<feature> -b <feature> origin/main`
  → build → `gh pr create` → merge → `git worktree remove`. Branch names are
  **plain** (`combat-resolve-hang`, not `fix/…` or `claude/…`).
  **Stage explicit files; never `git add -A/-u`.**
- **Multi-Claude coordination** (see the repo `CLAUDE.md`): only take
  unassigned open issues; claim by assigning `@me` **and** posting a claim
  comment; release both if you stop.
- After a PR merges on GitHub the local `~/dev/melee` lags `origin/main` —
  `git pull` first.
- The shared libs are **all pinned to release tags** in `requirements.txt`
  (hexarena `v0.1.0`, tarmar-rules `v0.2.1`, origami-common `v0.2.1`, origami-auth
  `v0.3.2`) — never `@main` (a moving tag once broke a CI push, #246). To change a
  lib: cut a new release tag there, then bump the pin here.

## Open threads / next ideas

The ~230-issue bug/audit backlog is **cleared** — every filed audit finding and
playtest bug through 2026-07-10 is merged, deployed, and prod-green. Remaining:

- **#399 — remote players can't edit their characters pre-game** (open): a
  remote joiner has no game link until the game exists, so they can't reach the
  editor beforehand. Needs a pre-game lobby / shareable invite where each seated
  human builds its own roster. The natural next multiplayer feature.
- **Wizard milestone follow-ups** (no issues yet): more spells beyond Magic Fist /
  Stone Flesh; **Tarmar mana** (deliberately deferred — this milestone was
  Classic-only); an AI that casts.
- **#234 — save the current game's fighters as saved characters** (unclaimed).
- **#235 — accounts polish** (unclaimed): profile editing, password reset.
- Smaller, older idea (no issue): lift `chargen`'s Tarmar stat rules into
  `tarmar-rules` so tarmar-studio's character creator shares the validator.

See also `docs/tarmar-ruleset-integration.md` for the original dual-ruleset
design and the Melee↔Tarmar mapping, and `docs/reference/` for source specs.
