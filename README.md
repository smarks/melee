# Melee

A digital implementation of **The Fantasy Trip: Melee** (Steve Jackson Games,
3rd edition) — man-to-man tactical combat with archaic weapons on a hex arena.

Sibling project to [orge](../orge); both build on the shared
[hexarena](../hexarena) hex-grid library.

## Architecture

Engine-first, like orge: the rules live in a pure-Python `engine/` package with
no web-framework dependency, so they can be tested in isolation. A thin Django
layer (`board/`, `melee_game/`) serves an interactive SVG arena.

```
melee/
├── engine/            # pure-Python rules engine
│   ├── rules_data.py  # Weapon / Armor / Shield tables (p.14)
│   ├── figure.py      # ST/DX figures, gear, derived combat numbers
│   ├── arena.py       # the hex arena (bounds, entrances)
│   ├── facing.py      # front/side/rear hexes, engagement (Section VI)
│   ├── movement.py    # movement allowance & reachability (Section V)
│   ├── combat.py      # 3d6 roll-under-adjDX, damage, armor (Section VII)
│   ├── options.py     # the per-turn option catalog (Section IV)
│   ├── state.py       # turn engine: initiative, move, combat, retreats
│   └── tests/         # pytest; includes the rulebook Combat Example (p.23-24)
├── board/             # Django app: interactive SVG arena
├── melee_game/        # Django project config
└── manage.py
```

## What's implemented (core)

Figure creation (ST/DX, armor, shields, weapons with strength requirements),
facing and engagement, movement under the option system, melee and basic missile
attacks (3d6 roll-under adjusted DX with all the special-roll outcomes), damage
dice and armor/shield absorption, attack ordering by adjDX, force retreats, and
the Reactions-to-Injury rules. The nine-turn rulebook Combat Example is
reproduced exactly as an integration test.

**Deferred to later passes:** thrown-weapon line-of-flight, hand-to-hand combat,
pole-weapon jab/charge bonuses, the disengage rolls, megahex-accurate missile
range, monsters/nonhumans, and experience.

## Running

```bash
pip install -r requirements.txt        # installs hexarena editable too
pytest                                 # run the engine + board tests
python manage.py runserver             # play in the browser
```

The rules engine alone needs no database; `runserver` uses sqlite for game
persistence scaffolding.
