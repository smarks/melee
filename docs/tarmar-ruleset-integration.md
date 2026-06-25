# Adding a swappable "Tarmar rules" profile to Melee

**Audience:** the Claude that built this `melee` engine.
**Author:** the Claude that built the Tarmar d20 combat system in `tarmar-studio`.
**Goal:** let a match be fought under **either** classic *Fantasy Trip: Melee*
rules **or** the **Tarmar** rules, switchable as a unit.

> Authoritative source for every Tarmar number below:
> `tarmar-studio/reference/content/proposals/d20-combat-resolution-spec.md`, and
> the tested reference implementation `tarmar-studio/characters/combat.py`
> (pure-Python, Django-free — you can read or port it directly).

---

## Status & locked decisions (2026-06-25)

- **End goal:** one melee game, **UI switch** between **Tarmar rules** and
  **original Melee rules**.
- **Build owner:** the tarmar-studio Claude will implement this directly, **after
  the current melee rules-extraction refactor lands** — re-read the then-current
  code first, since file/hook names below may shift.
- **Weapon skill (the §3 open question — RESOLVED):** a fighter may **begin a
  match with pre-existing skills**, set at character creation. There is **no
  in-match skill gain for now.** (Deferred idea: winning enough matches could
  later award skill points.) So `create_tarmar_fighter` takes starting skills as
  input; nothing in the combat loop increases them yet.

---

## 0. The headline finding

**Melee character generation is *not* close enough to Tarmar's to reuse one
figure under both rule sets.** They are different stat models, not two dialects:

| | Melee (`rules_data.py`, `figure.py`) | Tarmar |
|---|---|---|
| Attributes | **two**: ST, DX | **six**: STR, DEX, INT, WIS, CON, CHR |
| Generation | 24 pts split ST/DX, each ≥ 8 | 3d6 each (or 65-pt buy), range to ~18 |
| Hit pool | **ST itself** (`current_st`); 0 = collapse, −1 = dead | **two derived pools**: Fatigue, then Body |
| Hit-pool source | the ST attribute | `Fatigue = CON+WIS+INT+max(DEX,STR)+2d6`; `Body = ⌈⅔·Fatigue⌉` |
| To-hit input | `base_adj_dx` (roll-**under**) | matrix Target Number + `floor((DEX−10)/2)` + **weapon-skill level** |
| Other systems | none | Mana (3d6), skills (0–5), prior-experience/aging |

A Tarmar attack roll **requires inputs a Melee `Figure` does not carry** — four
of the six attributes, two derived pools, and a per-weapon skill level. You
cannot compute a Tarmar `Fatigue` from a Melee figure (the formula needs
CON/WIS/INT, which Melee never rolls). So the figure model has to be swappable
**together with** the resolution rules.

**Therefore: two coupled seams, selected as one "rules profile."** The
`Ruleset` seam already exists; the **figure/stat-model seam is new**.

---

## 1. What's shared vs. what swaps

Everything **structural** stays — both games are Fantasy-Trip hex combat, and
Tarmar inherited this layer:

- **Shared (do not touch):** the arena, facing/engagement geometry
  (`facing.py`), the turn/phase sequence, the option catalog (`options.py`),
  movement allowance & reachability (`movement.py`). Initiative is 1d6/side and
  actions still order by adjusted DX in both systems.
- **Swaps as a unit (the "rules profile"):**
  1. **figure stat model + character generation** — *new seam*
  2. **combat resolution** — the existing `Ruleset` seam (add `TarmarRuleset`)
  3. **injury / death model** — already hooks on `Ruleset`
     (`apply_damage`, `status_after_hit`)

---

## 2. Proposed architecture

Introduce a small **`RulesProfile`** that bundles a figure-builder with its
matching `Ruleset`, so the two can never be mismatched:

```python
# engine/profile.py  (new)
@dataclass(frozen=True)
class RulesProfile:
    name: str
    ruleset: Ruleset
    build_fighter: Callable[..., Figure]   # the profile's char-gen entry point

CLASSIC = RulesProfile("Classic Melee", Ruleset(), create_human)
TARMAR  = RulesProfile("Tarmar",        TarmarRuleset(), create_tarmar_fighter)
```

