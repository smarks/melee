"""
Facing, front/side/rear hexes, and engagement (Section VI).

Each figure faces one of the six hex directions. Of its six surrounding hexes,
three are *front* (the faced hex and its two flanks), two are *side*, and one is
*rear*. Facing decides three things:

* **Engagement** -- a figure is *engaged* if it stands in an enemy's front hex.
* **Who may be attacked** -- only an enemy in one of your three front hexes.
* **Attack bonuses** -- striking from a foe's side is +2 DX, from the rear +4.

Direction indices match :data:`hexarena.hex.CUBE_DIRECTIONS` for a flat-top
grid, so ``facing`` is simply that index.
"""
from __future__ import annotations

from hexarena.hex import Hex, HexLayout

from .figure import Figure, Posture

FRONT = "front"
SIDE = "side"
REAR = "rear"


def zone_of_direction(facing: int, direction_index: int) -> str:
    """Classify ``direction_index`` relative to a figure facing ``facing``."""
    offset = (direction_index - facing) % 6
    if offset in (0, 1, 5):
        return FRONT
    if offset in (2, 4):
        return SIDE
    return REAR  # offset == 3


def facing_toward(layout: HexLayout, from_hex: Hex, to_hex: Hex) -> int:
    """Direction index (0-5) whose front points most directly at ``to_hex``.

    Walk the six headings out of ``from_hex`` and keep the one whose front hex
    lands nearest ``to_hex``. For an adjacent target this is the heading that puts
    it in the front hex. The single source for "turn to face that hex", shared by
    the AI (:mod:`engine.ai`) and the scenario seater (:mod:`board.scenario`).
    """
    best_dir, best_dist = 0, None
    for direction in range(6):
        distance = layout.distance(layout.neighbor(from_hex, direction), to_hex)
        if best_dist is None or distance < best_dist:
            best_dir, best_dist = direction, distance
    return best_dir


def front_hexes(layout: HexLayout, figure: Figure) -> list[Hex]:
    """The hexes in front of ``figure`` (no bounds-checking).

    For a single-hex figure these are exactly the three front hexes (the faced
    hex and its two flanks), in the original ``(-1, 0, 1)`` order. For a
    multi-hex figure (the giant) the front is the union of every footprint hex's
    own three front hexes, deduped and with the figure's own footprint removed --
    the forward edge of the whole cluster.
    """
    footprint = figure.footprint(layout)
    footprint_set = set(footprint)
    fronts: list[Hex] = []
    for hex_position in footprint:
        for delta in (-1, 0, 1):
            candidate = layout.neighbor(hex_position, (figure.facing + delta) % 6)
            if candidate not in footprint_set and candidate not in fronts:
                fronts.append(candidate)
    return fronts


def zone_toward(layout: HexLayout, observer: Figure, point: Hex) -> str | None:
    """Which zone of ``observer`` the ``point`` lies in, or ``None``.

    Works at any range (not just adjacency) by taking the direction of the first
    step along the line from the observer to the point -- so a shield correctly
    covers frontal missile fire, not only adjacent blows. A figure on the ground
    faces "rear" in all six directions (it has no front), per Section VI.
    """
    if observer.position is None or point == observer.position:
        return None
    footprint = observer.footprint(layout)
    if len(footprint) > 1:                       # multi-hex observer (the giant)
        return _multi_zone_toward(layout, observer, point, footprint)
    line = layout.line(observer.position, point)
    direction = layout.direction_to(observer.position, line[1])
    if direction is None:
        return None
    # A giant snake's side hexes count as front hexes for all purposes (p.21):
    # it strikes so fast it has no flank or rear to exploit, so every attack on
    # it is a frontal one.
    if observer.all_front:
        return FRONT
    if observer.posture == Posture.PRONE:        # prone only: no front (#354)
        # A KNEELING figure keeps its front per Spencer's rulebook ruling (#354);
        # only PRONE loses it. The engine has no crawl/pick-up posture.
        return REAR
    return zone_of_direction(observer.facing, direction)


def _multi_zone_toward(
    layout: HexLayout, observer: Figure, point: Hex, footprint: list[Hex]
) -> str | None:
    """Zone of a multi-hex observer (the giant) toward ``point``.

    Front if the point lies in the cluster's front edge; otherwise the zone is
    read from the footprint hex nearest the point, using the giant's facing -- so
    a flank/rear attacker still earns its facing bonus against a giant.
    """
    if observer.all_front:
        return FRONT
    if observer.posture == Posture.PRONE:        # prone only: no front (#354)
        return REAR
    if point in set(front_hexes(layout, observer)):
        return FRONT
    nearest = min(footprint, key=lambda hex_position: layout.distance(hex_position, point))
    if point == nearest:                         # standing on a footprint hex (HTH)
        return REAR
    line = layout.line(nearest, point)
    if len(line) < 2:
        return None
    direction = layout.direction_to(nearest, line[1])
    if direction is None:
        return None
    return zone_of_direction(observer.facing, direction)


