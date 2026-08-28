"""De assetboom — Character Assets.

ESI geeft assets als een **platte lijst**: elk item met een `location_id`, een
`location_type` en een `location_flag`. Het addertje is dat die locatie meestal
geen station is maar een ander item van jezelf — een kist, een schip, een kist
in een schip. Bij één character bleek van 3326 assets er 3229 in zoiets te
liggen. Wie de keten niet omhoog loopt, ziet dus vrijwel niets op een station
liggen en filtert met een locatiekeuze alles weg.

Deze module doet dat werk één keer per zoekopdracht:

1. haal de rijen van alle gekozen characters op (parallel, uit de cache);
2. bouw een index item_id -> rij en ouder -> kinderen;
3. loop per rij de keten omhoog naar de **wortel** (station, structuur of
   systeem) en onthoud onderweg welke vlaggen je passeerde;
4. los namen op: types, zelfgegeven scheeps- en containernamen, locaties.

Stap 3 is gememoiseerd op ouder-id. Zonder dat is het werk kwadratisch in het
aantal items; met memo is het lineair, want ketens worden gedeeld.
"""

import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from django.db import connections

from characterassets import esi

logger = logging.getLogger(__name__)

# Hoeveel characters we tegelijk bij ESI ophalen. Hoger is niet sneller: ESI
# heeft een foutlimiet en de meeste antwoorden komen toch uit de cache.
PARALLEL = 6

# Zoveel treffers tonen we; daarboven zegt de pagina "verfijn je zoekopdracht".
MAX_TREFFERS = 500
# Zoveel knopen tekenen we in de boom. Een station met tienduizend items leest
# niemand meer, en de pagina wordt er alleen traag van.
MAX_KNOPEN = 4000

# --------------------------------------------------------------------------
# Locatievlaggen
# --------------------------------------------------------------------------
# ESI's location_flag zegt *waar in* iets een item zit. Dat is precies waar het
# hier om draait: dezelfde Bustard kan spullen in de cargo, de Fleet Hangar en de
# Ship Hangar hebben, en dat zijn drie verschillende plekken om te zoeken.

VLAG_LABEL = {
    "Hangar": "Hangar",
    "AssetSafety": "Asset Safety",
    "Deliveries": "Deliveries",
    "CorpDeliveries": "Corp Deliveries",
    "Cargo": "Cargo",
    "DroneBay": "Drone Bay",
    "FleetHangar": "Fleet Hangar",
    "ShipHangar": "Ship Hangar",
    "FighterBay": "Fighter Bay",
    "BoosterBay": "Booster Bay",
    "CorpseBay": "Corpse Bay",
    "FrigateEscapeBayHold": "Frigate Escape Bay",
    "QuantumCoreRoom": "Quantum Core Room",
    "StructureDeedBay": "Structure Deed Bay",
    "InfrastructureHangar": "Infrastructure Hangar",
    "Unlocked": "In container",
    "Locked": "In container (locked)",
    "AutoFit": "In container",
    "Skill": "Skills",
    "Implant": "Implants",
    "Wardrobe": "Wardrobe",
}

# Alles wat aan een schip vastzit. Een item met zo'n vlag zit per definitie in
# een schip, en dat is meteen de betrouwbaarste manier om een schip te
# herkennen zonder de SDE erbij te halen.
SCHIP_VLAG_PATRONEN = (
    re.compile(r"^(HiSlot|MedSlot|LoSlot|RigSlot|SubSystemSlot|ServiceSlot|FighterTube)\d*$"),
    re.compile(r"^Specialized.*Hold$"),
)
SCHIP_VLAGGEN = {
    "Cargo", "DroneBay", "FleetHangar", "ShipHangar", "FighterBay",
    "BoosterBay", "CorpseBay", "FrigateEscapeBayHold", "SpecializedFuelBay",
}

CONTAINER_VLAGGEN = {"Unlocked", "Locked", "AutoFit"}

