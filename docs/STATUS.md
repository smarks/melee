# Melee — project status & handoff

_Last updated: 2026-06-26._

A digital **The Fantasy Trip: Melee** that can also be played under a **Tarmar
d20** rule set, with a browser SVG arena, a heuristic computer opponent, a
validated pre-match fighter editor, and a fully narrated combat log.

## The three repos (all public, under `~/dev`)

| Repo | Role |
|---|---|
| **hexarena** | shared hex-grid library (coords, **injectable dice incl. `dn(sides)` / d20**, pathfinding, layout). |
| **tarmar-rules** | shared **Tarmar d20 combat core** (weapon-class × armour-tier matrix, modifiers, crit, Hybrid armour), extracted byte-identical from `tarmar-studio`. Single source of truth for the d20 math. |
| **melee** | the game. `engine/` = pure-Python rules; `board/` = Django SVG board + JSON API. Depends on both libs via `git+https@main` (CI) or `pip install -e ../hexarena ../tarmar-rules` (local). |

> Sibling project: **`~/Documents/dev/tarmar-studio`** — the Django "second brain"
> where the Tarmar d20 combat system was first built and shipped (its design spec:
> `reference/content/proposals/d20-combat-resolution-spec.md`).

## Architecture (melee)

- **Structure vs. policy split.** The arena, facing/engagement, turn sequence,
  movement, and options are structural and shared. *Mechanics* are swappable.
- **`engine/profile.py` — `RulesProfile = (figure stat model + Ruleset)`**, picked
  as a unit. `CLASSIC` (ST/DX figures, 3d6-under-adjDX) and `TARMAR` (six
  attributes → Fatigue/Body, d20 roll-over).
- **Combat resolution.** Classic lives in `engine/ruleset.py` + `engine/combat.py`.
  Tarmar lives in `engine/tarmar.py` (`TarmarFigure`, `TarmarRuleset`) and reads
  the shared **`tarmar_rules`** package — no duplicated math.
- **`engine/chargen.py`** — the validated pre-match character builder
  (`catalog` / `stat_rules` / `validate` / `build`). The Tarmar stat rules are
  isolated here for later reuse in tarmar-studio (see "Open threads").
- **`engine/ai.py`** — heuristic computer opponent (no LLM). `board/views.py`
  `_advance_computer` auto-plays computer sides; `_auto_end_if_idle` ends a turn
  with nothing left to do.
- **`engine/narrative.py`** — the running play-by-play log (combat + every
  non-combat operation).

## What's shipped (all merged to `main`)

Dual-ruleset foundation (direct merges):
- `375f99c` Ruleset seam · `8681801` Tarmar (d20) profile · hexarena d20 push ·
  new public `tarmar-rules` repo.

Feature PRs:
- **#14** heuristic computer opponent
- **#15** keep chosen move-destination highlighted until Confirm
- **#16** combat narrated as a running play-by-play
- **#17** clearer, disabled-aware status & control UI
- **#18** default to vs-Computer, lighter theme, auto-end idle turns, health bars
- **#19** validated pre-match fighter editor (`/api/catalog`, `/api/game/new_custom`)
- **#20** two weapons per fighter (plus the dagger)
- **#21** switch the ready weapon mid-fight (Ready Weapon / Change Weapons)
- **#22** narrate every operation (initiative, moves, swaps, retreats, turn markers)

## Run it

```bash
cd ~/dev/melee && git pull
pip install -e ../hexarena -e ../tarmar-rules     # once
python manage.py runserver                        # http://127.0.0.1:8000/
```

New-game panel: pick **Rules** (Classic / Tarmar), **Opponent** (vs Computer /
hot-seat), and optionally **Customize…** to edit fighters. Game state is
in-memory server-side; no DB migrations needed.

## Tests

```bash
python -m pytest -q     # in each repo
```

~85 (melee) · 27 (hexarena) · 10 (tarmar-rules). Gold standards: melee's
`engine/tests/test_combat_example.py` reproduces the rulebook's Flavius-vs-Wulf
fight; `test_tarmar.py` pins the d20 system; `test_chargen.py` locks validation.

## Working conventions (important)

- **Do commit work in a throwaway git worktree**, never in the shared
  `~/dev/melee` checkout: `git worktree add ~/dev/melee-claude -b claude/<feature>
  origin/main` → build → `gh pr create` → `gh pr merge --squash --delete-branch`
  → `git worktree remove`. **Stage explicit files; never `git add -A/-u`.**
- After a PR merges on GitHub, the local `~/dev/melee` lags `origin/main` —
  `git pull` before running or starting new work.

## Open threads / next ideas

- **Reuse:** lift `chargen`'s Tarmar stat rules (attribute set, 3–18 range,
  65-point budget, validation) into the shared `tarmar-rules` package so
  tarmar-studio's character creator shares the same validator. Needs a
  tarmar-rules version bump + re-publish + melee dep bump.
- **Force retreat** works in the engine/API but has no UI button yet.
- **Tarmar combat — deferred refinements:** the severe-crit confirm roll
  (triple + bleeding), fumble drop/break on a natural 1, and mana/magic are not
  yet in melee's Tarmar profile (they exist in the spec / tarmar-studio).
- **Deferred Melee mechanics** (GitHub issues #1–#13): thrown-weapon
  line-of-flight, fuller hand-to-hand, pole-weapon jab/charge bonuses, disengage
  rolls, megahex-accurate missile range, monsters/nonhumans, experience &
  advancement.
- **Polish:** a full light theme (current is a lightened dark theme); editor
  layout; optional token flash / extra narration flavour.

See also `docs/tarmar-ruleset-integration.md` for the original dual-ruleset
design and the Melee↔Tarmar mapping.
