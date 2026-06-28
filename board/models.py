"""Persistent data for the board app: a player's saved fighters."""
from __future__ import annotations

from django.conf import settings
from django.db import models


class SavedCharacter(models.Model):
    """A fighter spec a logged-in player saved to reuse in the setup wizard."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="saved_characters")
    name = models.CharField(max_length=80)
    profile = models.CharField(max_length=32)   # "Classic Melee" / "Tarmar"
    spec = models.JSONField()                    # the chargen fighter spec
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"],
                                    name="unique_owner_character_name"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.profile})"

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name,
                "profile": self.profile, "spec": self.spec}


class SavedGame(models.Model):
    """A persisted in-progress game, so a match survives a server restart (#12).

    ``data`` is the full serialized board game-dict produced by
    :func:`board.persistence.game_to_json` — the GameState plus the board's phase
    machine, controllers, and seats. Keyed by the game's short ``gid`` so a save
    overwrites the previous snapshot of the same game.
    """

    gid = models.CharField(max_length=32, unique=True)
    profile = models.CharField(max_length=32, blank=True)   # "Classic Melee" / "Tarmar"
    data = models.JSONField()                               # serialized board game-dict
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated"]

    def __str__(self) -> str:
        return f"SavedGame {self.gid} ({self.profile})"