# De filterknoppen op de zoekpagina. Sleutel -> (label, hoe herken je 'm).
# "keten" betekent: de vlag hoeft niet op het item zelf te zitten, hij mag ook
# ergens boven in de keten zitten. Een stapel ammo in een kist in de Fleet
# Hangar van een Bustard hoort namelijk gewoon bij "Fleet Hangar".
FILTERS = ("hangar", "schip", "shiphangar", "fleethangar", "safety", "container")
FILTER_LABEL = {
    "hangar": "Hangar",
    "schip": "In een schip",
    "shiphangar": "Ship Hangar",
    "fleethangar": "Fleet Hangar",
    "safety": "Asset Safety",
    "container": "In een container",
}

# Woorden die in de zoekbalk als **plek** gelden in plaats van als itemnaam.
# De volgorde is niet vrij: langere zinnen staan boven de korte, zodat "fleet
# hangar" niet als het losse woord "hangar" wordt gelezen.
#
# Dit is bewust raadwerk, en het raden gaat soms mis: van de 1491 typenamen in
# een echte corp bevatten er 7 het woord "container" (Station Container...), 2
# "hangar" en 1 "safety" (de Asset Safety Wrap zelf). Daarom vervangt een plek
# de naamzoektocht nooit maar komt hij eroverheen — zie :func:`zoek_slim`.
PLAATS_WOORDEN = (
    ("fleethangar", ("fleet hangar", "fleethangar", "vlootruim")),
    ("shiphangar", ("ship hangar", "shiphangar", "scheepsruim")),
    ("safety", ("asset safety", "assetsafety", "safety", "veiligheid")),
    ("schip", ("in een schip", "in schepen", "schip", "schepen")),
    ("container", ("in een kist", "container", "kist", "kisten")),
    ("hangar", ("hangar",)),
)


def plek_voorbeelden():
    """De plekwoorden om onder de zoekbalk te tonen.

    Het eerste woord per plek is het duidelijkste; de rest zijn synoniemen die
    wel werken maar niemand hoeft te lezen. Een zoekbalk die stiekem meer kan
    dan hij laat zien is een zoekbalk die niemand zo gebruikt.
    """
    return [varianten[0] for _, varianten in PLAATS_WOORDEN]


def lees_plaats(term):
    """Haal plek-woorden uit een zoekterm.

    Geeft (rest, plaatsen, herkende woorden). "tritanium in fleet hangar" wordt
    ("tritanium in", {"fleethangar"}, ["fleet hangar"]); dat losse "in" valt
    verderop weg als stopwoord — als hele deelstring zou het namelijk niets
    meer vinden, want geen enkel item heet "Tritanium in".
    """
    rest = f" {(term or '').lower()} "
    plaatsen, woorden = set(), []
    for sleutel, varianten in PLAATS_WOORDEN:
        for woord in varianten:
            # Op woordgrens: "kist" mag niet in "kistrand" vallen, en
            # "ship" niet in "Ship Scanner I" — dat laatste staat er daarom
            # ook niet als variant in.
            patroon = rf"(?<![a-z0-9]){re.escape(woord)}(?![a-z0-9])"
            if re.search(patroon, rest):
                plaatsen.add(sleutel)
                woorden.append(woord)
                rest = re.sub(patroon, " ", rest)
                break
    return re.sub(r"\s+", " ", rest).strip(), plaatsen, woorden


def is_schipvlag(vlag):
    """Of deze vlag alleen op een schip voorkomt."""
    if vlag in SCHIP_VLAGGEN:
        return True
    return any(p.match(vlag or "") for p in SCHIP_VLAG_PATRONEN)


def vlag_label(vlag):
    """Leesbare naam voor een location_flag.

    Voor wat niet in de tabel staat maken we er zelf iets van: ESI gooit er
    geregeld een nieuwe vlag in (een nieuw scheepshold, een nieuwe bay), en dan
    is "SpecializedAmmoHold" als "Specialized Ammo Hold" nog altijd beter
    leesbaar dan de kale CamelCase.
    """
    if not vlag:
        return ""
    if vlag in VLAG_LABEL:
        return VLAG_LABEL[vlag]
    return re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)", " ", vlag)


