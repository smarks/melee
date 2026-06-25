"""WSGI entry point."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "melee_game.settings")
application = get_wsgi_application()
