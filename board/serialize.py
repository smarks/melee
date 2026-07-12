"""
JSON serialization of game state for the SVG front end (renderer-agnostic).

Hexes are referenced by their "CCRR" label so the wire format matches the
geometry produced by :mod:`board.geometry`.
"""
from __future__ import annotations

from engine.chargen import TARMAR_EXTRA_STATS
from engine.figure import Figure
from engine.state import STAFF_WEAPON_NAME, GameState
from engine.tarmar import TarmarFigure

from .geometry import label_of


def _basic_spread(figure: Figure) -> tuple[int, int]:
    """The figure's *basic* ST/DX: its live attributes with any Section IX
    advancement (#10) stripped back out, so the editor and :func:`chargen.build`
    still see ST+DX summing to the race total.

    This is one half of a round-trip invariant, defined here once so both readers
    share a single notion of "basic spread":

    * The mid-game edit path re-applies the bought points -- ``_update_figure`` in
      :mod:`board.views` does ``rebuilt.strength += figure.added_st`` (and DX),
      the exact inverse, so a re-spec neither gains nor loses XP-bought ST/DX.
    * The save-character path (``api_game_save_character``) instead keeps this
      basic spread as the stored *base* character and deliberately does NOT re-add,
      so a saved fighter loads fresh from the setup wizard.
    """
    return figure.strength - figure.added_st, figure.dexterity - figure.added_dx


def _edit_spec(figure: Figure) -> dict:
    """The figure's base chargen spec, so the UI can edit it and round-trip it
    through :func:`engine.chargen.build`."""
    ready = figure.ready_weapon
    ready_name = ready.name if ready else "Dagger"
    second = next((w for w in figure.weapons
                   if w is not ready and w.name != "Dagger"), None)
    # A two-handed ready weapon leaves no free hand for a shield (Section III), so
    # a spec pairing them is illegal and chargen.build rejects the round-trip --
    # which froze mid-game edits on a two-handed wielder and stalled the turn
    # (#298). Drop the shield in exactly the case chargen would reject: a
    # two-handed ready weapon with no one-handed second weapon to justify carrying
    # it. Keep the shield when a one-handed backup pairs with it (the engine slings
    # it while the two-hander is out), and keep a one-handed wielder's
    # merely-slung shield too, so any legitimately-carried shield still round-trips.
    ready_is_two_handed = ready is not None and ready.two_handed
    has_one_handed_backup = second is not None and not second.two_handed
    shield_name = ("None" if ready_is_two_handed and not has_one_handed_backup
                   else figure.shield.name)
    # Report the *basic* spread (before any Section IX advancement, #10) so the
    # editor and chargen.build still see ST+DX summing to the race total. See
    # :func:`_basic_spread` for the re-add half of the invariant.
    basic_strength, basic_dexterity = _basic_spread(figure)
    spec = {
        "name": figure.name, "side": figure.side, "char_class": figure.char_class,
        "strength": basic_strength,
        "dexterity": basic_dexterity,
        "weapon": ready_name, "weapon2": second.name if second else "None",
        "armor": figure.armor.name, "shield": shield_name,
        "shield_ready": figure.shield_ready,
    }
    if figure.spells_known:
        # A wizard (Classic magic): its spec round-trips its IQ + chosen spells so
        # chargen.build re-recognises it as a wizard (engine.chargen._is_wizard keys
        # on a non-empty "spells" list). A wizard may carry weapons like anyone
        # else (#411): its spec's ``weapon`` is the READY weapon ("Staff"/"None"
        # included — the wizard convention chargen._build_wizard reads), and
        # ``weapon2`` the other carried pick. The staff itself is never a pick
        # (the Staff spell grants it on rebuild) and the dagger is the free
        # extra, so both are skipped when naming the second slot. A shield is
        # still forced empty (a wizard cannot carry one, p.23). These keys are
        # set only for a wizard, so a plain fighter's spec is byte-identical.
        wizard_ready = ready.name if ready else "None"
        second_pick = next(
            (w for w in figure.weapons
             if w is not ready and w.name not in ("Dagger", STAFF_WEAPON_NAME)),
            None)
        spec.update(
            intelligence=figure.intelligence,
            spells=list(figure.spells_known),
            has_staff=figure.has_staff,
            weapon=wizard_ready,
            weapon2=second_pick.name if second_pick else "None",
            shield="None",
        )
    if isinstance(figure, TarmarFigure):
        # The four extra Tarmar attributes come from the one source (TARMAR_EXTRA_STATS)
        # so their names live in engine.chargen, not re-typed here.
        spec.update({stat: getattr(figure, stat) for stat in TARMAR_EXTRA_STATS})
        spec.update(
            skill=figure.weapon_skill.get(ready_name, 0),
            skill2=figure.weapon_skill.get(second.name, 0) if second else 0)
    return spec