def _footprints_adjacent(layout: HexLayout, figure: Figure, other: Figure) -> bool:
    """Whether any hex of one figure's footprint is adjacent to the other's.

    For two single-hex figures this is just ``distance == 1``; it generalises
    adjacency to a multi-hex figure (the giant) without changing the single-hex
    answer.
    """
    figure_footprint = figure.footprint(layout)
    other_footprint = other.footprint(layout)
    return any(
        layout.distance(here, there) == 1
        for here in figure_footprint
        for there in other_footprint
    )


def is_engaged_by(layout: HexLayout, figure: Figure, enemy: Figure) -> bool:
    """True if ``figure`` stands in ``enemy``'s front hex (so enemy engages it).

    A prone or airborne enemy has no front and engages no one. Engagement also
    needs an *armed* enemy (p.9): "the only 'unarmed' enemy in this game is a
    wizard who has no staff" (Wizard, rules line 536), so a staffless (empty-
    handed) wizard engages no one either — foes in its front stay free to move.
    Both figures' footprints are honoured: ``figure`` is engaged if any of its
    footprint hexes sits in the enemy's (footprint-wide) front.
    """
    if enemy.posture == Posture.PRONE or enemy.collapsed or enemy.flying:
        return False
    if enemy.unarmed_wizard:       # a staffless wizard is unarmed and engages no one
        return False
    if figure.position is None or enemy.position is None:
        return False
    if not _footprints_adjacent(layout, figure, enemy):
        return False  # engagement requires adjacency
    return any(
        zone_toward(layout, enemy, hex_position) == FRONT
        for hex_position in figure.footprint(layout)
    )


def _engages(layout: HexLayout, figure: Figure, enemy: Figure) -> bool:
    """Whether ``enemy`` engages ``figure`` -- i.e. ``figure`` stands in the
    enemy's front hex (p.9).

    Engagement is one-directional: you are engaged only by a foe whose front hex
    you occupy. A figure that has slipped behind or beside an enemy is NOT
    engaged by it, even while turned to face it -- it stays free to move and may
    strike the enemy's exposed flank or rear. Two figures standing face-to-face
    are each still engaged, because each occupies the other's front.
    """
    return is_engaged_by(layout, figure, enemy)


def engagement_count(layout: HexLayout, figure: Figure, enemies) -> int:
    """How many distinct enemies are in melee contact with ``figure``."""
    return sum(1 for enemy in enemies if _engages(layout, figure, enemy))


def is_engaged(layout: HexLayout, figure: Figure, enemies) -> bool:
    """True if ``figure`` is in melee contact with enough standing enemies.

    Engagement is one-directional (see :func:`_engages`): ``figure`` is engaged
    only by foes whose front hex it occupies. Two figures face-to-face are each
    engaged (each is in the other's front) and both may Shift & Attack; but a
    figure behind or beside a foe is free. A normal figure is engaged by a single
    such foe; a giant needs **two** distinct foes in its front to be engaged
    (``needs_two_to_engage``) -- one lone figure cannot pin it (p.20). An
    airborne figure is never engaged.
    """
    if figure.flying:
        return False
    needed = 2 if figure.needs_two_to_engage else 1
    return engagement_count(layout, figure, enemies) >= needed


def attack_zone(layout: HexLayout, attacker: Figure, target: Figure) -> str | None:
    """Zone of ``target`` that ``attacker`` strikes from (for the DX bonus).

    Returns ``None`` if the attacker is not adjacent to the target.
    """
    if attacker.position is None or target.position is None:
        return None
    return zone_toward(layout, target, attacker.position)


def facing_bonus(zone: str | None) -> int:
    """DX bonus for striking from a given zone (Attacks, p.10)."""
    if zone == SIDE:
        return 2
    if zone == REAR:
        return 4
    return 0


def format_situational_parts(zone: str | None, *, ignore_facing: bool,
                             range_penalty: int, situational_note: str) -> list[str]:
    """The shared trailing fragments of a to-hit explanation -- the facing bonus,
    the range penalty, and any situational note -- used by both rule profiles'
    breakdown strings so the wording stays consistent."""
    parts: list[str] = []
    if not ignore_facing and facing_bonus(zone):
        parts.append(f"+{facing_bonus(zone)} {'rear' if zone == REAR else 'flank'}")
    if range_penalty:
        parts.append(f"{range_penalty:+d} range")
    if situational_note:
        parts.append(situational_note)
    return parts
