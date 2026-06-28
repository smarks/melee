"""Root URL configuration."""
from django.urls import include, path

urlpatterns = [
    path("accounts/", include("origami_auth.urls")),
    path("", include("board.urls")),
]
