"""Django admin — the user + saved-character/-game CRUD half of the admin role (#140).

An is_staff account signs in at /admin/ and gets standard create/read/update/delete
on accounts and saved characters/games. (Editing a fighter's stats mid-game lives
in the game UI itself, #86.)
"""
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from .models import SavedCharacter, SavedGame

admin.site.register(get_user_model(), UserAdmin)


@admin.register(SavedCharacter)
class SavedCharacterAdmin(admin.ModelAdmin):
    list_display = ("name", "profile", "owner", "created")
    list_filter = ("profile",)
    search_fields = ("name",)


@admin.register(SavedGame)
class SavedGameAdmin(admin.ModelAdmin):
    list_display = ("gid", "profile", "updated")
    search_fields = ("gid",)
