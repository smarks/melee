"""Root URL configuration."""
from django.urls import include, path

urlpatterns = [
    path("", include("board.urls")),
]