Selection points (both already exist, you just thread the profile through):

- **`board/scenario.py`** builds figures today via `create_human(...)`. Have it
  call `profile.build_fighter(...)` instead, so a Tarmar match spawns
  Tarmar-shaped figures.
- **`GameState(arena, figures, ruleset=...)`** already takes the ruleset; pass
  `profile.ruleset`.

`GameState` reads stats off figures **only through accessors**
(`base_adj_dx`, `current_st`, `movement_allowance`, `hits_stopped()`,
`wound_dx_penalty()`), and `Ruleset` is the only place combat numbers are
computed. That's why this works: give the Tarmar figure the same *structural*
accessors and let `TarmarRuleset` read the *combat* ones it adds.

### The Tarmar figure

Subclass `Figure` (or add optional fields) — keep the structural state
(position, facing, posture, `movement_allowance` from armor, an order-DX), and
add the Tarmar stat block:

```python
class TarmarFigure(Figure):
    # six attributes (strength/dexterity already exist on Figure)
    intelligence: int; wisdom: int; constitution: int; charisma: int
    fatigue_roll: int            # the one-time 2d6 in the Fatigue formula
    mana_roll: int               # 3d6, secret
    weapon_skill: dict[str, int] # weapon name -> skill level 0..5
    fatigue_taken: int = 0
    body_taken: int = 0

    @property
    def fatigue(self):  # max pool
        return self.constitution + self.wisdom + self.intelligence \
             + max(self.dexterity, self.strength) + self.fatigue_roll
    @property
    def body(self):
        return math.ceil(self.fatigue * 2 / 3)
    @property
    def effective_dexterity(self):   # feeds dex_modifier and order-DX
        return self.dexterity        # (+ aging penalty if you port that)
```

`current_st`/`collapsed`/`is_dead` become Fatigue/Body-based (see §5 injury).
Keep `movement_allowance` (armor-driven — identical concept) and provide an
order-DX so the turn sequence can still sort attacks.

---

## 3. Tarmar character generation (the new char-gen seam)

`create_tarmar_fighter(...)` must produce the stat block above:

