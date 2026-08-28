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
eigen assets-antwoord maar eens per uur.** De knop "Opnieuw ophalen" gooit onze
cache weg, maar kan CCP niet sneller laten zijn.
