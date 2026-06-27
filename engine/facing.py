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
    """The three hexes in front of ``figure`` (no bounds-checking)."""
    return [
        layout.neighbor(figure.position, (figure.facing + delta) % 6)
        for delta in (-1, 0, 1)
    ]


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
    line = layout.line(observer.position, point)
    direction = layout.direction_to(observer.position, line[1])
    if direction is None:
        return None
    if observer.posture == Posture.PRONE:
        return REAR
    return zone_of_direction(observer.facing, direction)


def is_engaged_by(layout: HexLayout, figure: Figure, enemy: Figure) -> bool:
    """True if ``figure`` stands in ``enemy``'s front hex (so enemy engages it).

    A prone enemy has no front and engages no one.
    """
    if enemy.posture == Posture.PRONE or enemy.collapsed:
        return False
    if figure.position is None or enemy.position is None:
        return False
    if layout.distance(enemy.position, figure.position) != 1:
        return False  # engagement requires adjacency
    return zone_toward(layout, enemy, figure.position) == FRONT


def is_engaged(layout: HexLayout, figure: Figure, enemies) -> bool:
    """True if ``figure`` is in melee contact with any standing enemy.

    Contact is mutual: a figure is engaged if it stands in an enemy's front hex
    **or** a standing enemy stands in the figure's own front hex (i.e. it is
    adjacent and facing the enemy). So two figures standing face-to-face both
    count as engaged — and both may Shift & Attack.
    """
    for enemy in enemies:
        if enemy.posture == Posture.PRONE or enemy.collapsed:
            continue
        if is_engaged_by(layout, figure, enemy):
            return True  # the enemy faces the figure
        if (figure.posture != Posture.PRONE and not figure.collapsed
                and figure.position is not None and enemy.position is not None
                and layout.distance(figure.position, enemy.position) == 1
                and zone_toward(layout, figure, enemy.position) == FRONT):
            return True  # the figure faces the enemy
    return False


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
