"""
The Tarmar rules profile: a d20 stat model + Ruleset, paired (Section: Tarmar).

Melee's `Ruleset` abstracts combat *mechanics* but not the *stat model* — its
hooks read ST/DX-shaped fields off `Figure`. Tarmar fighters are a different
shape (six attributes -> Fatigue/Body pools + per-weapon skill), so this module
supplies both halves and binds them:

  * :class:`TarmarFigure` — the Tarmar stat block, with `collapsed`/`is_dead`
    re-keyed onto Fatigue/Body so the structural engine (`state.py`) keeps
    working unchanged.
  * :class:`TarmarRuleset` — overrides `resolve_attack` wholesale (d20 roll-over
    against the weapon-class x armour-tier matrix), plus `apply_damage` /
    `status_after_hit` (Fatigue, then Body on a critical).

The d20 resolution math itself is NOT duplicated here — it lives in the shared
``tarmar_rules`` package (also used by tarmar-studio), the single source of
truth. This module only maps Melee's weapons/armour onto Tarmar classes/tiers
and feeds the figures' numbers in.

Deferred (kept simple for the first cut, noted for later): the severe-crit
"confirm" roll (triple + bleeding), fumble drop/break on a natural 1, mana/magic.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import tarmar_rules

from .combat import AttackResult
from .facing import FRONT, REAR, facing_bonus
from .figure import Figure
from .ruleset import DEAD, KNOCKDOWN, UNCONSCIOUS, Ruleset, main_gauche_parry
from .rules_data import KNOCKDOWN_HITS

# ---- Melee catalog -> Tarmar tags (the data mapping from the spec) ----------
WEAPON_CLASS: dict[str, str] = {
    "Dagger": "Piercing", "Main-Gauche": "Piercing", "Rapier": "Piercing",
    "Club": "Striking", "Hammer": "Striking", "Saber": "Striking",
    "Shortsword": "Striking", "Mace": "Striking", "Small ax": "Striking",
    "Broadsword": "Striking", "Morningstar": "Striking",
    "Two-handed sword": "Heavy Striking", "Battleaxe": "Heavy Striking",
    "Javelin": "Thrusting", "Spear": "Thrusting",
    "Halberd": "Heavy Thrusting", "Pike axe": "Heavy Thrusting",
    "Thrown rock": "Missile — Bows", "Sling": "Missile — Bows",
    "Small bow": "Missile — Bows", "Horse bow": "Missile — Bows",
    "Longbow": "Missile — Bows",
    "Light crossbow": "Missile — Crossbows",
    "Heavy crossbow": "Missile — Crossbows",
}
ARMOUR_TIER: dict[str, str] = {
    "None": "None", "Cloth": "Light", "Leather": "Light",
    "Chainmail": "Medium", "Half-plate": "Heavy", "Plate": "Heavy",
}
SHIELD_BONUS: dict[str, int] = {"Small shield": 1, "Large shield": 2}

DEFEND_TN_BONUS = 4  # dodge/defend raises your Target Number (no advantage/disadvantage)

# Knockdown fires when one turn's hits reach this fraction of the target's
# Fatigue pool. Derived from classic Melee's KNOCKDOWN_HITS (8) against its
# ~10-point ST pool (8/10 = 0.8); see TarmarRuleset.status_after_hit.
TARMAR_KNOCKDOWN_FATIGUE_FRACTION = KNOCKDOWN_HITS / 10


@dataclass
class TarmarFigure(Figure):
    """A Tarmar-shaped fighter. Six attributes feed two derived hit pools."""

    intelligence: int = 10
    wisdom: int = 10
    constitution: int = 10
    charisma: int = 10
    fatigue_roll: int = 7                 # the one-time 2d6 in the Fatigue formula
    mana_roll: int = 0                    # 3d6, secret; unused without magic
    weapon_skill: dict[str, int] = field(default_factory=dict)  # weapon -> 0..5
    fatigue_taken: int = 0
    body_taken: int = 0

    def __post_init__(self) -> None:
        # Tarmar lets you wield a weapon you're too weak for (with a to-hit
        # penalty, §3.1) — so do NOT raise on min_strength like the Melee base.
        if self.strength < 1 or self.dexterity < 1:
            raise ValueError("STR and DEX must be positive")
        if self.ready_weapon is not None and self.ready_weapon not in self.weapons:
            self.weapons.append(self.ready_weapon)

    @property
    def fatigue(self) -> int:
        return (self.constitution + self.wisdom + self.intelligence
                + max(self.dexterity, self.strength) + self.fatigue_roll)

    @property
    def body(self) -> int:
        return math.ceil(self.fatigue * 2 / 3)

    @property
    def current_fatigue(self) -> int:
        return self.fatigue - self.fatigue_taken

    @property
    def current_body(self) -> int:
        return self.body - self.body_taken

    @property
    def current_st(self) -> int:
        """Structural code/serialize read this; surface Fatigue as the pool."""
        return self.current_fatigue

    @property
    def collapsed(self) -> bool:
        """Unconscious / out of the fight when Fatigue is exhausted."""
        return self.current_fatigue <= 0

    @property
    def is_dead(self) -> bool:
        """Dead when Body is exhausted (only crits reach Body)."""
        return self.current_body <= 0

    @property
    def effective_dexterity(self) -> int:
        return self.dexterity

    @property
    def effective_strength(self) -> int:
        return self.strength


def create_tarmar_fighter(
    name: str,
    *,
    strength: int,
    dexterity: int,
    intelligence: int = 10,
    wisdom: int = 10,
    constitution: int = 10,
    charisma: int = 10,
    side: str,
    fatigue_roll: int = 7,
    weapon_skill: dict[str, int] | None = None,
    **gear,
) -> TarmarFigure:
    """Build a Tarmar fighter. ``weapon_skill`` is the starting skill (0-5) per
    weapon name — fighters begin with skills but do not gain them mid-match."""
    return TarmarFigure(
        name=name, strength=strength, dexterity=dexterity, side=side,
        intelligence=intelligence, wisdom=wisdom, constitution=constitution,
        charisma=charisma, fatigue_roll=fatigue_roll,
        weapon_skill=weapon_skill or {}, **gear,
    )


class TarmarRuleset(Ruleset):
    """Tarmar d20 combat: roll-over a weapon-vs-armour Target Number."""

    def order_dx(self, attacker, *, zone, ignore_facing=False) -> int:
        # Attacks still order by adjusted DX (armour/shield-adjusted).
        return attacker.base_adj_dx

    def resolve_attack(
        self, dice, attacker, target, *, zone, weapon=None,
        dice_count=1, ignore_facing=False, range_penalty=0,
        situational=0, situational_note="", extra_dice=0, hth_damage=None,
        force_hit=False, ranged=False,
    ) -> AttackResult:
        weapon = weapon or attacker.ready_weapon
        if hth_damage is not None:
            return self._resolve_hth(dice, attacker, target, zone, weapon, hth_damage)
        weapon_class = WEAPON_CLASS.get(weapon.name) if weapon else None
        if weapon_class is None:
            return AttackResult(
                hit=False, rolled=0, needed=0, dice_count=1, multiplier=1,
                raw_damage=0, damage=0, dropped_weapon=False, broke_weapon=False,
                weapon=weapon, zone=zone, note="no Tarmar weapon class",
                roll_under=False)

        tier = ARMOUR_TIER.get(target.armor.name, "None")
        # A shield only covers the front, so its to-hit bonus applies only to a
        # frontal attack -- matching the damage-absorption gate (`from_front`)
        # below. Without this, a shield would also protect the flank/rear and
        # nullify flanking against a shielded foe.
        shield = (SHIELD_BONUS.get(target.shield.name, 0)
                  if target.shield_ready and zone == FRONT else 0)
        dodge = tarmar_rules.dodge_modifier(target.base_adj_dx)
        # Dodge raises the TN only against a missile/thrown attack; defend only
        # against a melee blow (Melee p.20) — type-aware like the classic profile.
        defends = (ranged and target.dodging) or (not ranged and target.defending)
        defend = DEFEND_TN_BONUS if defends else 0
        target_number = tarmar_rules.target_number(
            weapon_class, tier, shield_bonus=shield, defender_dodge=dodge) + defend

        skill = attacker.weapon_skill.get(weapon.name, 0)
        situational = ((0 if ignore_facing else facing_bonus(zone))
                       + range_penalty + situational)
        bonus = tarmar_rules.to_hit_bonus(
            effective_dexterity=attacker.base_adj_dx,   # armour drags your aim
            skill_level=skill,
            effective_strength=attacker.strength,
            str_req=weapon.min_strength or None,
            situational=situational,
        )

        die = dice.dn(20)
        outcome = tarmar_rules.resolve_attack(die, target_number, bonus)
        if force_hit:                                    # flight already decided a hit
            outcome = {**outcome, "hit": True, "critical": False, "outcome": "hit"}
        multiplier = 2 if outcome["critical"] else 1   # crit = double dice (deferred: confirm->triple)

        raw_damage = damage = 0
        if outcome["hit"]:
            weapon_total = dice.total(weapon.damage.count) + weapon.damage.modifier
            if extra_dice:                       # pole weapon in/against a charge
                weapon_total += dice.total(extra_dice)
            raw_damage = max(0, weapon_total) * multiplier
            stops = target.hits_stopped(from_front=(zone == FRONT))
            damage = tarmar_rules.damage_after_armour(
                raw_damage, stops, weapon_class, tier)
            damage = max(0, damage - main_gauche_parry(target, weapon, zone))

        return AttackResult(
            hit=outcome["hit"], rolled=die, needed=target_number, dice_count=1,
            multiplier=multiplier, raw_damage=raw_damage, damage=damage,
            dropped_weapon=False, broke_weapon=False, weapon=weapon, zone=zone,
            body_hit=outcome["critical"],  # crit reaches Body (carried on the result, not the target)
            note=outcome["outcome"],
            roll_under=False,
            to_hit_breakdown=self._breakdown(
                attacker, weapon, weapon_class, tier, shield, defends,
                target_number, skill, zone, ignore_facing, range_penalty, bonus,
                situational_note))

    def _resolve_hth(self, dice, attacker, target, zone, weapon, hth_damage) -> AttackResult:
        """A grapple strike under Tarmar — bare hands have no weapon class, so this
        rolls d20 vs a flat grapple number with the DX and +4 rear adjustments,
        then takes off the target's flat armour stops. (An approximation.)"""
        target_number = 11
        bonus = tarmar_rules.dex_modifier(attacker.base_adj_dx) + facing_bonus(zone)
        die = dice.dn(20)
        outcome = tarmar_rules.resolve_attack(die, target_number, bonus)
        multiplier = 2 if outcome["critical"] else 1
        raw_damage = damage = 0
        if outcome["hit"]:
            raw_damage = max(0, dice.total(hth_damage.count) + hth_damage.modifier) * multiplier
            damage = max(0, raw_damage - target.hits_stopped(from_front=(zone == FRONT)))
        return AttackResult(
            hit=outcome["hit"], rolled=die, needed=target_number, dice_count=1,
            multiplier=multiplier, raw_damage=raw_damage, damage=damage,
            dropped_weapon=False, broke_weapon=False, weapon=weapon, zone=zone,
            body_hit=outcome["critical"], roll_under=False,
            note=outcome["outcome"], to_hit_breakdown=f"grapple: d20 {bonus:+d} vs {target_number}")

    @staticmethod
    def _breakdown(attacker, weapon, weapon_class, tier, shield, defending,
                   target_number, skill, zone, ignore_facing, range_penalty, bonus,
                   situational_note="") -> str:
        """How the d20 to-hit was reached: the target number it needed, and the
        bonus added to the die (with its parts)."""
        target = f"need {target_number} ({weapon_class} vs {tier}"
        if shield:
            target += f", shield +{shield}"
        if defending:
            target += ", defending"
        target += ")"
        parts = []
        dex = tarmar_rules.dex_modifier(attacker.base_adj_dx)
        if dex:
            parts.append(f"{dex:+d} DX")
        skill_b = tarmar_rules.skill_bonus(skill)
        if skill_b:
            parts.append(f"{skill_b:+d} skill")
        str_pen = tarmar_rules.strength_fit_penalty(
            attacker.strength, weapon.min_strength or None)
        if str_pen:
            parts.append(f"{str_pen:+d} str")
        if not ignore_facing and facing_bonus(zone):
            parts.append(f"+{facing_bonus(zone)} {'rear' if zone == REAR else 'flank'}")
        if range_penalty:
            parts.append(f"{range_penalty:+d} range")
        if situational_note:
            parts.append(situational_note)
        roll = f"roll d20 {bonus:+d}" + (f" ({', '.join(parts)})" if parts else "")
        return f"{target}; {roll}"

    def apply_damage(self, target, amount: int, *, body_hit: bool = False) -> None:
        target.fatigue_taken += amount
        target.hits_this_turn += amount          # keeps knockdown / force-retreat working
        if body_hit:
            target.body_taken += amount          # a crit reaches Body as well as Fatigue

    def status_after_hit(self, target):
        if target.current_body <= 0:
            return DEAD
        if target.current_fatigue <= 0:
            return UNCONSCIOUS
        # Classic Melee knocks a figure down after KNOCKDOWN_HITS (8) hits in one
        # turn -- a threshold tuned for that game's ~10-point ST pool. Tarmar's
        # Fatigue pool is ~5x larger, so the flat 8 would fire knockdown on nearly
        # any hit. Scale it to the same fraction of the target's own Fatigue pool
        # (8/10 = 0.8), so a staggering blow is still ~80% of the fighter's stamina
        # regardless of pool size.
        knockdown_threshold = math.ceil(
            target.fatigue * TARMAR_KNOCKDOWN_FATIGUE_FRACTION)
        if target.hits_this_turn >= knockdown_threshold:
            return KNOCKDOWN
        return None
