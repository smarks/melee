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

The spec's §7 crit/fumble layer is in (see #233): a natural 20 doubles the
damage dice and rolls a *confirm* d20 against the same Target Number — hitting
upgrades to the severe crit (triple damage, the blow reaches Body, bleeding in
the narration). A natural 1 rolls the d6 fumble table: 1-3 off-balance (-2 to
the next attack), 4-5 weapon dropped, 6 the weapon takes stress and breaks on a
second fumble. Deferred, noted for later: mana/magic.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import tarmar_rules

from .combat import AttackResult, roll_damage
from .facing import FRONT, REAR, facing_bonus, format_situational_parts
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

# The fumble outcome beyond tarmar_rules' stateless d6 table: a natural 1 with a
# weapon that already carries stress breaks it outright ("breaks on a second
# fumble", spec §7). Rides AttackResult.fumble_effect like the table's outcomes.
FUMBLE_BREAK = "break"

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
    # §7 fumble state. `off_balance` is a standing -2 on the next attack (set by
    # a 1-3 fumble, spent by the next attack). `stressed_weapons` holds the names
    # of carried weapons that took stress on a 6 — a second fumble breaks them.
    # Keyed by name on the FIGURE, not the weapon: the catalog Weapon objects are
    # shared singletons, so per-wielder state cannot live on them.
    off_balance: bool = False
    stressed_weapons: set[str] = field(default_factory=set)

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
        force_hit=False, ranged=False, blunted=False,
    ) -> AttackResult:
        weapon = weapon or attacker.ready_weapon
        if hth_damage is not None:
            return self._resolve_hth(
                dice, attacker, target, zone, weapon, hth_damage, blunted=blunted)
        weapon_class = WEAPON_CLASS.get(weapon.name) if weapon else None
        # A javelin carries two weapon classes: Thrusting in melee, Missile —
        # Bows when thrown, "and the GM picks by how it's used" (d20-combat-
        # resolution-spec.md §5). WEAPON_CLASS holds only its melee class, so a
        # thrown use (ranged) reclasses onto the Bows tier here (#262).
        if ranged and weapon is not None and weapon.name == "Javelin":
            weapon_class = "Missile — Bows"
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
        # A ready off-hand main-gauche parries as a +1 shield-style bonus on the
        # Target Number in the Tarmar path (spec §6: "main_gauche +1 (parry)"),
        # not the classic damage-absorbing parry. main_gauche_parry returns 1
        # only for a qualifying frontal, one-handed, non-missile blow (p.13).
        main_gauche = main_gauche_parry(target, weapon, zone)
        dodge = tarmar_rules.dodge_modifier(target.base_adj_dx)
        # Dodge raises the TN only against a missile/thrown attack; defend only
        # against a melee blow (Melee p.20) — type-aware like the classic profile.
        defends = (ranged and target.dodging) or (not ranged and target.defending)
        defend = DEFEND_TN_BONUS if defends else 0
        target_number = tarmar_rules.target_number(
            weapon_class, tier, shield_bonus=shield + main_gauche,
            defender_dodge=dodge) + defend

        skill = attacker.weapon_skill.get(weapon.name, 0)
        # A standing off-balance penalty (from a 1-3 fumble) drags this attack;
        # apply_attack_side_effects clears the flag once the attack is applied.
        off_balance = (tarmar_rules.OFF_BALANCE_PENALTY
                       if getattr(attacker, "off_balance", False) else 0)
        situational = ((0 if ignore_facing else facing_bonus(zone))
                       + range_penalty + situational + off_balance)
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
            outcome = {**outcome, "hit": True, "critical": False, "fumble": False,
                       "outcome": "hit"}

        # §7 natural 20: double dice, then a confirm d20 against the same TN —
        # hitting again upgrades to the severe crit (triple dice, reaches Body).
        multiplier = 1
        confirm_roll = 0
        severe_crit = False
        if outcome["critical"]:
            confirm_roll = dice.dn(20)
            severe_crit = tarmar_rules.confirm_severe_crit(
                confirm_roll, target_number, bonus)
            multiplier = (tarmar_rules.SEVERE_CRIT_DAMAGE_MULTIPLIER if severe_crit
                          else tarmar_rules.CRIT_DAMAGE_MULTIPLIER)

        # §7 natural 1: the d6 fumble table — unless the weapon already carries
        # stress, in which case this second fumble breaks it outright.
        dropped_weapon = broke_weapon = False
        fumble_effect = ""
        if outcome["fumble"]:
            if weapon.name in getattr(attacker, "stressed_weapons", ()):
                broke_weapon = True
                fumble_effect = FUMBLE_BREAK
            else:
                fumble_effect = tarmar_rules.fumble_result(dice.dn(6))
                dropped_weapon = fumble_effect == tarmar_rules.FUMBLE_DROP

        raw_damage = damage = 0
        if outcome["hit"]:
            raw_damage = roll_damage(dice, weapon.damage, multiplier, extra_dice)
            raw_damage = self._blunt(raw_damage, blunted)  # practice bout (p.22)
            stops = target.hits_stopped(
                from_front=(zone == FRONT), from_rear=(zone == REAR))
            damage = tarmar_rules.damage_after_armour(
                raw_damage, stops, weapon_class, tier)

        return AttackResult(
            hit=outcome["hit"], rolled=die, needed=target_number, dice_count=1,
            multiplier=multiplier, raw_damage=raw_damage, damage=damage,
            dropped_weapon=dropped_weapon, broke_weapon=broke_weapon,
            weapon=weapon, zone=zone,
            body_hit=severe_crit,  # only the CONFIRMED crit reaches Body (§7)
            note=outcome["outcome"],
            roll_under=False,
            auto_hit=force_hit,
            confirm_roll=confirm_roll, severe_crit=severe_crit,
            fumble_effect=fumble_effect,
            to_hit_breakdown=self._breakdown(
                attacker, weapon, weapon_class, tier, shield, defends,
                target_number, skill, zone, ignore_facing, range_penalty, bonus,
                situational_note, off_balance=off_balance,
                main_gauche=main_gauche))

    def _resolve_hth(self, dice, attacker, target, zone, weapon, hth_damage,
                     *, blunted=False) -> AttackResult:
        """A grapple strike under Tarmar — bare hands have no weapon class, so this
        rolls d20 vs a flat grapple number with the DX and +4 rear adjustments,
        then takes off the target's flat armour stops. (An approximation.) The
        §7 severe-crit confirm applies here too — Body must be exactly as hard
        to reach bare-handed as armed — but not the fumble table: a grappler
        has no weapon in hand to drop or stress."""
        target_number = 11
        # A standing off-balance penalty (from a prior 1-3 fumble) drags this
        # grapple strike just as it drags an armed blow in resolve_attack;
        # apply_attack_side_effects clears the flag once this attack is applied,
        # so read it here or it is spent unused (#311).
        off_balance = (tarmar_rules.OFF_BALANCE_PENALTY
                       if getattr(attacker, "off_balance", False) else 0)
        bonus = (tarmar_rules.dex_modifier(attacker.base_adj_dx)
                 + facing_bonus(zone) + off_balance)
        die = dice.dn(20)
        outcome = tarmar_rules.resolve_attack(die, target_number, bonus)
        multiplier = 1
        confirm_roll = 0
        severe_crit = False
        if outcome["critical"]:
            confirm_roll = dice.dn(20)
            severe_crit = tarmar_rules.confirm_severe_crit(
                confirm_roll, target_number, bonus)
            multiplier = (tarmar_rules.SEVERE_CRIT_DAMAGE_MULTIPLIER if severe_crit
                          else tarmar_rules.CRIT_DAMAGE_MULTIPLIER)
        raw_damage = damage = 0
        if outcome["hit"]:
            raw_damage = roll_damage(dice, hth_damage, multiplier)
            raw_damage = self._blunt(raw_damage, blunted)  # practice bout (p.22)
            damage = max(0, raw_damage - target.hits_stopped(
                from_front=(zone == FRONT), from_rear=(zone == REAR)))
        return AttackResult(
            hit=outcome["hit"], rolled=die, needed=target_number, dice_count=1,
            multiplier=multiplier, raw_damage=raw_damage, damage=damage,
            dropped_weapon=False, broke_weapon=False, weapon=weapon, zone=zone,
            body_hit=severe_crit, roll_under=False,
            confirm_roll=confirm_roll, severe_crit=severe_crit,
            note=outcome["outcome"], to_hit_breakdown=f"grapple: d20 {bonus:+d} vs {target_number}")

    @staticmethod
    def _breakdown(attacker, weapon, weapon_class, tier, shield, defending,
                   target_number, skill, zone, ignore_facing, range_penalty, bonus,
                   situational_note="", off_balance=0, main_gauche=0) -> str:
        """How the d20 to-hit was reached: the target number it needed, and the
        bonus added to the die (with its parts)."""
        target = f"need {target_number} ({weapon_class} vs {tier}"
        if shield:
            target += f", shield +{shield}"
        if main_gauche:
            target += f", main-gauche +{main_gauche}"
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
        if off_balance:
            parts.append(f"{off_balance:+d} off-balance")
        parts.extend(format_situational_parts(
            zone, ignore_facing=ignore_facing,
            range_penalty=range_penalty, situational_note=situational_note))
        roll = f"roll d20 {bonus:+d}" + (f" ({', '.join(parts)})" if parts else "")
        return f"{target}; {roll}"

    def apply_attack_side_effects(self, attacker, result) -> None:
        """The fumble table's lingering effects on the ATTACKER (§7, #233).

        Any completed attack spends a standing off-balance penalty (it applied
        to this very roll), and a fresh 1-3 fumble sets a new one — both in a
        single assignment. A 6 marks the wielded weapon stressed; the break (a
        second fumble with it) clears the mark, since the weapon itself is gone
        from the game. Mutation lives here, not in :meth:`resolve_attack`, so
        resolution stays pure over the figures.
        """
        result_effect = result.fumble_effect
        attacker.off_balance = result_effect == tarmar_rules.FUMBLE_OFF_BALANCE
        if result.weapon is None:
            return
        if result_effect == tarmar_rules.FUMBLE_STRESS:
            attacker.stressed_weapons.add(result.weapon.name)
        elif result_effect == FUMBLE_BREAK:
            attacker.stressed_weapons.discard(result.weapon.name)

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
