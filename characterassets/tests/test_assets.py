"""Tests voor de assetboom.

Deze tests draaien op **verzonnen** ESI-rijen, niet op een echte corp. Dat is
hier geen luiheid maar noodzaak: of er op dit moment toevallig iemand een schip
in de Ship Hangar van een ander schip heeft staan, mag niet bepalen of we weten
dat die code werkt.

De Orca hieronder is dus met opzet een schip dat niemand in de corp bezit — hij
staat er juist omdat hij én een Fleet Hangar én een Ship Hangar heeft, en dat
tweede geval komt in de echte data (nu) nergens voor. Kom je deze namen ergens
op een pagina tegen, dan kijk je naar testgegevens en niet naar je eigen
spullen.

De gevallen die ertoe doen zijn allemaal een vorm van "iets zit in iets dat in
iets anders zit":

* een kist in de Fleet Hangar van een schip;
* een schip in de Ship Hangar van een ander schip, met lading in dat schip;
* een gefitte module in een schip dat in Asset Safety zit.
"""

from unittest.mock import patch

from django.test import TestCase

from characterassets import assets


class NepCharacter:
    def __init__(self, character_id, character_name):
        self.character_id = character_id
        self.character_name = character_name


# item_id, type_id, aantal, vlag, loc_id, loc_type, singleton
RIJEN = [
    # Een Orca in de hangar van een station.
    (100, 28606, 1, "Hangar", 60003760, "station", True),
    #   ... met een kist in z'n Fleet Hangar ...
    (101, 17366, 1, "FleetHangar", 100, "item", True),
    #   ... en daarin de ammo die we straks zoeken.
    (102, 12608, 500, "Unlocked", 101, "item", False),
    #   ... plus een fregat in z'n Ship Hangar ...
    (103, 587, 1, "ShipHangar", 100, "item", True),
    #   ... met iets in de cargo van dat fregat.
    (104, 34, 1200, "Cargo", 103, "item", False),

    # Asset Safety: een wrap met daarin een schip met een gefitte module.
    (200, 60, 1, "AssetSafety", 60014683, "station", True),
    (201, 596, 1, "Hangar", 200, "item", True),
    (202, 3634, 1, "HiSlot0", 201, "item", True),

    # Losse stapels van hetzelfde spul op dezelfde plek: horen in de boom als
    # één regel te eindigen.
    (300, 34, 1000, "Hangar", 60003760, "station", False),
    (301, 34, 2000, "Hangar", 60003760, "station", False),
]

NAMEN = {
    28606: "Orca", 17366: "Station Container", 12608: "Scourge Fury Heavy Missile",
    587: "Rifter", 34: "Tritanium", 60: "Asset Safety Wrap", 596: "Impairor",
    3634: "Civilian Gatling Pulse Laser",
    60003760: "Jita IV - Moon 4 - Caldari Navy Assembly Plant",
    60014683: "Gergish IX - Moon 10 - Wiyrkomi Corporation Factory",
}

EIGEN_NAMEN = {100: "Vrachtwagen", 101: "Ammo Hangar 2"}


def nep_assets(character_id, ververs=False):
    rijen = [{
        "item_id": i, "type_id": t, "quantity": q, "location_flag": v,
        "location_id": lid, "location_type": lt, "is_singleton": s,
    } for i, t, q, v, lid, lt, s in RIJEN]
    return rijen, "Fri, 28 Aug 2026 10:00:00 GMT"