def _figure_dict(state: GameState, figure: Figure) -> dict:
    front_label = None
    if figure.position is not None:
        faced = state.arena.layout.neighbor(figure.position, figure.facing)
        front_label = label_of(faced.col, faced.row)
    footprint_labels = (
        [label_of(hex_position.col, hex_position.row)
         for hex_position in figure.footprint(state.arena.layout)]
        if figure.position else []
    )
    data = {
        "uid": figure.uid,
        "side": figure.side,
        "name": figure.name,
        "char_class": figure.char_class,
        "label": label_of(figure.position.col, figure.position.row)
        if figure.position else None,
        "facing": figure.facing,
        "front_label": front_label,
        "size": figure.size,
        "flying": figure.flying,
        "footprint": footprint_labels,
        "st": figure.current_st,
        "max_st": figure.strength,
        "dx": figure.base_adj_dx,
        "posture": figure.posture.value,
        "weapon": figure.ready_weapon.name if figure.ready_weapon else None,
        "weapons": [w.name for w in figure.weapons],
        "reloading": figure.missile_cooldown,
        "hth_opponents": figure.hth_opponents,
        "shield": figure.shield.name if figure.shield_ready else None,
        "dodging": figure.dodging,
        "defending": figure.defending,
        "dead": figure.is_dead,
        "collapsed": figure.collapsed,
        "engaged": state.engaged(figure) if figure.can_act() else False,
        "can_act": figure.can_act(),
        "acted": figure.current_option is not None,
        # The action set this selection pass (its Option value), for the tracker.
        "option": figure.current_option.value if figure.current_option else None,
        "armor": figure.armor.name,
        "model": "melee",
        # Section IX progression (#10) so the UI can show XP and advancement.
        "experience": figure.experience,
        "added_st": figure.added_st,
        "added_dx": figure.added_dx,
        "edit_spec": _edit_spec(figure),
    }
    if figure.spells_known:
        # A wizard (Classic magic; TFT: Wizard): ST is BOTH its injury pool and its
        # spell-power (mana) pool (p.3-4), so surface it framed as mana alongside the
        # spells it knows and any continuing protection in effect. These keys appear
        # only for a wizard, so a plain fighter's wire output stays byte-identical.
        data["is_wizard"] = True
        data["intelligence"] = figure.intelligence
        data["spells_known"] = list(figure.spells_known)
        # Whether this wizard owns a staff (the Staff spell, p.19). The staff
        # currently in hand already rides the ordinary "weapon" field above.
        data["has_staff"] = figure.has_staff
        data["active_spells"] = dict(figure.active_spells)
        data["spell_protection"] = figure.spell_protection
        data["mana"] = figure.current_st          # ST doubles as the mana pool
        data["max_mana"] = figure.strength
    if isinstance(figure, TarmarFigure):
        # Tarmar fighters track two pools instead of a single ST; surface both
        # so the front end can render a Tarmar sheet (Fatigue, then Body).
        data["model"] = "tarmar"
        data["fatigue"] = figure.current_fatigue
        data["max_fatigue"] = figure.fatigue
        data["body"] = figure.current_body
        data["max_body"] = figure.body
        weapon = figure.ready_weapon
        data["skill"] = figure.weapon_skill.get(weapon.name, 0) if weapon else 0
        # The full attribute spread + per-weapon skills, public for every figure so
        # the read-only sheet is as complete for opponents as for your own (#323).
        # These are display-only wire fields, distinct from the owner/admin edit_spec.
        # Iterated from the one source (TARMAR_EXTRA_STATS) in its declared order,
        # so the wire keys and their order are identical to the hand-listed four.
        for stat in TARMAR_EXTRA_STATS:
            data[stat] = getattr(figure, stat)
        data["weapon_skills"] = {
            carried.name: figure.weapon_skill.get(carried.name, 0)
            for carried in figure.weapons
        }
    return data


def dump_game(state: GameState, *, meta: dict | None = None) -> dict:
    """Full game state plus board-phase metadata for the UI."""
    payload = {
        "turn": state.turn_number,
        "sides": state.sides,
        "figures": [_figure_dict(state, f) for f in state.figures],
        "dropped": [{"label": label_of(hex_pos.col, hex_pos.row), "name": weapon.name}
                    for hex_pos, weapon in state.dropped],
        "log": state.log[-40:],
    }
    if meta:
        payload.update(meta)
    return payload