# --------------------------------------------------------------------------
# De index
# --------------------------------------------------------------------------

class Index:
    """Alle assets van een groep characters, klaar om in te zoeken."""

    def __init__(self):
        self.rijen = []             # elke asset-rij, verrijkt
        self.per_item = {}          # item_id -> rij
        self.kinderen = defaultdict(list)   # ouder item_id -> [rij]
        self.wortels = {}           # wortel-id -> {naam, soort, items}
        self.chars = {}             # character_id -> naam
        self.bijgewerkt = ""        # oudste last-modified van ESI
        self.zonder_token = []      # characters waar we niet bij konden


def bouw(characters, ververs=False):
    """Bouw de index voor deze characters."""
    idx = Index()
    if not characters:
        return idx

    met, zonder = esi.met_assets_token(characters)
    idx.zonder_token = [c.character_name for c in zonder]
    idx.chars = {c.character_id: c.character_name for c in met}
    if not met:
        return idx

    ruw = _haal_parallel([c.character_id for c in met], ververs)

    # --- 1. platte rijen, met character erbij -----------------------------
    tijden = []
    for cid, (rijen, bijgewerkt) in ruw.items():
        if bijgewerkt:
            tijden.append(bijgewerkt)
        for a in rijen:
            item_id = a.get("item_id")
            if not item_id:
                continue
            rij = {
                "item_id": item_id,
                "type_id": a.get("type_id"),
                "aantal": int(a.get("quantity") or 0),
                "vlag": a.get("location_flag") or "",
                "loc_id": a.get("location_id"),
                "loc_type": a.get("location_type") or "",
                "singleton": bool(a.get("is_singleton")),
                # ESI zegt zelf of dit een blueprint-kopie is. Dat is de enige
                # manier om een BPC van een BPO te onderscheiden: ze delen
                # hetzelfde type_id en dus dezelfde naam.
                "kopie": bool(a.get("is_blueprint_copy")),
                "character_id": cid,
                "character": idx.chars.get(cid, str(cid)),
            }
            idx.rijen.append(rij)
            idx.per_item[item_id] = rij
    idx.bijgewerkt = min(tijden) if tijden else ""

    for rij in idx.rijen:
        if rij["loc_type"] == "item" and rij["loc_id"] in idx.per_item:
            idx.kinderen[rij["loc_id"]].append(rij)

    # --- 2. namen ---------------------------------------------------------
    typenamen = esi.namen({r["type_id"] for r in idx.rijen if r["type_id"]})
    for rij in idx.rijen:
        rij["type_naam"] = typenamen.get(rij["type_id"]) or f"Type {rij['type_id']}"

    _eigen_namen(idx, ververs)
    _plaatjes(idx, typenamen)

    # --- 3. wat is een schip, wat een container? --------------------------
    soorten = _soorten(idx)

    # --- 4. keten omhoog --------------------------------------------------
    memo = {}
    wortel_items = defaultdict(int)
    for rij in idx.rijen:
        wortel, pad, vlaggen, in_schip = _keten(
            rij["loc_id"], rij["loc_type"], memo, idx, soorten)
        rij["wortel"] = wortel
        rij["pad"] = pad
        rij["ketenvlaggen"] = vlaggen | {rij["vlag"]}
        rij["in_schip"] = in_schip
        rij["vlag_label"] = vlag_label(rij["vlag"])
        rij["soort"] = soorten.get(rij["item_id"], "item")
        rij["groepen"] = _groepen(rij)
        wortel_items[wortel] += 1

    _wortel_namen(idx, wortel_items, [c.character_id for c in met])
    return idx


