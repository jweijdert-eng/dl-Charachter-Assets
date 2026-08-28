"""Tests voor de ESI-laag — vooral het foutbudget.

Waarom hier zoveel aandacht naar één statuscode gaat: een **420** van ESI is
niet "deze call ging mis" maar "je hebt te veel fouten gemaakt, je krijgt even
niets meer". Wie daarop hetzelfde doet als bij een 503 — nog eens proberen,
iets anders proberen — maakt de straf langer en sleept de rest van Alliance
Auth mee, want dat foutbudget geldt per IP.

De echte 420 kwam hier binnen tijdens een corp-brede zoekopdracht en liet een
halve hangar zien alsof dat alles was. Beide kanten daarvan staan hieronder.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from requests.structures import CaseInsensitiveDict

from characterassets import esi

# De pauze staat in de cache, dus de cache doet hier mee als testonderwerp.
# Wel in het geheugen en niet in Redis: deze tests horen te draaien zonder dat
# er eerst een server aan moet.
in_geheugen = override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})


def nep_antwoord(status, data=None, headers=None):
    """Een minimaal `requests`-antwoord."""

    class Antwoord:
        status_code = status

        def __init__(self):
            self.headers = CaseInsensitiveDict(headers or {})

        def json(self):
            if data is None:
                raise ValueError("geen json")
            return data

    return Antwoord()


@in_geheugen
class FoutbudgetTest(TestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_420_wordt_niet_opnieuw_geprobeerd(self):
        """Een 420 is geen hik: elke nieuwe poging is weer een fout."""
        antwoord = nep_antwoord(420, headers={"X-Esi-Error-Limit-Reset": "42"})
        with patch.object(esi._session, "request", return_value=antwoord) as call:
            data, _, fout = esi._call("GET", "/characters/1/assets/")

        self.assertIsNone(data)
        self.assertEqual(fout, esi.FOUT_LIMIET)
        self.assertEqual(call.call_count, 1)

    def test_420_legt_ook_de_rest_stil(self):
        """De pauze is gedeeld: na één 420 vraagt niemand nog iets."""
        antwoord = nep_antwoord(420, headers={"X-Esi-Error-Limit-Reset": "60"})
        with patch.object(esi._session, "request", return_value=antwoord):
            esi._call("GET", "/universe/names/")

        self.assertGreater(esi._pauze_rest(), esi.MAX_WACHT)

        with patch.object(esi._session, "request") as call:
            data, _, fout = esi._call("GET", "/characters/1/assets/")

        self.assertEqual(fout, esi.FOUT_LIMIET)
        call.assert_not_called()

    def test_bijna_op_is_ook_stoppen(self):
        """Niet tot 0 aftellen — de rest van AA deelt hetzelfde budget."""
        antwoord = nep_antwoord(403, headers={"X-Esi-Error-Limit-Remain": "5",
                                              "X-Esi-Error-Limit-Reset": "30"})
        with patch.object(esi._session, "request", return_value=antwoord):
            esi._call("GET", "/universe/structures/1/", token="t")

        self.assertGreater(esi._pauze_rest(), 0)

    def test_budget_uit_een_geslaagde_call(self):
        """Ook een 200 draagt de teller mee; die mag niet genegeerd worden."""
        antwoord = nep_antwoord(200, data=[], headers={"X-Esi-Error-Limit-Remain": "3",
                                                       "X-Esi-Error-Limit-Reset": "20"})
        with patch.object(esi._session, "request", return_value=antwoord):
            esi._call("GET", "/characters/1/assets/", token="t")

        self.assertGreater(esi._pauze_rest(), 0)

    def test_ruim_budget_pauzeert_niet(self):
        antwoord = nep_antwoord(200, data=[], headers={"X-Esi-Error-Limit-Remain": "97",
                                                       "X-Esi-Error-Limit-Reset": "60"})
        with patch.object(esi._session, "request", return_value=antwoord):
            esi._call("GET", "/characters/1/assets/", token="t")

        self.assertEqual(esi._pauze_rest(), 0)


@in_geheugen
class PaginaTest(TestCase):
    """Een half antwoord mag nooit voor een heel antwoord doorgaan."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_mislukte_pagina_is_onvolledig(self):
        pagina1 = nep_antwoord(200, data=[{"item_id": 1}],
                               headers={"X-Pages": "3"})
        stuk = nep_antwoord(420, headers={"X-Esi-Error-Limit-Reset": "60"})
        with patch.object(esi._session, "request", side_effect=[pagina1, stuk]):
            rijen, _, volledig = esi._paged("/characters/1/assets/", token="t")

        self.assertEqual(len(rijen), 1)
        self.assertFalse(volledig)

    def test_alle_paginas_is_volledig(self):
        pagina1 = nep_antwoord(200, data=[{"item_id": 1}], headers={"X-Pages": "2"})
        pagina2 = nep_antwoord(200, data=[{"item_id": 2}], headers={"X-Pages": "2"})
        with patch.object(esi._session, "request", side_effect=[pagina1, pagina2]):
            rijen, _, volledig = esi._paged("/characters/1/assets/", token="t")

        self.assertEqual(len(rijen), 2)
        self.assertTrue(volledig)

    def test_onvolledig_overschrijft_de_cache_niet(self):
        """Een mislukte verversing laat staan wat er stond.

        Anders is één 420 erger dan niets doen: cache leeg, pagina leeg, en de
        volgende bezoeker begint meteen weer te trekken.
        """
        cache.set("ca_assets_7", [{"item_id": 99}], 3600)
        cache.set("ca_assets_vol_7", True, 3600)

        with patch.object(esi, "token_for", return_value="t"), \
                patch.object(esi, "_paged", return_value=([], {}, False)):
            rijen, _, volledig = esi.assets(7, ververs=True)

        self.assertEqual(rijen, [{"item_id": 99}])
        self.assertTrue(volledig)
        self.assertEqual(cache.get("ca_assets_7"), [{"item_id": 99}])

    def test_half_antwoord_wordt_kort_bewaard(self):
        with patch.object(esi, "token_for", return_value="t"), \
                patch.object(esi, "_paged",
                             return_value=([{"item_id": 1}], {}, False)):
            _, _, volledig = esi.assets(8)

        self.assertFalse(volledig)
        self.assertFalse(cache.get("ca_assets_vol_8"))


