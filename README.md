# Character Assets

Alliance Auth-plugin om spullen terug te vinden. Zoekt door de assets van alle
gekoppelde characters heen — inclusief wat er **in** iets anders ligt:

* in de **cargo**, **Drone Bay** of een slot van een schip;
* in de **Ship Hangar** en **Fleet Hangar** van elk schip dat er een heeft
  (Bustard, Porpoise, Rorqual, dread, carrier);
* in een container, ook in een container in een container;
* in **Asset Safety**.

Naast de zoeklijst zit er een boomweergave in: klap een station open en blader
door de hangars, schepen en kisten die daar staan.

## Zoeken op een plek

Er zijn geen filterknoppen; een plek is gewoon een zoekterm. `fleet hangar`,
`ship hangar`, `asset safety`, `hangar`, `schip` en `kist` worden herkend, en
je mag ze met een itemnaam combineren: **`smartbomb in fleet hangar`**.

Dat raden gaat soms mis, want plekwoorden zitten ook in itemnamen — er bestaat
een *Station Container*, en Asset Safety levert een *Asset Safety Wrap* op.
Daarom **vervangt** een plek de naamzoektocht nooit maar komt hij eroverheen:
je krijgt alles op die plek én alles met die tekst in de naam. Je verliest dus
nooit een treffer door hoe de plugin een woord uitlegt, en boven de resultaten
staat wat er met je zoekterm gebeurd is.

Wil je uitsluitend op naam zoeken, zet de term dan tussen aanhalingstekens:
`"asset safety wrap"`. En `?vlag=fleethangar` in de URL filtert hard op één
plek — handig om een link te delen.

## Waarom dit meer is dan een lijstje

ESI geeft assets als een platte lijst waarin de "locatie" van een item meestal
geen station is maar een ánder item van jezelf. Bij één character bleek van
3326 assets er **3229** in een kist of schip te liggen. Wie die keten niet
omhoog loopt, ziet dus vrijwel niets op een station staan.

Deze plugin loopt die keten wel, en onthoudt onderweg welke locatievlaggen hij
passeerde. Daarom vindt het filter *Fleet Hangar* ook een stapel ammo die in
een kist in de Fleet Hangar van een Bustard ligt.

## Installeren

```bash
pip install git+https://github.com/jweijdert-eng/dl-Charachter-Assets.git
```

In `local.py`:

```python
INSTALLED_APPS += ["characterassets"]
```

Daarna:

```bash
python manage.py migrate
python manage.py collectstatic
```

En **herstart de webserver samen met de installatie**. Een menu-item verwijst
via een hash naar een geregistreerde hook; kent het draaiende proces die hook
nog niet terwijl de rij al in de database staat, dan valt het hele AA-menu om.

## Permissies

| Permissie | Wat het geeft |
| --- | --- |
| `characterassets.basic_access` | De eigen characters doorzoeken |
| `characterassets.corp_access` | Ook alle gekoppelde characters van de corp |

## Scopes

De plugin vraagt zelf niets: hij gebruikt de tokens die er al liggen (van
CharLink, Member Audit of een andere plugin).

| Scope | Waarvoor |
| --- | --- |
| `esi-assets.read_assets.v1` | **Nodig.** Zonder dit token doet een character niet mee. |
| `esi-universe.read_structures.v1` | Optioneel. Zonder dit heet een spelersstructuur "Onbekende locatie 1035…". |

Characters zonder assets-token worden bovenaan de pagina genoemd, zodat een
lege zoekopdracht niet als "hij heeft het niet" gelezen wordt terwijl het
"we mogen niet kijken" is.

## Instellingen

Standaard is "de hele corp" de corp van je eigen main. Anders instellen in
`local.py`:

```python
CHARACTERASSETS_CORPORATION_IDS = [98000001, 98000002]
CHARACTERASSETS_ALLIANCE_IDS = [99013537]
```

## Cache warm houden (aanbevolen bij een grote corp)

Zonder dit haalt de eerste bezoeker de assets op en wacht daarop. Met een
handjevol characters is dat een seconde; bij tachtig characters is het een
pagina die lijkt te hangen.

```python
CELERYBEAT_SCHEDULE["characterassets_warm_cache"] = {
    "task": "characterassets.tasks.warm_cache",
    "schedule": 3600,
}
```

Eens per uur is genoeg en ook het maximum dat zin heeft: **ESI ververst z'n
eigen assets-antwoord maar eens per uur.** De knop "Opnieuw ophalen" haalt langs
onze cache heen op, maar kan CCP niet sneller laten zijn.

## Als ESI 420 geeft (error limited)

ESI telt fouten: ongeveer honderd per zestig seconden. Ga je eroverheen, dan
krijg je **420** op *alles* — ook op calls die het prima zouden doen, en ook in
de rest van Alliance Auth, want dat budget geldt per IP en niet per plugin.

Sinds 1.2.0 doet de plugin drie dingen om daar niet in te belanden:

- **De teller wordt bij elk antwoord gelezen**, ook bij foute. Zakt het budget
  onder de twintig, dan legt de plugin zichzelf stil tot de teller weer
  bijgevuld is — gedeeld tussen de webserver, de Celery-worker en alle threads.
- **Structuurnamen vragen we aan de juiste piloot.** Wie er spullen heeft
  liggen mag er docken; die vragen we eerst, en hoogstens nog twee anderen
  daarna. Blind alle tokens langs alle structuren halen is honderden 403's, en
  precies zo maak je dat foutbudget op.
- **Een 420 wordt niet opnieuw geprobeerd.** Dat is geen hik maar een straf, en
  het nog eens proberen maakt de straf langer.

Kwam er tóch niet alles binnen, dan staat dat boven de pagina in plaats van dat
je een halve hangar voor de hele aanziet — en die halve lijst wordt niet een uur
lang als de waarheid bewaard. Een mislukte verversing laat de vorige gegevens
staan; oud maar waar is beter dan leeg.
