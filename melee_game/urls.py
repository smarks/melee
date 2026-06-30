"""Root URL configuration."""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("origami_auth.urls")),
    path("", include("board.urls")),
]