def _haal_parallel(character_ids, ververs):
    """De ruwe ESI-rijen per character, een paar tegelijk.

    `connections.close_all()` in de worker is geen sierlijk detail maar nodig:
    elke thread opent z'n eigen databaseverbinding voor de tokenopzoeking, en
    die blijven anders open rondslingeren. Op Windows loopt dat uit de hand —
    daar hebben we eerder de ephemeral poorten mee uitgeput.
    """
    uit = {}

    def haal(cid):
        try:
            return cid, esi.assets(cid, ververs)
        except Exception:  # noqa: BLE001 — één character mag de rest niet slopen
            logger.exception("Assets: ophalen mislukt voor %s", cid)
            return cid, ([], "")
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        for cid, resultaat in pool.map(haal, character_ids):
            uit[cid] = resultaat
    return uit


def _eigen_namen(idx, ververs):
    """Zelfgegeven namen van schepen en containers ophalen.

    Alleen voor items waar iets **in** zit. Elke gefitte module is ook een
    singleton, dus alle singletons opvragen zou de call tien keer zo groot
    maken voor namen die niemand ziet.
    """
    per_char = defaultdict(list)
    for ouder_id in idx.kinderen:
        rij = idx.per_item.get(ouder_id)
        if rij and rij["singleton"]:
            per_char[rij["character_id"]].append(ouder_id)

    for rij in idx.rijen:
        rij["eigen_naam"] = ""
        rij["naam"] = rij["type_naam"]

    for cid, item_ids in per_char.items():
        for item_id, naam in esi.item_namen(cid, item_ids, ververs).items():
            rij = idx.per_item.get(item_id)
            if rij:
                rij["eigen_naam"] = naam
                rij["naam"] = f"{rij['type_naam']} \"{naam}\""


# Blueprints hebben op images.evetech.net geen `icon` maar een eigen variant:
# `bp` voor het origineel, `bpc` voor een kopie. Vraag je er toch `icon` op,
# dan komt er een **400** terug en staat er een kapot plaatje op de pagina.
# Reaction Formulas zitten in dezelfde categorie en doen precies hetzelfde.
BLUEPRINT_ACHTERVOEGSELS = (" blueprint", " reaction formula")

# SKINs hebben op de plaatjesserver helemaal niets: elke variant geeft een 404.
# Ze krijgen daarom geen <img> maar een leeg vakje — een gebroken-afbeeldingicoon
# op elke SKIN-regel ziet eruit alsof de plugin stuk is.
SKIN_ACHTERVOEGSEL = " skin"


def _plaatjes(idx, typenamen):
    """Zet per rij de juiste afbeeldingsvariant voor images.evetech.net.

    Drie soorten: blueprints (`bp`), blueprint-kopieën (`bpc`) en de rest
    (`icon`). Vraag je `icon` op voor een blueprint, dan komt er een **400**
    terug en staat er een kapot plaatje op de pagina — dat is precies wat hier
    misging. Lege string betekent: helemaal geen afbeelding tonen.
    """
    bp_types = {tid for tid, naam in typenamen.items()
                if naam.lower().endswith(BLUEPRINT_ACHTERVOEGSELS)}
    # Tweede mening uit eveuniverse (categorie 9 = Blueprint), voor zover het
    # al in de database staat. Net als bij de schepen: puur uit de DB, nooit
    # een ESI-call, want dit draait bij elke zoekopdracht.
    bp_types |= _types_in_categorie(set(typenamen) - bp_types, 9)

    skins = {tid for tid, naam in typenamen.items()
             if naam.lower().endswith(SKIN_ACHTERVOEGSEL)}
    skins |= _types_in_categorie(set(typenamen) - skins - bp_types, 91)

    for rij in idx.rijen:
        if rij["type_id"] in skins:
            rij["plaatje"] = ""
        elif rij["kopie"]:
            rij["plaatje"] = "bpc"
        elif rij["type_id"] in bp_types:
            rij["plaatje"] = "bp"
        else:
            rij["plaatje"] = "icon"


def _types_in_categorie(type_ids, categorie_id):
    """Welke van deze types in die eveuniverse-categorie zitten (DB-only)."""
    if not type_ids:
        return set()
    try:
        from eveuniverse.models import EveType

        return set(EveType.objects
                   .filter(id__in=list(type_ids), eve_group__eve_category_id=categorie_id)
                   .values_list("id", flat=True))
    except Exception:  # noqa: BLE001 — eveuniverse is bijvangst, geen eis
        return set()


