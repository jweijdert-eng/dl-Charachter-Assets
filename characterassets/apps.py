"""App Configuration"""

from django.apps import AppConfig

from characterassets import __version__


class CharacterAssetsConfig(AppConfig):
    name = "characterassets"
    label = "characterassets"
    verbose_name = f"Character Assets v{__version__}"
