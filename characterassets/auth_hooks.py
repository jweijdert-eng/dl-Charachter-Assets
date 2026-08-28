"""Hook into Alliance Auth — Character Assets.

Eén menu-item; zoeken en de boomweergave zijn tabbladen binnen de pagina zelf.

LET OP bij het hernoemen van deze klasse of module: AA bepaalt de identiteit van
een menu-item met sha256(module.KlasseNaam) en zet die hash in de database. Kan
een draaiend proces die hash niet meer oplossen, dan knalt het menu — en omdat
het menu op élke pagina getekend wordt, ligt daarmee het hele dashboard plat.
Installeren, verwijderen of hernoemen dus altijd samen met een herstart van de
webserver.
"""

from django.utils.translation import gettext_lazy as _

from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook

from characterassets import urls


class CharacterAssetsMenuItem(MenuItemHook):
    """Menu-item, alleen zichtbaar voor wie de plugin mag gebruiken."""

    def __init__(self):
        MenuItemHook.__init__(
            self,
            _("Character Assets"),
            "fas fa-boxes-stacked fa-fw",
            "characterassets:index",
            # Zonder expliciete order krijgt een item 9999, waar alle andere
            # plugins ook zitten en de volgorde dus willekeurig wordt.
            order=1020,
            navactive=["characterassets:"],
        )

    def render(self, request):
        if request.user.has_perm("characterassets.basic_access"):
            return MenuItemHook.render(self, request)
        return ""


@hooks.register("menu_item_hook")
def register_menu():
    return CharacterAssetsMenuItem()


@hooks.register("url_hook")
def register_urls():
    return UrlHook(urls, "characterassets", r"^characterassets/")
