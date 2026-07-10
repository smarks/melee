# Wizard / Magic + Mana Milestone — Design Plan

**Branch:** `wizard-mana` (forked from `main`; NEVER merged to main until Spencer approves 3×)
**Review cadence:** 3 approval gates before merge to `main`
**Status:** Gate 1 review artifact

---

## 0. Source material (read + cited)

**Classic magic — `docs/reference/the-fantasy-trip-wizard-rules.pdf` (TFT: Wizard, 3rd ed., 24pp), read via pdftotext.** Key rules (page markers as printed):
- **Creating a wizard (p.3–4):** human = 8 ST / 8 DX / 8 IQ + 8 free = **32 total**, none below 8. **ST is both the injury pool AND the spell-power pool** — each spell has a ST cost drained from ST; a wizard cannot cast a spell that would drop ST below 0 (may cast one reducing it to exactly 0). **IQ sets how many spells he knows (= IQ) and which** (spells are IQ-tiered; an IQ-N wizard picks from IQ-8…IQ-N, up to N). **DX is the casting to-hit.**
- **Turn sequence (p.5):** adds a **"Renew Spells"** stage before movement — continuing spells must be re-energized (ST paid) each turn or they end.
- **Casting (p.11):** one new spell/turn; 3-dice to-hit ≤ adjDX. Specials: 3 triple / 4 double / 5 auto-hit; 16 auto-miss; **17 fizzle, lose full ST; 18 fizzle + knocked down, lose full ST.**
- **Missile spells (p.12):** Magic Fist, Fireball, Lightning. Target + ST spent (**max 3**). DX range penalty by megahex. Damage = 1 die/ST, −2/die (Magic Fist), −1/die (Fireball), −0 (Lightning); never below ST invested. Straight line, blocked by walls.
- **Thrown spells (p.13):** act on a figure/object (Blur, Freeze, Drop Weapon…). −1 DX/hex. Hit → effect + full ST; **miss → lose 1 ST.** One of each per figure; not cumulative.
- **Control spells (p.13–14):** thrown subclass; victim gets **3-dice save vs IQ**. Save → caster loses 1 ST; fail → full ST + control.
- **Creation spells (p.15):** summon wolf/fire/wall/shadow/images. Miss → lose 1 ST. Can't act the turn they appear.
- **Protection (p.19–20):** **Stone Flesh stops 4 hits/attack, Iron Flesh 6** — subtracts from each incoming attack like armor. **Staff (p.19):** grants a staff (1 die, 0 ST to strike).
- **Combining with Melee (p.23):** fighters are **IQ 8**; a wizard may wear armor but **cannot cast with a shield or non-staff weapon ready** (−4 DX with a non-staff weapon).
- **CRITICAL GAP:** the numeric **Spell Table** (exact per-spell ST cost + IQ tier) lives on the Reference Pages mini-booklet, **not in the supplied PDF's text layer**; the ITL PDF is un-OCR'd scans. → Open question #1.

**Tarmar magic — `~/Documents/dev/tarmar-studio/reference/content/proposals/d20-combat-resolution-spec.md`:** the spec has **no magic system** (only §10: general checks stay `3d6 ≤ attribute`). melee already carries a stubbed `mana_roll` on `TarmarFigure` (`engine/tarmar.py:84`). Tarmar magic would have to be **designed from scratch**, not ported.

---

## 1. Scope — RECOMMENDATION: **Classic (TFT Wizard) only this milestone; defer Tarmar mana.**
1. Classic magic is a complete authoritative ruleset; Tarmar magic **doesn't exist yet** (would need inventing + its own review) — bundling it blows the 3-gate cadence.
2. ST-as-mana is already native (ST pool, apply_damage, absorbed). Magic Fist ≈ a missile attack with a ST cost; Stone Flesh ≈ armor via `absorbed()`.
3. Build the **seam for both**: new hooks go on the `Ruleset` base (`resolve_spell`, `apply_spell_cost`, `spell_protection`) exactly like `resolve_attack`, so a future `TarmarRuleset.resolve_spell` drops in without re-plumbing. The `mana_roll` stub stays; `TARMAR_MANA` becomes its own milestone.

