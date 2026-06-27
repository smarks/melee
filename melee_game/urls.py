"""Root URL configuration."""
from django.urls import include, path

urlpatterns = [
    path("accounts/", include("tarmar_auth.urls")),
    path("", include("board.urls")),
]