def _soorten(idx):
    """{item_id: 'schip'|'container'} voor alles waar iets in zit.

    Een schip herkennen we aan z'n **kinderen**: alleen een schip heeft spullen
    in een Cargo, een Drone Bay of een slot hangen. Dat werkt zonder de SDE en
    zonder een ESI-call per type.

    Blijft over: een leeg schip in een hangar, dat geen kinderen heeft en dus
    ook niet als ouder in deze lijst staat — dat hoeven we niet te weten. En
    een schip waar alleen iets in de cargo zit maar niets gefit is, valt nog
    steeds goed uit, want Cargo is ook een scheepsvlag.

    Als eveuniverse de types al geladen heeft gebruiken we dat als tweede
    mening (categorie 6 = Ship). Puur uit de database, nooit een ESI-call: dat
    zou de eerste zoekopdracht van een grote corp minutenlang laten hangen.
    """
    soorten = {}
    for ouder_id, kinderen in idx.kinderen.items():
        if any(is_schipvlag(k["vlag"]) for k in kinderen):
            soorten[ouder_id] = "schip"
        else:
            soorten[ouder_id] = "container"

    onbekend = {idx.per_item[i]["type_id"]
                for i, s in soorten.items() if s == "container"}
    schip_types = _schip_types(onbekend)
    if schip_types:
        for item_id, soort in soorten.items():
            if soort == "container" and idx.per_item[item_id]["type_id"] in schip_types:
                soorten[item_id] = "schip"
    return soorten


def _schip_types(type_ids):
    """Welke van deze type-ids een schip zijn, volgens eveuniverse.

    Best-effort: staat django-eveuniverse er niet, of zijn de types nog niet
    geladen, dan komen we hier gewoon met minder terug. De vlaggen hierboven
    doen het echte werk.
    """
    return _types_in_categorie(type_ids, 6)


def _keten(loc_id, loc_type, memo, idx, soorten):
    """(wortel, pad, vlaggen, in_schip) voor een locatie.

    Het pad loopt van boven naar beneden: eerst de buitenste kist, dan wat
    daarin zit. De vlaggenverzameling is de reden dat "Fleet Hangar" ook een
    stapel ammo vindt die in een kist in die Fleet Hangar ligt.
    """
    if loc_type != "item" or loc_id not in idx.per_item:
        return loc_id, [], frozenset(), False
    if loc_id in memo:
        return memo[loc_id]

    # Vooraf invullen: ESI hoort geen kringetje te geven, maar als het toch
    # gebeurt lopen we ons hier niet stuk op oneindige recursie.
    memo[loc_id] = (loc_id, [], frozenset(), False)

    ouder = idx.per_item[loc_id]
    wortel, pad, vlaggen, in_schip = _keten(
        ouder["loc_id"], ouder["loc_type"], memo, idx, soorten)
    soort = soorten.get(loc_id, "container")
    knoop = {
        "item_id": loc_id,
        "naam": ouder.get("naam") or ouder.get("type_naam") or f"Item {loc_id}",
        "soort": soort,
        "vlag_label": vlag_label(ouder["vlag"]),
    }
    uit = (wortel, pad + [knoop], vlaggen | {ouder["vlag"]},
           in_schip or soort == "schip")
    memo[loc_id] = uit
    return uit


def _groepen(rij):
    """De filtergroepen waar deze rij in valt (kan er meer dan één zijn)."""
    vlaggen = rij["ketenvlaggen"]
    uit = set()
    if "Hangar" in vlaggen:
        uit.add("hangar")
    if "AssetSafety" in vlaggen:
        uit.add("safety")
    if "ShipHangar" in vlaggen:
        uit.add("shiphangar")
    if "FleetHangar" in vlaggen:
        uit.add("fleethangar")
    if rij["in_schip"] or any(is_schipvlag(v) for v in vlaggen):
        uit.add("schip")
    if vlaggen & CONTAINER_VLAGGEN:
        uit.add("container")
    return uit


