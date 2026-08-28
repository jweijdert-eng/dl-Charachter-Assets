"""ESI-laag — Character Assets.

Platte `requests` in plaats van de swagger-client, hetzelfde patroon als
aa-mijndashboard en aa-corp-hauling: sneller, voorspelbaarder, en je ziet wat
er over de lijn gaat.

Alles wat hier opgehaald wordt gaat in de cache. Dat is geen luxe maar de kern:
één character heeft al gauw duizenden assets over meerdere pagina's, en een
corp-brede zoekopdracht raakt tientallen characters tegelijk.

**Foutbudget.** ESI telt je fouten: ongeveer 100 per 60 seconden, en dan krijg
je **420 (error limited)** op *alles* — ook op calls die het prima zouden doen,
en ook in de rest van Alliance Auth, want dat budget geldt per IP en niet per
plugin. Elke 4xx telt mee, dus een lus die twintig tokens langs dezelfde
structuur probeert graaft z'n eigen gat. Daarom houdt deze module één gedeelde
pauze bij (:func:`_pauzeer`) die elk antwoord bijwerkt, ook de foute.
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
TTL_ONVOLLEDIG = 300        # half opgehaald: kort bewaren, gauw opnieuw proberen

# 420 staat hier **niet** tussen. Dat is geen hik maar een straf, en het nog
# eens proberen maakt de straf langer. Die krijgt z'n eigen afhandeling in
# `_call`.
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_TRIES = 4

# Onder dit aantal resterende fouten stoppen we uit onszelf. Niet tot 0 wachten:
# de rest van Alliance Auth deelt hetzelfde budget en moet ook nog wat kunnen.
FOUT_DREMPEL = 20
PAUZE_KEY = "ca_esi_pauze_tot"   # unix-tijd waarop we weer mogen vragen
MAX_WACHT = 10                   # zo lang wacht een pagina hoogstens op budget

# Eén sessie voor het hele proces: hergebruik van TLS-verbindingen in plaats van
# er honderden opzetten (scheelt tijd en voorkomt poortuitputting op Windows).
_session = requests.Session()
_session.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=8, pool_maxsize=8, max_retries=0,
))

# Waarden van `fout` die de aanroepers uit elkaar moeten houden.
FOUT_LIMIET = "foutlimiet"   # het budget is op — niets meer proberen
FOUT_CALL = "call"           # deze ene call ging mis


# --------------------------------------------------------------------------
# Foutbudget
# --------------------------------------------------------------------------

def _pauze_rest():
    """Seconden dat we niets mogen vragen. 0 = ga je gang."""
    try:
        tot = float(cache.get(PAUZE_KEY) or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, tot - time.time())


def _pauzeer(seconden, reden):
    """Leg alle ESI-verkeer van deze plugin even stil.

    In de cache en niet in een variabele: de webserver, de Celery-worker en de
    zes threads van een corp-zoekopdracht delen hetzelfde foutbudget, dus die
    moeten ook dezelfde pauze zien.
    """
    seconden = max(1.0, min(float(seconden or 60), 120.0))
    tot = time.time() + seconden
    try:
        huidig = float(cache.get(PAUZE_KEY) or 0)
    except (TypeError, ValueError):
        huidig = 0.0
    if tot > huidig:
        cache.set(PAUZE_KEY, tot, int(seconden) + 5)
        logger.warning("Assets: ESI-foutbudget — %ss pauze (%s)", int(seconden), reden)


def _lees_budget(headers):
    """Kijk bij **elk** antwoord hoeveel foutbudget er nog is.

    Juist bij een fout antwoord, want dat is precies het moment waarop het
    budget slinkt. Alleen naar de header van een 200 kijken is nutteloos: dan
    merk je de 420 pas als je er al in zit.
    """
    try:
        resterend = int(headers.get("X-Esi-Error-Limit-Remain"))
    except (TypeError, ValueError, AttributeError):
        return
    if resterend > FOUT_DREMPEL:
        return
    try:
        reset = int(headers.get("X-Esi-Error-Limit-Reset") or 60)
    except (TypeError, ValueError):
        reset = 60
    _pauzeer(reset, f"nog {resterend} fouten over")


# --------------------------------------------------------------------------
# Calls
# --------------------------------------------------------------------------

def _call(methode, path, token=None, params=None, json=None):
    """Eén ESI-call met backoff. Geeft (data, headers, fout).

    `fout` is None als het gelukt is, `FOUT_LIMIET` als het foutbudget op is en
    `FOUT_CALL` bij al het andere. Dat onderscheid is geen franje: bij een
    foutlimiet moet de aanroeper **stoppen** in plaats van iets anders proberen.
    """
    headers = {**UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for poging in range(1, MAX_TRIES + 1):
        rest = _pauze_rest()
        if rest:
            if rest > MAX_WACHT:
                logger.info("Assets: %s overgeslagen, nog %ss foutpauze", path, int(rest))
                return None, {}, FOUT_LIMIET
            time.sleep(rest)

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

        _lees_budget(r.headers)

        if r.status_code == 200:
            try:
                return r.json(), r.headers, None
            except ValueError:
                return None, r.headers, FOUT_CALL

        if r.status_code == 420:
            # Het budget is al op. Nog een poging is nog een fout; de pauze
            # staat inmiddels, dus we geven het hier meteen op.
            _pauzeer(int(r.headers.get("X-Esi-Error-Limit-Reset") or 60), "420 van ESI")
            return None, r.headers, FOUT_LIMIET

        if r.status_code in RETRY_STATUS and poging < MAX_TRIES:
            time.sleep(int(r.headers.get("Retry-After", 0)) or min(2 ** poging * 0.5, 8))
            continue

        logger.info("Assets: %s gaf %s", path, r.status_code)
        return None, r.headers, FOUT_CALL
    return None, {}, FOUT_CALL


def _get_of_fout(path, token=None, params=None):
    """(data, fout) — elke aanroeper hier moet weten *waarom* iets misging."""
    data, _, fout = _call("GET", path, token, params)
    return data, fout


def _post_of_fout(path, body, token=None):
    data, _, fout = _call("POST", path, token, json=body)
    return data, fout


def _paged(path, token=None, params=None, max_pages=25):
    """Alle pagina's van een gepagineerde endpoint. Geeft (rijen, headers, volledig).

    **Niet** stoppen zodra een pagina korter is dan 1000 regels. Dat lijkt een
    veilige aanname — een volle pagina is 1000, dus korter is de laatste — maar
    ESI vult een pagina niet altijd helemaal: de assets van één character gaven
    999 regels op pagina 1 van 3. Met de lengte als stopteken verlies je dan
    stilzwijgend tweederde van iemands spullen, zonder fout en zonder
    waarschuwing. Het echte aantal pagina's staat in de **X-Pages**-header.

    Diezelfde stilte dreigt als pagina 3 mislukt. Daarom komt `volledig` mee
    terug: wie een half antwoord een uur lang als de waarheid opslaat, laat
    iemands halve hangar verdwijnen zonder dat er iets misging op het scherm.
    """
    eerste, headers, fout = _call("GET", path, token, {**(params or {}), "page": 1})
    if fout or eerste is None:
        return [], headers, False
    rijen = list(eerste)
    try:
        paginas = int(headers.get("X-Pages") or 1)
    except (TypeError, ValueError):
        paginas = 1
    for p in range(2, min(paginas, max_pages) + 1):
        blok, _, fout = _call("GET", path, token, {**(params or {}), "page": p})
        if fout or blok is None:
            return rijen, headers, False
        if not blok:
            break
        rijen.extend(blok)
    return rijen, headers, paginas <= max_pages


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


def _tokens_per_character(character_ids, scope):
    """{character_id: access-token} — het nieuwste bruikbare token per character.

    Per character en niet als platte lijst, want voor structuurnamen maakt het
    uit *wiens* token je pakt: alleen wie er mag docken krijgt een naam terug.
    """
    from esi.models import Token

    uit = {}
    for token in (Token.objects
                  .filter(character_id__in=list(character_ids), scopes__name=scope)
                  .order_by("-created")):
        if token.character_id in uit:
            continue
        try:
            uit[token.character_id] = token.valid_access_token()
        except Exception:  # noqa: BLE001
            continue
    return uit


# --------------------------------------------------------------------------
# Assets
# --------------------------------------------------------------------------

def assets(character_id, ververs=False):
    """Alles wat dit character bezit, als platte ESI-rijen.

    Geeft (rijen, bijgewerkt, volledig) waarbij `bijgewerkt` de last-modified
    van ESI is — eerlijker om te tonen dan "zojuist opgehaald", want CCP werkt
    dit maar eens per uur bij. `ververs=True` haalt langs de cache heen op;
    verder dan dat ESI-uur komt geen enkele knop.

    Een mislukte verversing gooit de oude gegevens **niet** weg. Anders maakt
    één 420 het erger dan niets doen: dan is de cache leeg, staat de pagina
    leeg, en gaat de volgende bezoeker het meteen weer proberen.
    """
    key = f"ca_assets_{character_id}"
    tkey = f"ca_assets_tijd_{character_id}"
    vkey = f"ca_assets_vol_{character_id}"

    def _uit_cache():
        hit = cache.get(key)
        if hit is None:
            return None
        vol = cache.get(vkey)
        return hit, cache.get(tkey) or "", True if vol is None else bool(vol)

    if not ververs:
        gecached = _uit_cache()
        if gecached is not None:
            return gecached

    token = token_for(character_id)
    if not token:
        cache.set(key, [], 300)     # kort: een token kan zo gekoppeld worden
        cache.set(vkey, True, 300)
        return [], "", True

    rijen, headers, volledig = _paged(f"/characters/{character_id}/assets/", token)
    if not rijen and not volledig:
        # Er is niets binnengekomen. Wat er stond is oud maar waar, en dat is
        # beter dan een lege pagina.
        oud = _uit_cache()
        if oud is not None:
            return oud
        return [], "", False

    if not volledig:
        logger.warning("Assets: onvolledig antwoord voor %s (%s rijen) — kort bewaard",
                       character_id, len(rijen))

    bijgewerkt = headers.get("last-modified") or ""
    ttl = TTL_ASSETS if volledig else TTL_ONVOLLEDIG
    cache.set(key, rijen, ttl)
    cache.set(tkey, bijgewerkt, ttl)
    cache.set(vkey, volledig, ttl)
    return rijen, bijgewerkt, volledig


def alles_gecached(character_ids):
    """Staat van élk van deze characters de assets al in de cache?

    Puur om te beslissen of de bezoeker een laadpagina krijgt of meteen z'n
    resultaat. Doet zelf geen enkele call — de vraag "gaat dit lang duren?" mag
    natuurlijk niet zelf lang duren.
    """
    ids = list(character_ids)
    if not ids:
        return True
    gevonden = cache.get_many([f"ca_assets_{cid}" for cid in ids])
    return len(gevonden) == len(set(ids))


def item_namen(character_id, item_ids, ververs=False):
    """Namen die de speler zelf aan schepen en containers gaf.

    Zonder dit heet elke Bustard "Bustard" en elke kist "Station Container", en dat
    is precies het verschil tussen "hij ligt ergens" en "hij ligt in *Ammo
    Hangar 2*". ESI wil hier maximaal 1000 item-ids per call.

    Alleen singletons hebben een naam; de aanroeper filtert daarop, want een
    stapel van 200 raketten heeft er per definitie geen.
    """
    key = f"ca_itemnamen_{character_id}"
    if not ververs:
        hit = cache.get(key)
        if hit is not None:
            return hit

    token = token_for(character_id)
    uit, mislukt = {}, False
    if token:
        for blok in _blokken(sorted(set(item_ids)), 1000):
            data, fout = _post_of_fout(f"/characters/{character_id}/assets/names/",
                                       blok, token)
            if fout:
                mislukt = True
                if fout == FOUT_LIMIET:
                    break
                continue
            for r in data or []:
                naam = (r.get("name") or "").strip()
                # ESI geeft letterlijk "None" terug voor een naamloos item.
                if naam and naam != "None":
                    uit[r["item_id"]] = naam

    if mislukt:
        # Een halve namenlijst niet een uur lang als de waarheid bewaren, en al
        # helemaal niet over een volledige heen zetten.
        oud = cache.get(key)
        if oud:
            return oud
        cache.set(key, uit, TTL_ONVOLLEDIG)
        return uit

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
    data, fout = _post_of_fout("/universe/names/", list(blok))
    if fout == FOUT_LIMIET:
        # Splitsen zou hier tien keer opnieuw tegen dezelfde dichte deur
        # bonken, en elke bons is weer een fout. Nummers op het scherm zijn
        # vervelend; het foutbudget van heel Alliance Auth opmaken is erger.
        return {}
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


# Hoeveel tokens we hoogstens langs één structuur halen. Elk token dat er niet
# mag docken kost een 403, en aan 403's gaat het foutbudget op: twintig
# characters maal dertig structuren is zeshonderd fouten, oftewel een
# gegarandeerde 420 binnen een minuut.
MAX_TOKENS_PER_STRUCTUUR = 3


def structuur_namen(structure_ids, character_ids, eigenaars=None):
    """Namen van spelersstructuren (Upwell), voor zover we erbij mogen.

    Eén token is niet genoeg: /universe/structures/{id}/ geeft **403** als dat
    character geen dockingrechten heeft, en dat verschilt per structuur.

    Maar blind alle tokens langs alle structuren halen is precies hoe je jezelf
    error-limited krijgt. Daarom `eigenaars`: {structuur_id: {character_id}} van
    wie er spullen liggen. Die persoon heeft er vrijwel zeker dockingrechten —
    z'n hangar staat er tenslotte — dus die vragen we eerst, en daarna hoogstens
    nog een paar anderen.
    """
    eigenaars = eigenaars or {}
    uit, ontbreekt = {}, []
    for sid in {int(s) for s in structure_ids if s}:
        hit = cache.get(f"ca_struct_{sid}")
        if hit is not None:
            uit[sid] = hit
        else:
            ontbreekt.append(sid)
    if not ontbreekt:
        return uit

    tokens = _tokens_per_character(character_ids, STRUCTURES_SCOPE)
    if not tokens:
        # Niemand heeft de structures-scope. Dan is elke call bij voorbaat
        # zinloos; niet proberen scheelt een fout per structuur.
        for sid in ontbreekt:
            uit[sid] = ""
            cache.set(f"ca_struct_{sid}", "", TTL_STRUCT_FOUT)
        return uit

    overig = list(tokens)
    for sid in ontbreekt:
        if _pauze_rest():
            # Budget op. Liever een nummer op het scherm dan de hele auth
            # plat; niet cachen, dan proberen we het straks gewoon opnieuw.
            uit[sid] = ""
            continue

        volgorde = [cid for cid in eigenaars.get(sid) or () if cid in tokens]
        volgorde += [cid for cid in overig if cid not in volgorde]

        naam, gestopt = "", False
        for cid in volgorde[:MAX_TOKENS_PER_STRUCTUUR]:
            r, fout = _get_of_fout(f"/universe/structures/{sid}/", tokens[cid])
            if r:
                naam = r.get("name") or ""
                break
            if fout == FOUT_LIMIET:
                gestopt = True
                break

        uit[sid] = naam
        if naam:
            cache.set(f"ca_struct_{sid}", naam, TTL_STRUCT)
        elif not gestopt:
            cache.set(f"ca_struct_{sid}", "", TTL_STRUCT_FOUT)
    return uit
