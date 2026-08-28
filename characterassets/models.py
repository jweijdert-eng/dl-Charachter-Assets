"""Models — Character Assets.

De plugin bewaart niets: assets komen live uit ESI en gaan in de cache. ESI
ververst z'n eigen assets-antwoord maar eens per uur, dus een eigen kopie in de
database zou geen seconde verser zijn dan de cache — alleen meer onderhoud.

Blijft over: een meta-model voor de permissies.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class General(models.Model):
    """Meta-model voor permissies."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ("basic_access", _("Kan de eigen assets doorzoeken")),
            ("corp_access", _("Kan de assets van de hele corp doorzoeken")),
        )
