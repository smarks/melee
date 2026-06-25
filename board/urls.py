from django.urls import path

from . import views

app_name = "board"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/game/new", views.api_new_game, name="api_new_game"),
    path("api/game/<str:gid>", views.api_state, name="api_state"),
    path("api/game/<str:gid>/options", views.api_options, name="api_options"),
    path("api/game/<str:gid>/action", views.api_action, name="api_action"),
]
