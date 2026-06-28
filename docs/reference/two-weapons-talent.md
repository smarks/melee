# Two Weapons talent (Florentine fighting) — spec

Distilled from *The Fantasy Trip: In the Labyrinth* (the full rulebook PDF is kept
local at `docs/reference/the-fantasy-trip-in-the-labyrinth.pdf`, gitignored). This
is the reference for a future Melee engine feature; it is **not** part of the base
Melee/Wizard microgame rules, which have no two-weapon-fighting mechanic at all.

## The talent

**Two Weapons** — IQ 2 talent. Also called the **Florentine** style (Nitto / Katori
Ryu in the Orient): using two one-handed weapons at once.

- **Prerequisites:** `DX ≥ 11`, *and* the weapon talent for each weapon used this way.
- **When it's required:** Anyone may fight two-handed for free **if** the off-hand is
  a **dagger, main-gauche, or spike shield** (or combos like net-and-trident, two
  cesti). The talent is required only to fight with two *real* weapons — two swords,
  sword + mace, etc.

## What the talent grants

On any turn the figure attacks, it chooses **one** of:

| Option | Effect |
|---|---|
| **(a) Attack with both** | First attack at normal DX; **second attack at −4 DX**. May target the same or different figures. |
| **(b) Attack + parry** | Normal attack with one weapon; the other parries — **stops 2 points** of damage from each non-missile attack coming from a **front hex**. |
| **(c) Parry with both** | **+1 die** to enemies' rolls to hit this figure, **stops 4 points** from any successful hit, but the figure **does not threaten** the enemy (no attack that turn). |

**Fencer (talent):** automatically includes Two Weapons, but must use **two rapiers**
or **rapier + main-gauche**. A non-fencer may use any two weapons it has the ST for.

## The three tiers (model all of these)

1. **Main weapon + dagger/main-gauche, no talent (anyone):** off-hand acts as a light
   shield — parries **1 hit** per attack from non-missile, one-handed, front-hex
   attacks, at **−1 DX** (like a small shield). If attacking, may also make a
   **separate dagger attack** vs the same enemy at **−4 DX**.
2. **Two real weapons, no talent (anyone):** allowed but clumsy — **−6 DX on each
   attack** and **no defensive benefit**.
3. **Two Weapons talent (`DX ≥ 11`):** the full menu (a)/(b)/(c) above.

## Proposed engine hooks (`~/dev/melee/engine`)

The base engine models one `ready_weapon` and one attack per turn (`figure.py`,
`state.py`). Florentine is an **Advanced/ITL ruleset-layer** feature, so it should
hang off the ruleset seam (see `docs/tarmar-ruleset-integration.md`), not the base:

- **Figure:** allow a second wielded weapon to be designated "off-hand" (today
  `ready_weapon` is singular); add a `talents`/`two_weapons` capability and a
  `dexterity ≥ 11` + weapon-talent gate.
- **Legal options (`state.legal_options`):** when a two-weapon figure attacks, expose
  the (a)/(b)/(c) choice instead of a single attack.
- **Attack resolution (`state.resolve_attack`):**
  - (a) resolve a second attack at the off-hand weapon, `adjDX − 4`.
  - (b) flag the off-hand as a 2-point front-hex parry for the turn (extend
    `hits_stopped` with a front-only, non-missile shield-equivalent).
  - (c) no attack; apply `+1 die` to incoming to-hit and a 4-point damage stop.
- **No-talent fallbacks:** dagger/main-gauche off-hand → 1-pt parry at −1 DX + optional
  −4 dagger attack; two real weapons without the talent → −6 DX per attack, no defense.

When ready to build, pull exact weapon stats (ST, damage, one-/two-handed) from the
rulebook's Weapon Table and reconcile with `engine/rules_data.py`.