**Definition of done:** in the browser, create a Classic wizard (ST/DX/IQ spread + chosen spells), place it, and over real turns cast the initial spell set (direct-damage missile, defensive, utility/control) with correct ST depletion, casting-roll resolution (incl. 17/18 fizzles), continuing-spell upkeep, spell narration, AI wizards that cast, and full invariant/soak/effect coverage. Tarmar figures unchanged.

---

## 2. Core mechanics (Classic)
- **Spell power = ST** (no separate pool). Cast subtracts via `Ruleset.apply_spell_cost`. Cast below 0 ST illegal (rejected in `queue_spell`); to exactly 0 legal.
- **Knowing a spell:** gated by `spells_known` (chosen at chargen), size ≤ IQ, all tiers ≤ IQ.
- **Casting resolution:** 3-dice, new `classify_spell_roll` in `engine/combat.py` (parallel to `classify_roll`): 3/4/5 → triple/double/auto-hit; 16 auto-miss (lose 1 ST); 17 fizzle + full ST; 18 fizzle + full ST + knockdown. Distinct from weapons' drop/break accounting.
- **To-hit:** `spell_to_hit_number(...)` on `Ruleset`, reusing `missile_range_penalty` (missile) / −1/hex (thrown). No facing bonus vs the target (`ignore_facing=True`), but caster's armor/wound adjDX still applies.
- **Miss/fizzle ST:** thrown/creation/special miss = 1 ST; 17/18 = full ST.
- **Backfire:** 18 routes through the existing `KNOCKDOWN` status path (caster prone).
- **Continuing spells:** ST does not regenerate mid-fight; continuing spells cost ST each turn in the new **Renew phase** (p.5) — the one genuinely new turn stage.
- **Protection:** Stone Flesh/Iron Flesh fold into `absorbed()` — composes with armor, no new resolution path.

---

## 3. Initial spell set (small; each hits a different seam)
| Spell | Type | Seam | Rules | Phase |
|---|---|---|---|---|
| **Magic Fist** | Missile | flight/line, damage, ST/die | 1d−2 per ST, max 3; MH range penalty; line (p.12) | 2 |
| **Stone Flesh** | Thrown, continuing/defensive | `absorbed()`, Renew | stops 4/attack; one/figure; renewed each turn (p.19) | 2 |
| **Drop Weapon** | Thrown, utility/control | existing dropped-weapon machinery | target drops readied weapon; −1 DX/hex; miss = 1 ST (p.13) | 3 |
| **Fireball** | Missile | data variant | 1d−1 per ST (p.12) | 3 |
| **Lightning** | Missile | data variant | 1d per ST, blasts walls (p.12) | 3 |
| **Control Person** | Thrown (Control) | new 3d-vs-IQ save path | victim saves 3d≤IQ; else controlled (p.13-14) | 3 |
| **Staff** | Special (setup) | grants staff at build | 1-die staff, 0 ST to strike (p.19) | 3 |

**Deferred (noted, not built):** Fire/Wall/Shadow barriers (terrain objects), Summon Wolf/Myrmidon (created-figure lifecycle + upkeep), illusions/images/disbelief, Blur/Freeze/Invisibility, Reverse Missiles, Shock Shield, area/megahex spells — each its own future increment.

---

