"""Celery-taken — Character Assets.

De plugin werkt zonder deze taak: dan haalt de eerste bezoeker de assets op en
wacht daarop. Bij een handjevol characters is dat een seconde, bij een corp van
tachtig man is het een pagina die minutenlang lijkt te hangen.

Daarom deze taak: laat de worker de cache warm houden, dan is de pagina voor
iedereen meteen klaar. Eens per uur is genoeg — ESI ververst z'n eigen
assets-antwoord toch niet vaker.

    CELERYBEAT_SCHEDULE["characterassets_warm_cache"] = {
        "task": "characterassets.tasks.warm_cache",
        "schedule": 3600,
    }
"""

import logging

from celery import shared_task

from characterassets import esi

logger = logging.getLogger(__name__)


@shared_task
def warm_cache():
    """Haal de assets van elk gekoppeld character alvast op.

    We lopen de characters hier bewust **serieel** af. De webpagina mag zes
    tegelijk doen omdat er iemand op wacht; een achtergrondtaak heeft die haast
    niet en moet vooral niet met de rest van AA om ESI-budget vechten.
    """
    from allianceauth.eveonline.models import EveCharacter
    from esi.models import Token

    ids = set(Token.objects
              .filter(scopes__name=esi.ASSETS_SCOPE,
                      character_id__in=EveCharacter.objects
                      .filter(character_ownership__isnull=False)
                      .values_list("character_id", flat=True))
              .values_list("character_id", flat=True))

    gelukt, overgeslagen = 0, 0
    for character_id in ids:
        try:
            rijen, _, volledig = esi.assets(character_id, ververs=True)
        except Exception:  # noqa: BLE001 — één character mag de rest niet stoppen
            logger.exception("Character Assets: ophalen mislukt voor %s", character_id)
            continue
        if not volledig:
            # Meestal het ESI-foutbudget. Doorbeuken langs tachtig characters
            # houdt die limiet alleen maar in stand; de volgende ronde is over
            # een uur en dan is het budget allang weer aangevuld.
            overgeslagen += 1
            continue
        if rijen:
            gelukt += 1
            # De zelfgegeven namen horen bij dezelfde ronde: anders staat de
            # eerste bezoeker alsnog te wachten op een call per character.
            ouders = {r["location_id"] for r in rijen
                      if r.get("location_type") == "item"}
            eigen = {r["item_id"] for r in rijen if r.get("is_singleton")}
            esi.item_namen(character_id, ouders & eigen, ververs=True)

    logger.info("Character Assets: cache bijgewerkt voor %s van %s characters "
                "(%s onvolledig)", gelukt, len(ids), overgeslagen)
    return gelukt