def _wortel_namen(idx, wortel_items, character_ids):
    """Namen van de plekken waar de ketens uitkomen.

    Drie soorten wortels, en ze komen alle drie ergens anders vandaan:
    NPC-stations en systemen lossen publiek op via /universe/names/,
    spelersstructuren alleen met een token van iemand die er mag docken.
    """
    stations, structuren = [], []
    for wid in wortel_items:
        if not wid:
            continue
        # Upwell-structuren zitten in de hoge id-reeks; stations, systemen en
        # customs offices eronder.
        (structuren if wid >= 1_000_000_000_000 else stations).append(wid)

    namen = esi.namen(stations)
    namen.update(esi.structuur_namen(structuren, character_ids))

    for wid, aantal in wortel_items.items():
        naam = namen.get(wid) or ""
        if not naam:
            naam = f"Onbekende locatie {wid}" if wid else "Onbekend"
        idx.wortels[wid] = {
            "id": wid,
            "naam": naam,
            "soort": "structuur" if wid and wid >= 1_000_000_000_000 else "station",
            "items": aantal,
        }


# --------------------------------------------------------------------------
# Zoeken
# --------------------------------------------------------------------------

# Woorden die je typt om een zin te maken, niet om iets te vinden. Ze blijven
# over zodra er een plek uit de term gehaald is ("tritanium in fleet hangar"),
# en als deelstring vinden ze dan niets meer.
STOPWOORDEN = {"in", "op", "de", "het", "een", "van", "mijn", "m'n", "zit", "ligt", "liggen"}


def _zoekwoorden(term):
    """Een zoekterm als losse woorden, stopwoorden eruit."""
    return [w for w in re.split(r"\s+", (term or "").strip().lower())
            if w and w not in STOPWOORDEN]


def _matcht(rij, woorden):
    """Of alle zoekwoorden ergens in de naam van dit item voorkomen.

    Woord voor woord in plaats van als één deelstring: "compact large" vindt
    zo ook een *'Concussion' Compact Large Graviton Smartbomb*, waar de twee
    woorden niet aan elkaar vast staan. De zelfgegeven naam telt mee, zodat je
    op "rommel" die kist van jezelf terugvindt.
    """
    if not woorden:
        return True
    tekst = f"{rij['type_naam']} {rij['eigen_naam']}".lower()
    return all(w in tekst for w in woorden)


def zoek(idx, term, filters=None, character_id=None, wortel=None, limiet=MAX_TREFFERS):
    """Treffers voor een zoekterm, met hun volledige locatiepad."""
    samen, stapels = _treffers(idx, term, set(filters or ()), character_id, wortel)
    treffers, aantal = _sorteer(samen, limiet)
    return treffers, aantal, stapels


def _treffers(idx, term, filters, character_id=None, wortel=None):
    """{sleutel: treffer} plus het aantal losse stapels dat erin zit.

    Zoekt op de typenaam en op de naam die de speler zelf aan een schip of
    container gaf — "waar ligt mijn *Ammo Hangar*" moet die kist vinden, ook al
    heet het type gewoon Station Container.

    Gelijke stapels op dezelfde plek worden opgeteld: ESI knipt een voorraad
    soms in losse stacks, en dan wil je één regel van 4,2 miljoen Tritanium
    zien in plaats van zes regels die je zelf moet optellen.
    """
    woorden = _zoekwoorden(term)

    samen = {}
    totaal = 0
    for rij in idx.rijen:
        if character_id and rij["character_id"] != character_id:
            continue
        if wortel is not None and rij["wortel"] != wortel:
            continue
        if filters and not (filters & rij["groepen"]):
            continue
        if not _matcht(rij, woorden):
            continue

        totaal += 1
        # De kopie hoort in de sleutel: een BPO en een BPC delen hetzelfde
        # type_id en dus dezelfde naam, maar het zijn twee verschillende dingen
        # om op één regel bij elkaar op te tellen.
        sleutel = (rij["character_id"], rij["loc_id"], rij["type_id"], rij["vlag"],
                   rij["kopie"])
        hit = samen.get(sleutel)
        if hit:
            hit["aantal"] += rij["aantal"]
            continue
        samen[sleutel] = {
            "type_id": rij["type_id"],
            "plaatje": rij["plaatje"],
            "kopie": rij["kopie"],
            "naam": rij["naam"],
            "type_naam": rij["type_naam"],
            "aantal": rij["aantal"],
            "character": rij["character"],
            "character_id": rij["character_id"],
            "vlag_label": rij["vlag_label"],
            "in_schip": rij["in_schip"],
            "pad": rij["pad"],
            "wortel": idx.wortels.get(rij["wortel"], {"naam": "Onbekend"}),
            "groepen": rij["groepen"],
        }

    return samen, totaal


