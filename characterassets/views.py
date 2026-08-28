"""Views — Character Assets."""

import logging

from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import render

from characterassets import __title__, __version__, assets, esi

logger = logging.getLogger(__name__)


def _bereik(request):
    """Wiens assets we doorzoeken: 'mijn' of 'corp'.

    De corp-keuze hangt aan een eigen permissie. Wie die niet heeft mag het ook
    niet via de URL forceren, dus valt die stilletjes terug op de eigen
    characters — een foutmelding zou hier alleen maar verklappen dat de knop
    bestaat.
    """
    mag_corp = request.user.has_perm("characterassets.corp_access")
    keuze = request.GET.get("bereik") or ("corp" if mag_corp else "mijn")
    if keuze == "corp" and not mag_corp:
        keuze = "mijn"
    return keuze, mag_corp


def _characters(request, bereik):
    if bereik == "corp":
        return esi.corp_characters(request.user)
    return esi.eigen_characters(request.user)


def _basis(request):
    """De context die beide pagina's delen: bereik, characters, index."""
    bereik, mag_corp = _bereik(request)
    characters = _characters(request, bereik)
    ververs = request.GET.get("ververs") == "1"
    if ververs:
        esi.leeg_cache([c.character_id for c in characters])

    idx = assets.bouw(characters, ververs=ververs)

    # Filteren op één character: alleen zinvol als we er meerdere hebben.
    try:
        character_id = int(request.GET.get("character") or 0) or None
    except ValueError:
        character_id = None
    if character_id and character_id not in idx.chars:
        character_id = None

    return {
        "versie": __version__,
        "titel": __title__,
        "bereik": bereik,
        "mag_corp": mag_corp,
        "idx": idx,
        "characters": sorted(idx.chars.items(), key=lambda kv: kv[1].lower()),
        "character_id": character_id,
        "zonder_token": idx.zonder_token,
        "bijgewerkt": idx.bijgewerkt,
        "aantal_items": len(idx.rijen),
    }


@login_required
@permission_required("characterassets.basic_access")
def zoeken(request):
    """Zoekpagina: platte treffers met hun volledige locatiepad."""
    ctx = _basis(request)
    idx = ctx["idx"]

    term = (request.GET.get("q") or "").strip()
    gekozen = {f for f in request.GET.getlist("vlag") if f in assets.FILTERS}

    treffers, aantal, rijen = ([], 0, 0)
    if term or gekozen:
        treffers, aantal, rijen = assets.zoek(
            idx, term, gekozen, ctx["character_id"])

    ctx.update({
        "actief": "zoeken",
        "q": term,
        "gekozen": gekozen,
        "filters": [(f, assets.FILTER_LABEL[f], f in gekozen) for f in assets.FILTERS],
        "treffers": treffers,
        "aantal": aantal,
        "stapels": rijen,
        "afgekapt": aantal > len(treffers),
        "gezocht": bool(term or gekozen),
    })
    return render(request, "characterassets/zoeken.html", ctx)


@login_required
@permission_required("characterassets.basic_access")
def boom(request):
    """Boomweergave: blader door één locatie heen."""
    ctx = _basis(request)
    idx = ctx["idx"]

    try:
        wortel = int(request.GET.get("locatie") or 0) or None
    except ValueError:
        wortel = None

    plekken = sorted(idx.wortels.values(), key=lambda w: -w["items"])
    if wortel is None and len(plekken) == 1:
        wortel = plekken[0]["id"]

    term = (request.GET.get("q") or "").strip()
    knopen, afgekapt = ([], False)
    if wortel is not None:
        knopen, afgekapt = assets.boom(idx, wortel, term)

    ctx.update({
        "actief": "boom",
        "q": term,
        "plekken": plekken,
        "wortel": wortel,
        "wortel_naam": idx.wortels.get(wortel, {}).get("naam", ""),
        "knopen": knopen,
        "afgekapt": afgekapt,
        "max_knopen": assets.MAX_KNOPEN,
    })
    return render(request, "characterassets/boom.html", ctx)
