"""ESI-laag — Character Assets.

Platte `requests` in plaats van de swagger-client, hetzelfde patroon als
aa-mijndashboard en aa-corp-hauling: sneller, voorspelbaarder, en je ziet wat
er over de lijn gaat.

Alles wat hier opgehaald wordt gaat in de cache. Dat is geen luxe maar de kern:
één character heeft al gauw duizenden assets over meerdere pagina's, en een
corp-brede zoekopdracht raakt tientallen characters tegelijk.
"""

import logging
import time

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

ESI = "https://esi.evetech.net/latest"
UA = {"User-Agent": "aa-characterassets (Alliance Auth plugin; maintainer: Dutch Legions)"}

ASSETS_SCOPE = "esi-assets.read_assets.v1"
# Niet nodig om te zoeken: zonder dit token kunnen we alleen de naam van een
# spelersstructuur niet ophalen en heet die "Structuur 1035466617946".
STRUCTURES_SCOPE = "esi-universe.read_structures.v1"

# ESI ververst z'n assets-antwoord zelf maar eens per uur (`expires` staat een
# uur na `last-modified`). Korter cachen levert dus geen versere gegevens op,
# alleen meer verkeer.
TTL_ASSETS = 3600
TTL_ITEMNAMEN = 3600
TTL_NAMEN = 30 * 86400      # namen van types en stations veranderen nooit
TTL_STRUCT = 7 * 86400
TTL_STRUCT_FOUT = 3600      # mislukking kort vasthouden: rechten kunnen bijkomen

RETRY_STATUS = {420, 429, 500, 502, 503, 504}
MAX_TRIES = 4

# Eén sessie voor het hele proces: hergebruik van TLS-verbindingen in plaats van
# er honderden opzetten (scheelt tijd en voorkomt poortuitputting op Windows).
_session = requests.Session()
_session.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=8, pool_maxsize=8, max_retries=0,
))