def _sorteer(samen, limiet):
    treffers = sorted(samen.values(),
                      key=lambda h: (h["type_naam"].lower(), -h["aantal"]))
    return treffers[:limiet], len(treffers)


def zoek_slim(idx, term, character_id=None, limiet=MAX_TREFFERS):
    """Zoeken waarbij een plek in de zoekterm ook als plek telt.

    "fleet hangar" hoort te doen wat je verwacht zonder dat er een rij knoppen
    onder de zoekbalk hoeft te staan. Het addertje is dat plekwoorden ook in
    itemnamen voorkomen: er bestaat een *Station Container*, en Asset Safety
    levert een *Asset Safety Wrap* op.

    Daarom **vervangt** de plek de naamzoektocht nooit maar komt hij eroverheen:
    je krijgt alles op die plek én alles waar die tekst in de naam zit. Je
    verliest dus nooit een treffer door hoe wij een woord uitleggen. Wie alleen
    de naam wil, zet de term tussen aanhalingstekens.

    Geeft (treffers, aantal, stapels, uitleg) — die uitleg vertelt de pagina wat
    er met de zoekterm gebeurd is, want stilzwijgend iets anders doen dan de
    gebruiker typte is het ergste van twee werelden.
    """
    ruw = (term or "").strip()

    if len(ruw) >= 2 and ruw[0] == '"' and ruw[-1] == '"':
        letterlijk = ruw[1:-1].strip()
        samen, stapels = _treffers(idx, letterlijk, set(), character_id)
        treffers, aantal = _sorteer(samen, limiet)
        return treffers, aantal, stapels, {"letterlijk": letterlijk}

    op_naam, stapels = _treffers(idx, ruw, set(), character_id)
    rest, plaatsen, woorden = lees_plaats(ruw)
    if not plaatsen:
        treffers, aantal = _sorteer(op_naam, limiet)
        return treffers, aantal, stapels, {}

    in_plaats, plaats_stapels = _treffers(idx, rest, plaatsen, character_id)
    samen = dict(in_plaats)
    extra = 0
    for sleutel, hit in op_naam.items():
        if sleutel not in samen:
            samen[sleutel] = hit
            extra += 1

    treffers, aantal = _sorteer(samen, limiet)
    return treffers, aantal, stapels + plaats_stapels, {
        "plaatsen": [FILTER_LABEL[p] for p in sorted(plaatsen)],
        "woorden": woorden,
        # De opgeschoonde rest, niet de ruwe: "smartbomb in" tonen terwijl we
        # intern op "smartbomb" zoeken, is de gebruiker iets anders vertellen
        # dan we doen.
        "rest": " ".join(_zoekwoorden(rest)),
        "in_plaats": len(in_plaats),
        "op_naam": extra,
    }


# --------------------------------------------------------------------------
# Boom
# --------------------------------------------------------------------------

def boom(idx, wortel_id, term=""):
    """De inhoud van één locatie als uitklapbare boom.

    Per locatie eerst een laag met de vlaggen (Hangar, Ship Hangar, Cargo...),
    daaronder de items. Die tussenlaag is geen opsmuk: zonder dat staat de
    inhoud van de cargo van een Bustard op één hoop met wat er in de Fleet Hangar
    ligt, en dat zijn in het spel twee losse ruimtes.
    """
    woorden = _zoekwoorden(term)
    teller = {"n": 0}

    directe = [r for r in idx.rijen
               if r["wortel"] == wortel_id
               and not (r["loc_type"] == "item" and r["loc_id"] in idx.per_item)]
    knopen = _vlaglagen(idx, directe, woorden, teller)
    return knopen, teller["n"] >= MAX_KNOPEN


