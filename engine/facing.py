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


def side_hexes(layout: HexLayout, figure: Figure) -> list[Hex]:
    return [
        layout.neighbor(figure.position, (figure.facing + delta) % 6)
        for delta in (2, -2)
    ]


def rear_hex(layout: HexLayout, figure: Figure) -> Hex:
    return layout.neighbor(figure.position, (figure.facing + 3) % 6)


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
    if observer.posture != Posture.STANDING:    # prone or kneeling: no front (p.7)
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
    if observer.posture != Posture.STANDING:
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

    A prone or airborne enemy has no front and engages no one. Both figures'
    footprints are honoured: ``figure`` is engaged if any of its footprint hexes
    sits in the enemy's (footprint-wide) front.
    """
    if enemy.posture == Posture.PRONE or enemy.collapsed or enemy.flying:
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
    """Whether this one ``enemy`` is in melee contact with ``figure`` (mutual).

    Contact is mutual: it holds if ``figure`` stands in the enemy's front, or a
    standing enemy stands in the figure's own front (adjacent and facing it).
    """
    if enemy.posture == Posture.PRONE or enemy.collapsed or enemy.flying:
        return False
    if is_engaged_by(layout, figure, enemy):
        return True  # the enemy faces the figure
    if (figure.posture == Posture.PRONE or figure.collapsed or figure.flying
            or figure.position is None or enemy.position is None):
        return False
    if not _footprints_adjacent(layout, figure, enemy):
        return False
    return any(
        zone_toward(layout, figure, hex_position) == FRONT
        for hex_position in enemy.footprint(layout)
    )


def engagement_count(layout: HexLayout, figure: Figure, enemies) -> int:
    """How many distinct enemies are in melee contact with ``figure``."""
    return sum(1 for enemy in enemies if _engages(layout, figure, enemy))


def is_engaged(layout: HexLayout, figure: Figure, enemies) -> bool:
    """True if ``figure`` is in melee contact with enough standing enemies.

    Contact is mutual (see :func:`_engages`): two figures face-to-face both count
    as engaged, and both may Shift & Attack. A normal figure is engaged by a
    single foe; a giant needs **two** distinct foes in its front to be engaged
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
