"""Views — Character Assets."""

import logging

from django.contrib.auth.decorators import login_required, permission_required
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render

from characterassets import __title__, __version__, assets, esi

logger = logging.getLogger(__name__)

# Hoe lang de voortgang van één ophaalronde blijft staan. Ruim langer dan het
# ophalen duurt, maar kort genoeg om niet in een volgend bezoek te spoken.
VOORTGANG_TTL = 300


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


def _voortgang_key(request, bereik):
    """Eén sleutel per gebruiker én bereik.

    Per gebruiker omdat twee recruiters tegelijk kunnen kijken, en per bereik
    omdat "mijn characters" en "hele corp" verschillende aantallen zijn — anders
    springt de balk van 2/2 naar 3/40.
    """
    return f"ca_voortgang_{request.user.pk}_{bereik}"


def _moet_wachten(request, characters):
    """Duurt dit lang genoeg om er een laadpagina voor te tonen?

    Alleen als er echt nog niets in de cache zit. Bij een warme cache is de
    pagina er in een halve seconde en zou een laadscherm alleen maar een extra
    flits zijn.
    """
    if request.GET.get("ververs") == "1":
        return True
    return not esi.alles_gecached([c.character_id for c in characters])


def _basis(request):
    """De context die beide pagina's delen: bereik, characters, index."""
    bereik, mag_corp = _bereik(request)
    characters = _characters(request, bereik)
    # "Opnieuw ophalen" gaat langs de cache heen, maar gooit hem niet eerst
    # leeg: als ESI ons dan afwijst (420) staat de pagina leeg terwijl er
    # prima gegevens wáren. esi.assets zet de nieuwe alleen bij succes.
    ververs = request.GET.get("ververs") == "1"

    key = _voortgang_key(request, bereik)

    def melden(klaar, totaal, naam):
        cache.set(key, {"klaar": klaar, "totaal": totaal, "naam": naam},
                  VOORTGANG_TTL)

    cache.set(key, {"klaar": 0, "totaal": len(characters), "naam": ""}, VOORTGANG_TTL)
    try:
        idx = assets.bouw(characters, ververs=ververs, melden=melden)
    finally:
        # Klaar is klaar. Blijft dit staan, dan begint het volgende bezoek met
        # een balk die al halverwege is.
        cache.delete(key)

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
        "onvolledig": idx.onvolledig,
        "bijgewerkt": idx.bijgewerkt,
        "aantal_items": len(idx.rijen),
    }


def _laadpagina(request):
    """Toon een laadscherm in plaats van de bezoeker naar een witte pagina te
    laten kijken.

    Het ophalen gebeurt in de request zelf, dus zolang dat loopt komt er niets
    naar de browser. Daarom eerst dit schermpje terugsturen; de JS erin haalt
    dezelfde URL nog eens op met `?werk=1` (dát is de trage call, die de cache
    warm zet) en gaat daarna door naar het echte resultaat. Ondertussen vraagt
    hij elke seconde hoe ver we zijn.

    Zonder JavaScript werkt het ook: dan staat er een gewone link, en die doet
    precies wat de plugin hiervoor deed — wachten tot de pagina er is.
    """
    bereik, mag_corp = _bereik(request)
    ververst = request.GET.get("ververs") == "1"

    werk = request.GET.copy()
    werk["werk"] = "1"

    # Het doel is dezelfde pagina **zonder** ververs: dat is net gebeurd in de
    # werk-call. Bleef het staan, dan vindt die pagina zichzelf weer "moet
    # wachten", toont opnieuw dit scherm, ververst weer... en zo blijft de
    # bezoeker rondjes draaien terwijl ESI de volle laag krijgt.
    doel = request.GET.copy()
    doel.pop("ververs", None)

    pad = request.path
    return render(request, "characterassets/laden.html", {
        "versie": __version__,
        "titel": __title__,
        "doel": f"{pad}?{doel.urlencode()}" if doel else pad,
        "werk_url": f"{pad}?{werk.urlencode()}",
        "bereik": bereik,
        "ververst": ververst,
    })


@login_required
@permission_required("characterassets.basic_access")
def voortgang(request):
    """Hoe ver is het ophalen? {klaar, totaal, naam} voor de balk.

    Geen voortgang in de cache betekent klaar (of nooit begonnen); de
    laadpagina mag daar niet op blijven hangen.
    """
    bereik, _ = _bereik(request)
    stand = cache.get(_voortgang_key(request, bereik))
    if not stand:
        return JsonResponse({"klaar": 0, "totaal": 0, "naam": "", "bezig": False})
    return JsonResponse({**stand, "bezig": True})


@login_required
@permission_required("characterassets.basic_access")
def zoeken(request):
    """Zoekpagina: platte treffers met hun volledige locatiepad."""
    if request.GET.get("werk") != "1":
        bereik, _ = _bereik(request)
        if _moet_wachten(request, _characters(request, bereik)):
            return _laadpagina(request)

    ctx = _basis(request)
    idx = ctx["idx"]

    term = (request.GET.get("q") or "").strip()
    # De knoppenrij is weg; plekken komen nu uit de zoekterm zelf. Als los
    # URL-filter blijft ?vlag= wel bestaan — handig om een link te delen die
    # meteen op Asset Safety staat.
    gekozen = {f for f in request.GET.getlist("vlag") if f in assets.FILTERS}

    treffers, aantal, rijen, uitleg = ([], 0, 0, {})
    if gekozen:
        treffers, aantal, rijen = assets.zoek(idx, term, gekozen, ctx["character_id"])
        uitleg = {"plaatsen": [assets.FILTER_LABEL[f] for f in sorted(gekozen)],
                  "via_url": True}
    elif term:
        treffers, aantal, rijen, uitleg = assets.zoek_slim(
            idx, term, ctx["character_id"])

    ctx.update({
        "actief": "zoeken",
        "q": term,
        "uitleg": uitleg,
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
    if request.GET.get("werk") != "1":
        bereik, _ = _bereik(request)
        if _moet_wachten(request, _characters(request, bereik)):
            return _laadpagina(request)

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