## 4. Engine integration
- **New `Ruleset` hooks (base, `engine/ruleset.py`), mirroring `resolve_attack`:** `resolve_spell(...) -> SpellResult` (composition), `spell_to_hit_number(...)`, `apply_spell_cost(...)`, extend `absorbed()` to add the caster's active protection. Classic behavior in the base; `TarmarRuleset` inherits no-op/NotImplemented stubs (Tarmar figures carry no spells → never reached).
- **`SpellResult`** (`engine/combat.py`, parallel to `AttackResult`): hit, rolled, needed, multiplier, st_spent, damage, fizzled, knockdown, spell_id, target_uid, save_made, to_hit_breakdown, note.
- **Option catalog (`engine/options.py`):** `Option.CAST` (context ANY, movement "none", is_attack=False) + `casts_spell: bool` on `OptionSpec`. Casting forbidden with a shield/non-staff weapon ready (p.23) — enforced in availability.
- **Targeting — reuse the #362 single source:** a `spell_targets(caster, spell)` on `_CombatMixin` reusing the exact ranged-target computation for missile spells / front-arc-adjacent for thrown; missile casters use `aim()` to turn-to-face (free, no facing bonus). One authority for human UI + AI, like `attack_candidates`.
- **Queue + resolve:** `PendingCast` (parallel to `PendingAttack`) + `queue_spell(...)` with guards mirroring `_validate_attack` (chose CAST, can act, ST ≥ cost, spell known, one/turn, target legal). Resolve inside the existing `resolve_combat` DX-ordered loop (casts sort by `order_dx`), `_resolve_cast` → `rules.resolve_spell` → `apply_spell_cost` → `apply_damage` → `status_after_hit`. Missile spells reuse `_resolve_flight`.
- **New Renew turn stage (Phase 3):** pre-movement pass energizing continuing spells; Phase 2 skips it (Magic Fist has no upkeep).
- **Figure fields + canonical enumeration/#245 drift-guard:** on `Figure` (one class): identity (carry-over + edit_spec) `intelligence:int=8`, `spells_known:list=[]`, `has_staff:bool=False`; per-fight `active_spells:dict={}`, `spell_protection:int=0` (read by `absorbed()`); per-turn `cast_this_turn:bool=False`. Add identity/per-fight to `CARRY_OVER_STATE` (#359/#369), `cast_this_turn` to `PER_TURN_FLAGS` (#155), and extend the #245 drift-guard's expected field set.
- **Wizard vs fighter:** same `Figure` class; a wizard has non-empty `spells_known`, the 32-pt ST/DX/IQ spread, IQ>8, optional staff. Fighters keep IQ 8, empty spells.

---

## 5. Char-gen (`engine/chargen.py`)
- Wizard spread: **ST+DX+IQ = 32, each ≥ 8** (a 3-attribute wizard spread); fighters gain `intelligence` (default 8) so IQ is first-class in Classic (back-compat via default).
- `validate()` wizard branch: IQ ≥ 8; `len(spells_known) ≤ IQ`; every spell tier ≤ IQ; ids exist; a non-staff ready weapon/shield flagged (or auto-slung, p.23).
- `catalog()` gains a `"spells"` section from a new **`engine/spells.py`** (`Spell` dataclass: id, name, type, iq_tier, st_cost, max_st, damage_per_st, stops, continuing).
- `build()` wizard branch → `create_wizard(...)` in `engine/figure.py` (sets IQ, spells_known, has_staff, staff weapon when Staff known).

---