@in_geheugen
class StructuurNamenTest(TestCase):
    """De grootste foutenfabriek van de plugin, voor de duidelijkheid."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_eigenaar_wordt_het_eerst_gevraagd(self):
        """Wie er spullen heeft liggen, mag er docken. Eén call, geen 403's."""
        gevraagd = []

        def nep_get(path, token=None, params=None):
            gevraagd.append(token)
            return ({"name": "1DQ1-A - Home"}, None)

        with patch.object(esi, "_tokens_per_character",
                          return_value={1: "token-1", 2: "token-2", 3: "token-3"}), \
                patch.object(esi, "_get_of_fout", side_effect=nep_get):
            uit = esi.structuur_namen([1_035_466_617_946], [1, 2, 3],
                                      {1_035_466_617_946: {3}})

        self.assertEqual(uit[1_035_466_617_946], "1DQ1-A - Home")
        self.assertEqual(gevraagd, ["token-3"])

    def test_hoogstens_een_paar_tokens_per_structuur(self):
        """Zonder deze rem is twintig characters maal dertig structuren een 420."""
        pogingen = []

        def nep_get(path, token=None, params=None):
            pogingen.append(token)
            return (None, esi.FOUT_CALL)      # 403: mag er niet docken

        tokens = {i: f"token-{i}" for i in range(1, 21)}
        with patch.object(esi, "_tokens_per_character", return_value=tokens), \
                patch.object(esi, "_get_of_fout", side_effect=nep_get):
            uit = esi.structuur_namen([1_035_466_617_946], list(tokens))

        self.assertEqual(uit[1_035_466_617_946], "")
        self.assertEqual(len(pogingen), esi.MAX_TOKENS_PER_STRUCTUUR)

    def test_foutlimiet_stopt_de_hele_lus(self):
        """Bij een 420 niet doorlopen, en de leegte ook niet een uur cachen."""
        pogingen = []

        def nep_get(path, token=None, params=None):
            pogingen.append(path)
            return (None, esi.FOUT_LIMIET)

        with patch.object(esi, "_tokens_per_character", return_value={1: "token-1"}), \
                patch.object(esi, "_get_of_fout", side_effect=nep_get):
            esi.structuur_namen([1_035_466_617_946, 1_035_466_617_947], [1],
                                None)

        self.assertEqual(len(pogingen), 2)     # één poging per structuur
        self.assertIsNone(cache.get("ca_struct_1035466617946"))

    def test_zonder_scope_geen_enkele_call(self):
        """Niemand met de structures-scope? Dan is elke call een gratis fout."""
        with patch.object(esi, "_tokens_per_character", return_value={}), \
                patch.object(esi, "_get_of_fout") as call:
            uit = esi.structuur_namen([1_035_466_617_946], [1, 2])

        call.assert_not_called()
        self.assertEqual(uit[1_035_466_617_946], "")


@in_geheugen
class NamenTest(TestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_foutlimiet_splitst_de_batch_niet_op(self):
        """Binair splitsen hoort bij een 400 (rot id), niet bij een 420.

        Bij een dichte deur is elke helft weer een bons: duizend ids worden dan
        twintig extra fouten bovenop een budget dat al op is.
        """
        with patch.object(esi, "_post_of_fout",
                          return_value=(None, esi.FOUT_LIMIET)) as call:
            uit = esi._namen_blok([1, 2, 3, 4])

        self.assertEqual(uit, {})
        self.assertEqual(call.call_count, 1)

    def test_rot_id_wordt_er_wel_uitgesplitst(self):
        """Eén onbekend id mag geen duizend goede namen meeslepen."""
        def nep_post(path, body, token=None):
            if 666 in body:
                return None, esi.FOUT_CALL
            return [{"id": i, "name": f"Naam {i}"} for i in body], None

        with patch.object(esi, "_post_of_fout", side_effect=nep_post):
            uit = esi._namen_blok([1, 2, 666, 4])

        self.assertEqual(uit, {1: "Naam 1", 2: "Naam 2", 4: "Naam 4"})
