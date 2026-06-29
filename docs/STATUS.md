# Melee — project status & handoff

_Last updated: 2026-06-27._

A digital **The Fantasy Trip: Melee** that can also be played under a **Tarmar
d20** rule set. Browser SVG arena; a New-game **setup wizard**; **2–5 teams** of
**1–3 combatants**; a heuristic computer opponent; a validated **pick / generate
/ edit** fighter editor; **optional accounts** with **saved characters**; and a
fully narrated combat log.

## The four repos (all public, under `~/dev`)

| Repo | Role |
|---|---|
| **hexarena** | shared hex-grid library (coords, **injectable dice incl. `dn(sides)` / d20**, pathfinding, layout). |
| **tarmar-rules** | shared **Tarmar d20 combat core** (weapon-class × armour-tier matrix, modifiers, crit, Hybrid armour), extracted byte-identical from `tarmar-studio`. Single source of truth for the d20 math. |
| **tarmar-auth** | shared **reusable Django login app** (a concrete `User` model + register/login/logout/profile views, forms, templates). melee uses it for accounts. |
| **melee** | the game. `engine/` = pure-Python rules; `board/` = Django SVG board + JSON API. Depends on the three libs via `git+https@main` (CI) or `pip install -e ../hexarena ../tarmar-rules ../tarmar-auth` (local). |

> Sibling project: **`~/Documents/dev/tarmar-studio`** — the Django "second brain"
> where the Tarmar d20 combat system was first built and shipped (design spec:
> `reference/content/proposals/d20-combat-resolution-spec.md`). It has its **own**
> custom user model; it was **not** modified for accounts (swapping a live
> `AUTH_USER_MODEL` is unsafe). It could adopt `tarmar-auth`'s *views* later.

## Architecture (melee)

- **Structure vs. policy split.** Arena, facing/engagement, turn sequence,
  movement, and the option catalog are structural and shared. *Mechanics* swap.
- **`engine/profile.py` — `RulesProfile = (figure stat model + Ruleset)`**, picked
  as a unit. `CLASSIC` (ST/DX, 3d6-under-adjDX) and `TARMAR` (six attributes →
  Fatigue/Body, d20 roll-over).
- **Combat resolution.** Classic in `engine/ruleset.py` + `engine/combat.py`;
  Tarmar in `engine/tarmar.py` reading the shared **`tarmar_rules`** package.
- **`engine/chargen.py`** — validated character builder (`catalog` / `stat_rules`
  / `validate` / `build`); the Tarmar stat rules are isolated for later reuse.
- **`engine/ai.py`** — heuristic computer opponent (no LLM). `board/views.py`
  `_advance_computer` plays computer teams; `_auto_end_if_idle` ends idle turns.
- **`engine/narrative.py`** — the running play-by-play log.
- **Multi-team setup** — `board/scenario.build_game` / `build_custom_skirmish`
  place 2–5 colour-coded teams around a square arena; `TEAM_IDS` = red/blue/green/
  gold/violet. The `board/views.py` new-game endpoints take `teams`/`per_team`/
  `mode` (P×AI = one AI team, the last; P×P = all human).
- **Accounts** — `tarmar_auth` provides login; `board/models.SavedCharacter`
  (owner+name unique, profile, spec JSON) stores a player's fighters via
  login-gated, CSRF-protected endpoints. The game is fully playable anonymously.

## What's shipped (all merged to `main`)

- **Dual-ruleset foundation:** Ruleset seam, the Tarmar (d20) profile, the
  hexarena d20, and the public `tarmar-rules` repo.
- **#14–#22 — gameplay & UI:** computer opponent; move-destination highlight;
  combat narration; disabled-aware controls; default-vs-Computer + lighter theme
  + auto-end idle turns + health bars; two weapons; mid-fight weapon switching;
  full-operation narration.
- **#23 — `docs/STATUS.md`.**
- **#24–#28 — the flow-doc rework:** (#24) side-panel layout; (#25) New-game
  **setup wizard + multi-team** engine (+#26 one AI team); (#27) **per-character
  movement & combat flow** (auto-advance, Full/Half move, Done-with-phase, Do
  nothing); (#28) **pick / generate / edit characters per team**.
- **#29–#30 — accounts:** (#29) login via the shared `tarmar-auth` app;
  (#30) **saved characters** (save/reuse fighters when logged in).

## Run it

```bash
cd ~/dev/melee && git pull
pip install -e ../hexarena -e ../tarmar-rules -e ../tarmar-auth   # once
python manage.py migrate                                          # accounts tables
python manage.py runserver                                        # http://127.0.0.1:8000/
```

**New game** opens the wizard: **Rules** (Classic / Tarmar) → **Mode** (vs
Computer / same screen) → **Teams** (2–5) → **Combatants** (1–3) → optionally
**Pick / generate / edit fighters…** (🎲 Generate; 💾 Save / Load saved when
logged in) → **Begin**. Header **Log in** link → `/accounts/`. Game state is
in-memory per game id; the DB holds only accounts + saved characters.

## Tests

```bash
python -m pytest -q     # in each repo
```

~111 (melee) · 27 (hexarena) · 10 (tarmar-rules) · 3 (tarmar-auth). Gold
standards: melee's `engine/tests/test_combat_example.py` (the rulebook
Flavius-vs-Wulf fight); `test_tarmar.py` (the d20 system); `test_chargen.py`
(validation); `test_scenario.py` (multi-team placement).

## Working conventions (important)

- **Do commit work in a throwaway git worktree**, never the shared `~/dev/melee`
  checkout: `git worktree add ~/dev/melee-claude -b claude/<feature> origin/main`
  → build → `gh pr create` → `gh pr merge --squash --delete-branch` →
  `git worktree remove`. **Stage explicit files; never `git add -A/-u`.**
- After a PR merges on GitHub the local `~/dev/melee` lags `origin/main` —
  `git pull` first.
- The shared libs (hexarena / tarmar-rules / tarmar-auth) are published; bump +
  push them before a melee change that depends on new lib code, so melee CI
  resolves it.

## Open threads / next ideas

- **Accounts polish:** profile editing, password reset, "save my current game's
  fighters as characters". tarmar-studio could share `tarmar-auth`'s view/form
  layer via a careful refactor (its user model stays put).
- **Reuse:** lift `chargen`'s Tarmar stat rules into `tarmar-rules` so
  tarmar-studio's character creator shares the validator.
- **Force retreat** works in the engine/API but has no UI button.
- **Tarmar combat — deferred refinements:** severe-crit confirm roll, fumble
  drop/break on a natural 1, mana/magic (exist in the spec / tarmar-studio).
- **Deferred Melee mechanics** (issues #1–#13): thrown-weapon line-of-flight,
  fuller hand-to-hand, pole-weapon jab/charge, disengage rolls, megahex-accurate
  missile range, monsters/nonhumans, experience & advancement.
- **Polish:** a full light theme (current is a lightened dark theme); wizard /
  editor layout; multi-team board readability with 4–5 teams.

See also `docs/tarmar-ruleset-integration.md` for the original dual-ruleset
design and the Melee↔Tarmar mapping.