## 6. UI
- Spell catalog + wizard setup (`api_catalog`, setup wizard, editor): a "Wizard" type; IQ input; a spell-picker live-constrained to ≤ IQ and tier-gated (reuse the Tarmar skill-picker patterns).
- Spell menu (`board.js drawControls`): a **Cast** row group parallel to attack rows → pick spell → target (reuse targeting plumbing) → ST-slider (1–3) for missile spells. `_figure_dict` surfaces is_wizard/intelligence/spells_known/active_spells; `_edit_spec` round-trips IQ + spells.
- Sheet: IQ, spells known, ST doubling as the spell-power gauge, active continuing spells.
- `control_state.js`: casting is another action in `combat_render`; the `must_attack` resolve-gate must NOT force a wizard to attack (a queued cast satisfies "has a plan"); `needsTarget` handles a wizard-with-no-plan.
- `api_options`: add `castable_spells` + `spell_targets` (from the #362 source); shield/weapon-ready reasons surface greyed (#73).
- `api_action`/`_FIGURE_ACTIONS`: add `"cast_spell"` → `_act_cast_spell` (mirrors `_act_queue_attack`), included in `_FIGURE_ACTIONS` for seat authz (#244).
- Dead-control guard (#388): every new Cast control gets effect-asserting e2e tests.

---

## 7. Determinism & safety nets
- All casting rolls use the injected `Dice` (no `random`); document the dice-stream order in `resolve_spell`.
- Invariants (`engine/invariants.py`): ST never < −1 by a cast; `len(spells_known) ≤ intelligence` and tiers ≤ IQ; `spell_protection ≥ 0`; a continuing spell implies ST paid this turn; a fizzle charged the right ST. Extend `assert_log_truthful` via `narrate_spell` (`engine/narrative.py`).
- Soak (`test_soak.py`): sometimes field wizards + cast; assert invariants after every step.
- Unit tests: `classify_spell_roll`, `resolve_spell`, each spell, ST accounting, Renew phase, wizard chargen validation (`test_spells.py`). New UI controls under the #388 guard.

---

## 8. Phasing → the 3 review gates
- **Gate 1 — Design (this doc).** Approve scope (Classic-only), spell set, ST-as-mana, seam design.
- **Gate 2 — Core casting engine + first spells + minimal UI, end-to-end.** `engine/spells.py` (Magic Fist + Stone Flesh); Figure wizard fields + enumeration/drift-guard; `create_wizard`; chargen branch; `resolve_spell` + hooks; `SpellResult`; `classify_spell_roll`; `Option.CAST`; `PendingCast`; `queue_spell`; cast resolution; `absorbed()` protection; minimal cast UI + ST slider + sheet gauge + `narrate_spell`; invariants + first soak + effect tests. **Demoable: a wizard casts Magic Fist and Stone Flesh in the browser.**
- **Gate 3 — Full spell set + char-gen UI + polish + MERGE.** Fireball/Lightning/Drop Weapon/Control (3d-vs-IQ save)/Staff; the Renew phase; wizard setup/editor UI (IQ + spell picker, live gating); polished spell menu + active-spell display; AI wizards cast; full coverage; drift-guard confirmed. **Then merge to main.**

---

## 9. Open questions for Spencer (Gate 1 decisions)
1. **Missing numeric Spell Table** (per-spell ST cost + IQ tier not in our PDF's text). Supply the reference sheet, or approve encoding canonical TFT values in `engine/spells.py` pending confirmation? (Blocks exact numbers, not architecture.)
2. **Classic-only this milestone**, Tarmar mana deferred — confirm.
3. **Initial spell set** (Magic Fist / Stone Flesh / Drop Weapon + Fireball/Lightning/Control/Staff) + the deferral list — confirm.
4. **Wizard = one Figure class with wizard fields + a 32-pt spread** (not a subclass) — confirm.
5. **Can existing fighters be wizards?** Recommendation: no (wizards built via the ST/DX/IQ spread; fighters stay IQ-8). Confirm — or is a hybrid "fighter who knows a spell or two" in scope?
6. **Wizard in the roster/setup:** a new "Wizard" archetype alongside Knight/Swordsman, AI-fieldable — confirm.
7. **Continuing-spell upkeep (Renew stage, p.5)** wanted in Gate 3 vs. fire-and-forget protection — confirm.

---

## 10. File-by-file change map (see the full plan for effort per file)
`engine/spells.py` (new), `engine/combat.py`, `engine/ruleset.py`, `engine/tarmar.py` (stubs), `engine/figure.py`, `engine/options.py`, `engine/state.py`, `engine/chargen.py`, `engine/narrative.py`, `engine/invariants.py`, `engine/ai.py`; `board/serialize.py`, `board/views.py`, `board/static/board/board.js`, `board/static/board/control_state.js`; `engine/tests/test_spells.py` (new) + soak/chargen/invariants; `board/tests/` e2e effect tests; `docs/`.