class AssetboomTest(TestCase):
    def setUp(self):
        chars = [NepCharacter(1, "Testpiloot")]
        patches = [
            patch.object(assets.esi, "assets", side_effect=nep_assets),
            patch.object(assets.esi, "met_assets_token", return_value=(chars, [])),
            patch.object(assets.esi, "namen",
                         side_effect=lambda ids: {i: NAMEN[i] for i in ids if i in NAMEN}),
            patch.object(assets.esi, "item_namen",
                         side_effect=lambda cid, ids, ververs=False: dict(EIGEN_NAMEN)),
            patch.object(assets.esi, "structuur_namen", return_value={}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.idx = assets.bouw(chars)

    def _hits(self, term="", filters=None):
        treffers, _, _ = assets.zoek(self.idx, term, filters or set())
        return treffers

    # -- de keten ---------------------------------------------------------

    def test_alles_komt_op_een_station_uit(self):
        """Geen enkel item mag 'ergens in een kist' blijven hangen."""
        wortels = {r["wortel"] for r in self.idx.rijen}
        self.assertEqual(wortels, {60003760, 60014683})

    def test_pad_loopt_van_buiten_naar_binnen(self):
        hit = self._hits("Scourge")[0]
        self.assertEqual([k["naam"] for k in hit["pad"]],
                         ['Orca "Vrachtwagen"', 'Station Container "Ammo Hangar 2"'])
        self.assertEqual(hit["wortel"]["naam"], NAMEN[60003760])

    def test_eigen_naam_is_doorzoekbaar(self):
        """Zoeken op de naam die je zelf aan een kist gaf."""
        self.assertTrue(self._hits("ammo hangar"))

    # -- de plekken uit de vraag ------------------------------------------

    def test_fleet_hangar_vindt_ook_wat_in_een_kist_daarin_ligt(self):
        namen = [h["type_naam"] for h in self._hits(filters={"fleethangar"})]
        self.assertIn("Scourge Fury Heavy Missile", namen)
        self.assertIn("Station Container", namen)

    def test_ship_hangar_vindt_het_schip_en_z_n_lading(self):
        hits = self._hits(filters={"shiphangar"})
        namen = [h["type_naam"] for h in hits]
        self.assertIn("Rifter", namen)
        # De Tritanium in de cargo van dat fregat zit twee lagen diep en telt
        # dus alleen mee als de vlaggen van de hele keten meedoen.
        self.assertIn("Tritanium", namen)

    def test_asset_safety_vindt_de_gefitte_module(self):
        namen = [h["type_naam"] for h in self._hits(filters={"safety"})]
        self.assertIn("Civilian Gatling Pulse Laser", namen)

    def test_in_een_schip(self):
        namen = {h["type_naam"] for h in self._hits(filters={"schip"})}
        self.assertIn("Scourge Fury Heavy Missile", namen)   # kist in fleet hangar
        self.assertIn("Civilian Gatling Pulse Laser", namen)  # gefit
        # Wat gewoon in de stationhangar ligt hoort er niet bij.
        los = [h for h in self._hits(filters={"schip"})
               if h["type_naam"] == "Tritanium" and not h["pad"]]
        self.assertEqual(los, [])

    def test_orca_wordt_als_schip_herkend(self):
        """Aan z'n kinderen: alleen een schip heeft een Fleet Hangar."""
        hit = self._hits("Scourge")[0]
        self.assertEqual(hit["pad"][0]["soort"], "schip")
        self.assertEqual(hit["pad"][1]["soort"], "container")

    # -- optellen ---------------------------------------------------------

    def test_losse_stapels_worden_opgeteld(self):
        hangar = [h for h in self._hits("Tritanium") if not h["pad"]]
        self.assertEqual(len(hangar), 1)
        self.assertEqual(hangar[0]["aantal"], 3000)

    def test_boom_toont_de_lagen_per_vlag(self):
        knopen, afgekapt = assets.boom(self.idx, 60003760)
        self.assertFalse(afgekapt)
        self.assertEqual([k["label"] for k in knopen], ["Hangar"])

        orca = next(k for k in knopen[0]["kinderen"] if k["label"].startswith("Orca"))
        self.assertEqual(sorted(k["label"] for k in orca["kinderen"]),
                         ["Fleet Hangar", "Ship Hangar"])
        # Twee losse stapels Tritanium, één regel in de boom.
        trit = [k for k in knopen[0]["kinderen"] if k["label"] == "Tritanium"]
        self.assertEqual(len(trit), 1)
        self.assertEqual(trit[0]["aantal"], 3000)

    def test_boom_filter_houdt_de_tak_naar_een_treffer_heel(self):
        knopen, _ = assets.boom(self.idx, 60003760)
        self.assertTrue(knopen)
        gefilterd, _ = assets.boom(self.idx, 60003760, "scourge")
        orca = gefilterd[0]["kinderen"][0]
        self.assertTrue(orca["label"].startswith("Orca"))
        # Alleen de tak naar de treffer blijft over: de Ship Hangar valt weg.
        self.assertEqual([k["label"] for k in orca["kinderen"]], ["Fleet Hangar"])

    def test_vlaglaag_toont_geen_aantal_van_zichzelf(self):
        """Een hangar is een plek; 'x3000' ernaast leest als 3000 hangars."""
        knopen, _ = assets.boom(self.idx, 60003760)
        self.assertEqual(knopen[0]["aantal"], 0)
        self.assertGreater(knopen[0]["totaal"], 0)