def _vlaglagen(idx, rijen, woorden, teller):
    """Groepeer rijen op vlag en bouw daaronder de knopen."""
    per_vlag = defaultdict(list)
    for rij in rijen:
        per_vlag[rij["vlag"]].append(rij)

    uit = []
    for vlag, groep in sorted(per_vlag.items(), key=lambda kv: _vlag_volgorde(kv[0])):
        kinderen = [k for k in (_knoop(idx, rij, woorden, teller) for rij in
                                sorted(groep, key=lambda r: r["naam"].lower()))
                    if k]
        if not kinderen:
            continue
        uit.append({
            "label": vlag_label(vlag) or "Overig",
            "soort": "vlag",
            "vlag": vlag,
            "kinderen": _voeg_stapels_samen(kinderen),
            # Een vlaglaag is een plek, geen ding: een "x1139889" ernaast leest
            # als een aantal exemplaren van de hangar zelf. Alleen het totaal
            # eronder zeggen is duidelijker.
            "aantal": 0,
            "totaal": sum(k["totaal"] for k in kinderen),
        })
    return uit


def _voeg_stapels_samen(knopen):
    """Tel losse stapels van hetzelfde spul op dezelfde plek bij elkaar op.

    ESI knipt een voorraad soms in losse stacks — twee keer "AIR Repairer
    Booster III x1" onder elkaar in dezelfde hangar. Alleen kale items met een
    naamloos type gaan samen: een schip of container heeft een eigen naam en
    een eigen inhoud, en die mag je nooit optellen.
    """
    samen, uit = {}, []
    for k in knopen:
        if k["kinderen"] or k["eigen_naam"]:
            uit.append(k)
            continue
        # Ook hier de kopie erbij: een BPO en een BPC heten hetzelfde.
        sleutel = (k["type_id"], k["character"], k["plaatje"])
        eerder = samen.get(sleutel)
        if eerder:
            eerder["aantal"] += k["aantal"]
            eerder["totaal"] += k["totaal"]
            continue
        samen[sleutel] = k
        uit.append(k)
    return uit


def _vlag_volgorde(vlag):
    """Hangar bovenaan, dan de scheepsruimtes, dan de rest."""
    vaste = ["Hangar", "AssetSafety", "Deliveries", "ShipHangar", "FleetHangar",
             "Cargo", "DroneBay"]
    return (vaste.index(vlag), "") if vlag in vaste else (len(vaste), vlag)


def _knoop(idx, rij, woorden, teller):
    """Eén item in de boom, met z'n eigen inhoud eronder.

    Een tak blijft staan als hij zelf matcht **of** als er dieper iets in zit
    dat matcht. Anders zou filteren op "Nanite" precies de container waar het
    in ligt wegfilteren.
    """
    if teller["n"] >= MAX_KNOPEN:
        return None
    teller["n"] += 1

    kinderen = []
    if idx.kinderen.get(rij["item_id"]):
        kinderen = _vlaglagen(idx, idx.kinderen[rij["item_id"]], woorden, teller)

    zelf_match = _matcht(rij, woorden)
    if not zelf_match and not kinderen:
        teller["n"] -= 1
        return None

    onder = sum(k["totaal"] for k in kinderen)
    return {
        "label": rij["naam"],
        "soort": rij.get("soort") or "item",
        "type_id": rij["type_id"],
        "plaatje": rij["plaatje"],
        "kopie": rij["kopie"],
        "aantal": rij["aantal"],
        "character": rij["character"],
        "eigen_naam": rij["eigen_naam"],
        "kinderen": kinderen,
        "totaal": rij["aantal"] + onder,
        "match": zelf_match and bool(woorden),
    }
