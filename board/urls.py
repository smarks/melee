from django.urls import path

from . import views

app_name = "board"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/catalog", views.api_catalog, name="api_catalog"),
    path("api/best_weapons", views.api_best_weapons, name="api_best_weapons"),
    path("api/characters", views.api_characters, name="api_characters"),
    path("api/characters/<int:pk>/delete", views.api_character_delete,
         name="api_character_delete"),
    path("api/game/new", views.api_new_game, name="api_new_game"),
    path("api/game/new_custom", views.api_new_custom, name="api_new_custom"),
    path("api/game/<str:gid>", views.api_state, name="api_state"),
    path("api/game/<str:gid>/options", views.api_options, name="api_options"),
    path("api/game/<str:gid>/action", views.api_action, name="api_action"),
    path("api/game/<str:gid>/seat", views.api_seat, name="api_seat"),
]