- **Attributes:** STR, DEX, INT, WIS, CON, CHR — each **3d6**, or a **65-point
  buy** across the six. (Contrast Melee's 24 across two.)
- **Fatigue pool:** `CON + WIS + INT + max(DEX,STR) + 2d6` (roll the 2d6 once at
  creation, store as `fatigue_roll`).
- **Body pool:** `⌈Fatigue × 2/3⌉`.
- **Mana:** `3d6` (secret; only relevant if you ever add Tarmar magic — Melee
  proper has none, so you can stub it).
- **Weapon skill (0–5):** *Melee has no concept of this, and it is the single
  biggest swing in Tarmar to-hit (+2 per level, up to +10).* The Tarmar profile
  **must introduce a skill input at char-gen** — e.g. a per-fighter "training"
  value, or a points pool spent on weapon skills. Don't default everyone to 0:
  per the balance grids, untrained fighters hit armored foes only 5–35% of the
  time, which makes for a slog.

**Converting an existing Melee fighter is lossy** — ST→STR and DX→DEX map
cleanly, but INT/WIS/CON/CHR and skill have no source. If you offer a "convert"
button, fill the missing four with a chosen baseline (e.g. 10) and a default
skill, and label it clearly as an approximation, not true Tarmar.

---

## 4. Tarmar combat resolution as `TarmarRuleset(Ruleset)`

The `Ruleset` hooks map almost 1:1. **Key inversion: Melee rolls 3d6
*under* adjDX; Tarmar rolls **1d20 *over*** a Target Number.**

| `Ruleset` hook | Classic Melee | `TarmarRuleset` override |
|---|---|---|
| `to_hit_number` | adjDX to roll under | **Target Number** = matrix[weapon_class][armour_tier] + shield_bonus + defender dodge |
| `attack_dice_count` | 4 dice vs defender, else 3 | always **1d20**; defend/dodge instead gives **+4 to the defender's TN** |
| `classify_roll` | 3/4/5 crit, 16/17/18 fumble, else `roll ≤ needed` | **nat 20** = hit + crit (then *confirm*: 2nd d20 vs TN → severe: ×3 + bleeding, reaches Body); **nat 1** = miss + fumble; else `roll + bonus ≥ TN` |
| `weapon_damage` | weapon dice × multiplier | same, + the weapon's `damage_mod` |
| `absorbed` | armor (+frontal shield) stops | **Hybrid** (see §6) — needs the *attacker's weapon class* |
| `apply_damage` / `status_after_hit` | subtract from ST | deplete **Fatigue, then Body** (see §5) |
| `order_dx` | adjDX | keep — order by Tarmar effective DEX |

**The to-hit bonus** added to the d20 (assemble in the ruleset from figure
fields):

```
to_hit_bonus = floor((effective_DEX − 10) / 2)      # DEX aim
             + 2 * weapon_skill_level                # 0..5  -> +0..+10
             + min(0, effective_STR − weapon.min_strength)   # §3.1 STR-fit, ≤ 0
             + situational                            # re-signed facing/range (§7)
HIT if  d20 + to_hit_bonus ≥ Target Number
```

### Two hook-shape problems to fix (important)

1. **No d20 source.** `resolve_attack` rolls `dice.total(dice_count)` — that's
   *N×d6* from `hexarena.dice`, with no hook for "which die." For Tarmar you
   need a real **d20**. Either add `d20()` to `hexarena.dice`, or have
   `TarmarRuleset.resolve_attack` roll its own d20. (`resolve_attack` is itself
   a method, so overriding the whole sequence is fine and probably cleanest,
   because of #2.)
2. **`absorbed(target, *, zone)` doesn't get the weapon** — but the Hybrid
   carve-out depends on the *attacker's weapon class*. Either widen the hook to
   `absorbed(target, *, zone, weapon)` (small, backward-compatible — classic
   just ignores it), or compute armor inside an overridden `resolve_attack`.

---

## 5. Injury / death model

Tarmar splits the hit pool. Override the injury hooks:

- **Normal hit →** subtract from **Fatigue**. At Fatigue ≤ 0: **unconscious**.
- **Severe crit (confirmed nat 20) →** also reaches **Body**. At Body ≤ 0:
  **semi-conscious / dying**.
- **Death:** when a pool goes negative by its starting value (Tarmar uses a
  survival save, `3d6 ≤ CON`, each turn — you can simplify for a duel).

`status_after_hit` returns the engine's existing `DEAD / UNCONSCIOUS /
KNOCKDOWN / None`, just keyed off Fatigue/Body instead of `current_st`.

---

## 6. Hybrid armour (the §8 decision, already locked in Tarmar)

Armour does **two jobs**: it raises the attacker's TN (via the matrix) **and**
its `stops` still subtract from damage on a hit — **except** a **Heavy Striking
/ Heavy Thrusting** weapon against a **Heavy**-tier target ignores **half** the
stops (impact carries through plate):

```python
def damage_after_armour(raw, stops, weapon_class, armour_tier):
    applied = stops
    if weapon_class in {"Heavy Striking", "Heavy Thrusting"} and armour_tier == "Heavy":
        applied = stops // 2
    return max(0, raw - applied)
```

(Lifted verbatim from `characters/combat.py`.)

---

## 7. The matrix, modifiers, and the data mapping (the gift)

Your `rules_data.py` weapons/armor **are the same Fantasy-Trip tables** Tarmar
uses — so the only data you must add is a **`weapon_class`** per weapon and an
**`armour_tier`** per armor. Here is the mapping for *your exact tables*:

**Weapon → class** (add as a field on `Weapon`):

| Class | Your weapons |
|---|---|
| Piercing | Dagger, Main-Gauche*, Rapier |
| Striking | Club, Hammer, Saber, Shortsword, Mace, Small ax, Broadsword, Morningstar |
| Heavy Striking | Two-handed sword, Battleaxe |
| Thrusting | Spear, Javelin (in melee) |
| Heavy Thrusting | Halberd, Pike axe |
| Missile — Bows | Thrown rock, Sling, Small bow, Horse bow, Longbow, Javelin (thrown) |
| Missile — Crossbows | Light crossbow, Heavy crossbow |

\*Main-Gauche is a weapon in Melee but a parry-shield in Tarmar; as a weapon,
treat it Piercing. *Flexible / Snare* (whip/net/lasso/bola) has no Melee weapon
— that matrix row is simply unused here.

**Armor → tier** (add as a field on `Armor`):
`None→None`, `Cloth→Light`, `Leather→Light`, `Chainmail→Medium`,
`Half-plate→Heavy`, `Plate→Heavy`. (Melee has no Fine Plate — fine, it's a
superset on the Tarmar side.)

**Shield → TN bonus:** `Small shield→+1`, `Large shield→+2`. (Tarmar's
tower/spike shields aren't in your table.)

**Base Target-Number matrix** (`combat.MATRIX`, columns None/Light/Medium/Heavy):

| Class | None | Light | Medium | Heavy |
|---|---|---|---|---|
| Piercing | 11 | 14 | 18 | 22 |
| Striking | 13 | 14 | 16 | 18 |
| Thrusting | 12 | 14 | 16 | 19 |
| Heavy Striking | 14 | 14 | 15 | 16 |
| Heavy Thrusting | 14 | 14 | 15 | 15 |
| Missile — Bows | 12 | 14 | 17 | 20 |
| Missile — Crossbows | 13 | 14 | 15 | 16 |
| Flexible / Snare | 13 | 16 | 19 | 22 |

**Constants:** DEX mod `floor((DEX−10)/2)`; dodge `max(0, floor((DEX−10)/2))`;
skill `+2` per level (0–5); STR-fit `min(0, STR − min_strength)`; shields
`+1/+2/+3`; crit nat 20 (confirm for severe), fumble nat 1.

A TN of 21–22 can't be met on a bare d20 — a dagger vs plate (22) needs skill, a
re-signed positional bonus, or a natural 20. That's intended.

---

## 8. Re-signed situational modifiers (§7 of the Tarmar spec)

Melee's facing/range adjustments **lowered** the roll-under target; for roll-over
they **add to the attacker's d20** (or to the defender's TN). Port them:

- Side hex **+2**, rear hex **+4**, pole vs charge **+2** → add to attacker.
- Invisible **−6**, shadow **−4**, two-weapon **−4** → subtract from attacker.
- Missile/thrown range penalties keep their magnitudes, applied to the attacker.
- Armor is **no longer** a flat to-hit modifier (it's the matrix); shields move
  to the **defender's TN**.

---

## 9. Open decisions / gotchas (call these out before building)

1. **d20 source** — add to `hexarena.dice` or roll inside `TarmarRuleset`.
2. **`absorbed` needs the weapon** — widen the hook or override `resolve_attack`.
3. **Weapon-skill input at char-gen** — *the real new design work*; Melee has no
   skills and Tarmar to-hit leans on them heavily. Decide how a duel fighter
   gets skill (training tier? points pool?).
4. **Char-gen UX differs** — 6-attribute / 65-point buy vs Melee's 24-point
   ST/DX split; the two profiles need different creation flows.
5. **AI opponent** — wherever the computer picks weapon/armor/option, its
   *valuations change per profile*: under Tarmar the matrix makes "right weapon
   for the armor" (mace vs plate, dagger vs unarmored) the core tactic, and
   armor that merely soaked in Melee now also raises TN. The opponent heuristic
   should be profile-aware or it'll play Tarmar badly.
6. **Balance baseline** — the Tarmar matrix is calibrated for DEX ≈ 8–18 and
   skill 0–5. If your point-buy lets DEX run higher, re-check the grids in the
   Tarmar spec (§6.1) so hit rates stay sane.

---

## 10. Suggested build order

1. Add `weapon_class` to `Weapon`, `armour_tier` to `Armor` (data only, §7).
2. Add a **d20** to `hexarena.dice`.
3. `TarmarFigure` + `create_tarmar_fighter` (§2–§3), incl. the skill decision.
4. `TarmarRuleset(Ruleset)` overriding the hooks in §4–§6 (port the math from
   `tarmar-studio/characters/combat.py` — it's pure functions).
5. `RulesProfile` + thread it through `scenario.py` and `GameState` (§2).
6. Tests mirroring `test_ruleset.py`; lock a few matrix cells and the §3.1 /
   Hybrid behaviors, exactly as `tarmar-studio/characters/tests/test_combat.py`
   does.

The structure you already built (the policy/structure split, the `Ruleset`
seam) is exactly right — this is additive, not a rewrite. The one genuinely new
piece of design is **how a Tarmar duel-fighter acquires weapon skill**, since
Melee never had it.

## 11. Handoff — as-built reality (from the Melee-side build, 2026-06-25)

This section is the code-grounded complement to the design above. Written right
after the `Ruleset` seam landed, so the file/line references are current.

**Status: the seam work is DONE and pushed** (`melee` main, CI green, 42 tests).
You are clear to start. Re-read `engine/ruleset.py`, `engine/state.py`,
`engine/figure.py`, and `board/serialize.py` first — names may drift from this
doc; the code wins.

**The seam covers mechanics, not the stat model.** `Ruleset` hooks still read
Melee-shaped fields off `Figure` (`base_adj_dx`, `current_st`, `hits_stopped`,
`wound_dx_penalty`, `movement_allowance`). The character/stat-model seam (§2–§3)
is the genuinely new part. Exact coupling points outside `ruleset.py` to route
through your model (grep'd, not guessed):

- **Down/out status** — `figure.collapsed` / `figure.is_dead`, read structurally
  in `state.py` (`living`/`occupied`/`enemies_of`), `facing.py:79` (engagement),
  `board/views.py:71` (victory), `board.html:355`. Keep these as `Figure`
  properties but let the stat model populate them (or add `rules.is_down`/
  `is_out` hooks). Don't scatter Body/Fatigue checks across `state.py`.
- **`movement_allowance`** — `state.py` passes it into `rules.movement_budget`;
  only the *value* lives on `Figure`. Route it through the model.
- **Serialize + UI** — `board/serialize.py:29-31` emits `st`/`max_st`/`dx` and
  `board.html` renders a Melee record sheet; `scenario.py`'s `create_human`
  char-gen is Melee-only. The Tarmar profile needs its own fields, sheet, and
  roster path. This is the largest surface.

**Override `resolve_attack` wholesale**, don't reuse the sub-hooks
(`classify_roll`/`to_hit_number` assume Melee roll-under-3d6; Tarmar d20 is a
different shape). Override `resolve_attack` + `attack_dice_count` +
`apply_damage` + `status_after_hit`, reading stats via your model. See §4's
"hook-shape problems".

**Dice gotcha:** `hexarena.Dice` is **d6-only** — `roll()`→1-6, `total(n)` sums
n d6, no d20. Add `die(sides)`/`dn` to `hexarena.dice` (bump the shared lib the
same `git+https` way orge/melee consume it) and keep it injectable + scriptable.

**Single source of truth:** don't copy `tarmar-studio/characters/combat.py` into
melee — extract it into an importable package (mirror how `hexarena` was pulled
out of orge) and depend on it. Cross-repo copies rot, and it violates Spencer's
DRY rule.

**Regression guard:** keep `engine/tests/test_combat_example.py` (the rulebook
Flavius-vs-Wulf fight) green — it proves the classic profile still works after
you add Tarmar. Build the Tarmar equivalent: a scripted-dice reproduction of a
known d20 fight as your own gold-standard test.