def _call(methode, path, token=None, params=None, json=None):
    """Eén ESI-call met backoff. Geeft (data, headers) of (None, {})."""
    headers = {**UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for poging in range(1, MAX_TRIES + 1):
        try:
            r = _session.request(
                methode, f"{ESI}{path}", headers=headers,
                params={"datasource": "tranquility", **(params or {})},
                json=json, timeout=20,
            )
        except requests.RequestException as exc:
            logger.info("Assets: %s mislukt (poging %s): %s", path, poging, exc)
            time.sleep(min(2 ** poging * 0.25, 4))
            continue

        if r.status_code == 200:
            resterend = r.headers.get("X-Esi-Error-Limit-Remain")
            if resterend is not None and int(resterend) < 10:
                wachten = int(r.headers.get("X-Esi-Error-Limit-Reset", 5))
                logger.warning("Assets: ESI-foutlimiet bijna op (%s over) — %ss wachten",
                               resterend, wachten)
                time.sleep(min(wachten, 10))
            try:
                return r.json(), r.headers
            except ValueError:
                return None, r.headers

        if r.status_code in RETRY_STATUS and poging < MAX_TRIES:
            time.sleep(int(r.headers.get("Retry-After", 0)) or min(2 ** poging * 0.5, 8))
            continue

        logger.info("Assets: %s gaf %s", path, r.status_code)
        return None, r.headers
    return None, {}


def _get(path, token=None, params=None):
    return _call("GET", path, token, params)[0]


def _post(path, body, token=None):
    return _call("POST", path, token, json=body)[0]


def _paged(path, token=None, params=None, max_pages=25):
    """Alle pagina's van een gepagineerde endpoint.

    **Niet** stoppen zodra een pagina korter is dan 1000 regels. Dat lijkt een
    veilige aanname — een volle pagina is 1000, dus korter is de laatste — maar
    ESI vult een pagina niet altijd helemaal: de assets van één character gaven
    999 regels op pagina 1 van 3. Met de lengte als stopteken verlies je dan
    stilzwijgend tweederde van iemands spullen, zonder fout en zonder
    waarschuwing. Het echte aantal pagina's staat in de **X-Pages**-header.
    """
    eerste, headers = _call("GET", path, token, {**(params or {}), "page": 1})
    if not eerste:
        return [], headers
    rijen = list(eerste)
    try:
        paginas = int(headers.get("X-Pages") or 1)
    except (TypeError, ValueError):
        paginas = 1
    for p in range(2, min(paginas, max_pages) + 1):
        blok = _get(path, token, {**(params or {}), "page": p})
        if not blok:
            break
        rijen.extend(blok)
    return rijen, headers


def _blokken(rij, groot):
    """Snijd een lijst in stukken van maximaal `groot`."""
    rij = list(rij)
    for i in range(0, len(rij), groot):
        yield rij[i:i + groot]


# --------------------------------------------------------------------------
# Characters en tokens
# --------------------------------------------------------------------------

def eigen_characters(user):
    """De EveCharacters van deze gebruiker, main eerst."""
    from allianceauth.eveonline.models import EveCharacter

    qs = list(EveCharacter.objects.filter(character_ownership__user=user))
    main = getattr(getattr(user, "profile", None), "main_character", None)
    if main:
        qs.sort(key=lambda c: c.character_id != main.character_id)
    return qs


def corp_characters(user):
    """Elk gekoppeld character in dezelfde corp (of alliance) als de gebruiker.

    "Gekoppeld" betekent hier: er hoort een AA-account bij. Of we er ook echt
    assets van kunnen lezen hangt van het token af; dat filtert
    :func:`met_assets_token` er verderop uit, zodat de pagina kan laten zien
    hoeveel characters er wel en niet meedoen.

    Wie de grens ergens anders wil leggen zet `CHARACTERASSETS_CORPORATION_IDS`
    of `CHARACTERASSETS_ALLIANCE_IDS` in local.py.
    """
    from allianceauth.eveonline.models import EveCharacter
    from django.conf import settings
    from django.db.models import Q

    corps = list(getattr(settings, "CHARACTERASSETS_CORPORATION_IDS", []) or [])
    allys = list(getattr(settings, "CHARACTERASSETS_ALLIANCE_IDS", []) or [])

    if not corps and not allys:
        main = getattr(getattr(user, "profile", None), "main_character", None)
        if not main:
            return []
        corps = [main.corporation_id]

    filter_q = Q(pk__in=[])
    if corps:
        filter_q |= Q(corporation_id__in=corps)
    if allys:
        filter_q |= Q(alliance_id__in=allys)

    return list(EveCharacter.objects
                .filter(character_ownership__isnull=False)
                .filter(filter_q)
                .order_by("character_name")
                .distinct())


def met_assets_token(characters):
    """Splits characters in (heeft een assets-token, heeft er geen).

    Eén query voor de hele lijst: bij een corp van honderd man is een query per
    character het verschil tussen een halve seconde en een pagina die hangt.
    """
    from esi.models import Token

    ids = [c.character_id for c in characters]
    met = set(Token.objects
              .filter(character_id__in=ids, scopes__name=ASSETS_SCOPE)
              .values_list("character_id", flat=True))
    return ([c for c in characters if c.character_id in met],
            [c for c in characters if c.character_id not in met])


def token_for(character_id, scope=ASSETS_SCOPE):
    """Een geldig access-token van dit character met deze scope, of None."""
    from esi.models import Token

    for token in (Token.objects
                  .filter(character_id=character_id, scopes__name=scope)
                  .order_by("-created")):
        try:
            return token.valid_access_token()
        except Exception:  # noqa: BLE001 — verlopen of ingetrokken
            continue
    return None


def _tokens_met_scope(character_ids, scope):
    """Alle bruikbare tokens van deze characters met deze scope, nieuwste eerst."""
    from esi.models import Token

    uit = []
    for token in (Token.objects
                  .filter(character_id__in=list(character_ids), scopes__name=scope)
                  .order_by("-created")):
        try:
            uit.append(token.valid_access_token())
        except Exception:  # noqa: BLE001
            continue
    return uit


# --------------------------------------------------------------------------
# Assets
# --------------------------------------------------------------------------

def assets(character_id, ververs=False):
    """Alles wat dit character bezit, als platte ESI-rijen.

    Geeft (rijen, bijgewerkt) waarbij `bijgewerkt` de last-modified van ESI is —
    eerlijker om te tonen dan "zojuist opgehaald", want CCP werkt dit maar eens
    per uur bij. `ververs=True` gooit onze eigen cache weg; verder dan dat
    ESI-uur komt geen enkele knop.
    """
    key = f"ca_assets_{character_id}"
    tkey = f"ca_assets_tijd_{character_id}"
    if ververs:
        cache.delete(key)
    hit = cache.get(key)
    if hit is not None:
        return hit, cache.get(tkey) or ""

    token = token_for(character_id)
    if not token:
        cache.set(key, [], 300)     # kort: een token kan zo gekoppeld worden
        return [], ""

    rijen, headers = _paged(f"/characters/{character_id}/assets/", token)
    bijgewerkt = headers.get("last-modified") or ""
    cache.set(key, rijen, TTL_ASSETS)
    cache.set(tkey, bijgewerkt, TTL_ASSETS)
    return rijen, bijgewerkt


def item_namen(character_id, item_ids, ververs=False):
    """Namen die de speler zelf aan schepen en containers gaf.

    Zonder dit heet elke Orca "Orca" en elke kist "Station Container", en dat
    is precies het verschil tussen "hij ligt ergens" en "hij ligt in *Ammo
    Hangar 2*". ESI wil hier maximaal 1000 item-ids per call.

    Alleen singletons hebben een naam; de aanroeper filtert daarop, want een
    stapel van 200 raketten heeft er per definitie geen.
    """
    key = f"ca_itemnamen_{character_id}"
    if ververs:
        cache.delete(key)
    hit = cache.get(key)
    if hit is not None:
        return hit

    token = token_for(character_id)
    uit = {}
    if token:
        for blok in _blokken(sorted(set(item_ids)), 1000):
            data = _post(f"/characters/{character_id}/assets/names/", blok, token)
            for r in data or []:
                naam = (r.get("name") or "").strip()
                # ESI geeft letterlijk "None" terug voor een naamloos item.
                if naam and naam != "None":
                    uit[r["item_id"]] = naam
    cache.set(key, uit, TTL_ITEMNAMEN)
    return uit


# --------------------------------------------------------------------------
# Namen van types, stations en structuren
# --------------------------------------------------------------------------

def namen(ids):
    """{id: naam} voor types, stations, systemen, characters en corps.

    **De valkuil van /universe/names/:** één onresolvebaar id laat ESI de hele
    batch met een 400 afwijzen. Wie dan alleen op r.ok controleert, gooit
    duizend goede namen weg om één rot id en laat alles als een nummer op het
    scherm staan. Daarom splitsen we een mislukte batch binair op tot het rotte
    id alleen overblijft; de rest lost gewoon op.
    """
    uit, ontbreekt = {}, []
    for ruw in {int(x) for x in ids if x}:
        hit = cache.get(f"ca_naam_{ruw}")
        if hit is not None:
            if hit:
                uit[ruw] = hit
        else:
            ontbreekt.append(ruw)
    for blok in _blokken(ontbreekt, 1000):
        uit.update(_namen_blok(blok))
    return uit


def _namen_blok(blok):
    if not blok:
        return {}
    data = _post("/universe/names/", list(blok))
    if data is None:
        if len(blok) == 1:
            # Onresolvebaar (verwijderd character, spelersstructuur). Kort
            # onthouden, zodat we het niet elke keer opnieuw proberen.
            cache.set(f"ca_naam_{blok[0]}", "", TTL_STRUCT_FOUT)
            return {}
        mid = len(blok) // 2
        return {**_namen_blok(blok[:mid]), **_namen_blok(blok[mid:])}

    uit = {}
    for r in data:
        naam = r.get("name") or ""
        uit[r["id"]] = naam
        cache.set(f"ca_naam_{r['id']}", naam, TTL_NAMEN)
    return uit


def structuur_namen(structure_ids, character_ids):
    """Namen van spelersstructuren (Upwell), voor zover we erbij mogen.

    Eén token is niet genoeg: /universe/structures/{id}/ geeft **403** als dat
    character geen dockingrechten heeft, en dat verschilt per structuur. Dus
    proberen we ze allemaal tot er één werkt.
    """
    uit, ontbreekt = {}, []
    for sid in {int(s) for s in structure_ids if s}:
        hit = cache.get(f"ca_struct_{sid}")
        if hit is not None:
            uit[sid] = hit
        else:
            ontbreekt.append(sid)
    if not ontbreekt:
        return uit

    tokens = _tokens_met_scope(character_ids, STRUCTURES_SCOPE)
    for sid in ontbreekt:
        naam = ""
        for token in tokens:
            r = _get(f"/universe/structures/{sid}/", token)
            if r:
                naam = r.get("name") or ""
                break
        uit[sid] = naam
        cache.set(f"ca_struct_{sid}", naam, TTL_STRUCT if naam else TTL_STRUCT_FOUT)
    return uit


def leeg_cache(character_ids):
    """Gooi alles weg wat we van deze characters bewaarden."""
    for cid in character_ids:
        cache.delete_many([f"ca_assets_{cid}", f"ca_assets_tijd_{cid}",
                           f"ca_itemnamen_{cid}"])
