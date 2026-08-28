"""Tests voor het laadscherm en de voortgangsbalk.

Het ophalen gebeurt in de request zelf, dus zolang dat loopt krijgt de browser
niets: bij een koude cache keek je seconden tot minuten naar een witte pagina.
Daarom komt er nu eerst een laadscherm terug dat de trage call zelf aftrapt.

Wat hier vastligt is vooral *wanneer* dat scherm verschijnt. Te vaak en je zet
er een extra klik en een flits in voor niets; te weinig en de bezoeker staart
weer naar niets.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from characterassets import assets, esi

in_geheugen = override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})


@in_geheugen
class CacheCheckTest(TestCase):
    """`alles_gecached` beslist of het laadscherm nodig is."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_niets_gecached(self):
        self.assertFalse(esi.alles_gecached([1, 2]))

    def test_alles_gecached(self):
        cache.set("ca_assets_1", [])
        cache.set("ca_assets_2", [])
        self.assertTrue(esi.alles_gecached([1, 2]))

    def test_half_gecached_telt_niet(self):
        """Eén ontbrekend character is genoeg voor een trage ronde."""
        cache.set("ca_assets_1", [])
        self.assertFalse(esi.alles_gecached([1, 2]))

    def test_lege_lijst_hoeft_niet_te_wachten(self):
        """Geen characters, niets op te halen — geen laadscherm."""
        self.assertTrue(esi.alles_gecached([]))

    def test_lege_assetlijst_telt_als_gecached(self):
        """Een character zonder assets heeft een lege lijst in de cache staan.

        Die mag niet als "nog niet opgehaald" gelezen worden, anders krijgt
        iemand met een leeg character elke keer opnieuw het laadscherm.
        """
        cache.set("ca_assets_1", [])
        self.assertTrue(esi.alles_gecached([1]))


class NepCharacter:
    def __init__(self, cid, naam):
        self.character_id = cid
        self.character_name = naam


@in_geheugen
class VoortgangTest(TestCase):
    """De balk moet meelopen met wat er echt af is."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_melden_per_character(self):
        chars = [NepCharacter(1, "Een"), NepCharacter(2, "Twee"), NepCharacter(3, "Drie")]
        gemeld = []

        with patch.object(assets.esi, "met_assets_token", return_value=(chars, [])), \
                patch.object(assets.esi, "assets", return_value=([], "", True)), \
                patch.object(assets.esi, "namen", return_value={}), \
                patch.object(assets.esi, "item_namen", return_value={}), \
                patch.object(assets.esi, "structuur_namen", return_value={}):
            assets.bouw(chars, melden=lambda k, t, n: gemeld.append((k, t, n)))

        # Drie meldingen, oplopend, en de laatste is compleet.
        self.assertEqual([g[0] for g in gemeld], [1, 2, 3])
        self.assertTrue(all(g[1] == 3 for g in gemeld))
        self.assertEqual(gemeld[-1][0], gemeld[-1][1])

    def test_namen_komen_mee(self):
        """De teller zegt bij welk character we zijn, niet welk id."""
        chars = [NepCharacter(7, "Testpiloot")]
        gemeld = []

        with patch.object(assets.esi, "met_assets_token", return_value=(chars, [])), \
                patch.object(assets.esi, "assets", return_value=([], "", True)), \
                patch.object(assets.esi, "namen", return_value={}), \
                patch.object(assets.esi, "item_namen", return_value={}), \
                patch.object(assets.esi, "structuur_namen", return_value={}):
            assets.bouw(chars, melden=lambda k, t, n: gemeld.append(n))

        self.assertEqual(gemeld, ["Testpiloot"])

    def test_zonder_melden_blijft_het_werken(self):
        """De taak en de tests roepen bouw() zonder balk aan."""
        chars = [NepCharacter(1, "Een")]
        with patch.object(assets.esi, "met_assets_token", return_value=(chars, [])), \
                patch.object(assets.esi, "assets", return_value=([], "", True)), \
                patch.object(assets.esi, "namen", return_value={}), \
                patch.object(assets.esi, "item_namen", return_value={}), \
                patch.object(assets.esi, "structuur_namen", return_value={}):
            idx = assets.bouw(chars)

        self.assertEqual(idx.chars, {1: "Een"})

    def test_traag_character_houdt_de_teller_niet_tegen(self):
        """`as_completed` en niet `map`: anders staat de balk stil op de traagste.

        Met `pool.map` komen de resultaten in de volgorde van de invoer binnen.
        Duurt character 1 twintig seconden, dan blijft de teller op 0 staan
        terwijl 2 tot en met 6 allang klaar zijn — precies het moment waarop de
        bezoeker denkt dat het hangt.
        """
        import threading

        chars = [NepCharacter(i, f"Char {i}") for i in range(1, 4)]
        eerste_mag_door = threading.Event()
        gemeld = []

        def traag(cid, ververs=False):
            if cid == 1:
                eerste_mag_door.wait(timeout=5)
            return [], "", True

        def melden(klaar, totaal, naam):
            gemeld.append(naam)
            if len(gemeld) == 2:      # twee anderen zijn al binnen
                eerste_mag_door.set()

        with patch.object(assets.esi, "met_assets_token", return_value=(chars, [])), \
                patch.object(assets.esi, "assets", side_effect=traag), \
                patch.object(assets.esi, "namen", return_value={}), \
                patch.object(assets.esi, "item_namen", return_value={}), \
                patch.object(assets.esi, "structuur_namen", return_value={}):
            assets.bouw(chars, melden=melden)

        self.assertEqual(len(gemeld), 3)
        self.assertNotEqual(gemeld[0], "Char 1")   # de trage was niet de eerste


@in_geheugen
class LaadschermViewTest(TestCase):
    """Krijgt de bezoeker het laadscherm, en daarna zijn echte pagina?"""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

        from allianceauth.authentication.models import CharacterOwnership
        from allianceauth.eveonline.models import EveCharacter
        from django.contrib.auth.models import Permission, User

        self.user = User.objects.create_user("piloot", password="geheim")
        self.user.user_permissions.add(
            Permission.objects.get(codename="basic_access",
                                   content_type__app_label="characterassets"))

        # AA wikkelt de URL's van elke plugin in `main_character_required`.
        # Zonder main character krijg je dus geen 200 maar een 302 naar het
        # dashboard — en dan test je niets van deze plugin.
        char = EveCharacter.objects.create(
            character_id=1, character_name="Testpiloot",
            corporation_id=98000001, corporation_name="Testcorp",
            corporation_ticker="TEST")
        CharacterOwnership.objects.create(character=char, user=self.user,
                                          owner_hash="testhash")
        self.user.profile.main_character = char
        self.user.profile.save()

        self.client.force_login(self.user)
        self.chars = [char]

    def _patches(self):
        return [
            patch.object(esi, "eigen_characters", return_value=self.chars),
            patch.object(assets.esi, "met_assets_token", return_value=(self.chars, [])),
            patch.object(assets.esi, "assets", return_value=([], "", True)),
            patch.object(assets.esi, "namen", return_value={}),
            patch.object(assets.esi, "item_namen", return_value={}),
            patch.object(assets.esi, "structuur_namen", return_value={}),
        ]

    def test_koude_cache_geeft_het_laadscherm(self):
        for p in self._patches():
            p.start()
            self.addCleanup(p.stop)

        r = self.client.get("/characterassets/zoeken/")

        self.assertEqual(r.status_code, 200)
        self.assertIn("characterassets/laden.html", [t.name for t in r.templates])
        # De balk moet er staan, en de knoppen om zelf verder te komen.
        self.assertContains(r, "progress-bar")
        self.assertContains(r, "werk=1")

    def test_warme_cache_gaat_er_recht_doorheen(self):
        cache.set("ca_assets_1", [])
        for p in self._patches():
            p.start()
            self.addCleanup(p.stop)

        r = self.client.get("/characterassets/zoeken/")

        self.assertIn("characterassets/zoeken.html", [t.name for t in r.templates])

    def test_werk_1_doet_het_werk_ook_bij_een_koude_cache(self):
        """Anders stuurt de laadpagina zichzelf in een kringetje."""
        for p in self._patches():
            p.start()
            self.addCleanup(p.stop)

        r = self.client.get("/characterassets/zoeken/?werk=1")

        self.assertIn("characterassets/zoeken.html", [t.name for t in r.templates])

    def test_voortgang_zegt_niet_bezig_als_er_niets_loopt(self):
        r = self.client.get("/characterassets/voortgang/")

        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["bezig"])

    def test_voortgang_wordt_opgeruimd(self):
        """Na afloop geen halve balk laten staan voor het volgende bezoek."""
        for p in self._patches():
            p.start()
            self.addCleanup(p.stop)

        self.client.get("/characterassets/zoeken/?werk=1")

        self.assertFalse(self.client.get("/characterassets/voortgang/").json()["bezig"])

    def test_ververs_stuurt_niet_in_een_kringetje(self):
        """Na het verversen mag het doel geen `ververs=1` meer bevatten.

        Anders vindt die pagina zichzelf opnieuw "moet wachten", toont weer het
        laadscherm, ververst weer — en blijft de bezoeker rondjes draaien
        terwijl ESI elke ronde de volle laag krijgt.
        """
        for p in self._patches():
            p.start()
            self.addCleanup(p.stop)

        r = self.client.get("/characterassets/zoeken/?ververs=1&q=tritanium")

        self.assertIn("characterassets/laden.html", [t.name for t in r.templates])
        self.assertNotIn("ververs", r.context["doel"])
        self.assertIn("q=tritanium", r.context["doel"])      # de zoekterm blijft
        self.assertIn("ververs=1", r.context["werk_url"])    # de werk-call ververst wél
        self.assertIn("werk=1", r.context["werk_url"])

    def test_warme_cache_maar_ververs_wacht_alsnog(self):
        """Verversen duurt sowieso lang; daar hoort het laadscherm ook bij."""
        cache.set("ca_assets_1", [])
        for p in self._patches():
            p.start()
            self.addCleanup(p.stop)

        r = self.client.get("/characterassets/zoeken/?ververs=1")

        self.assertIn("characterassets/laden.html", [t.name for t in r.templates])

    def test_zoekpagina_heeft_de_bezig_balk(self):
        """Ook een warme zoekopdracht is een paginalading; die mag je zien."""
        cache.set("ca_assets_1", [])
        for p in self._patches():
            p.start()
            self.addCleanup(p.stop)

        r = self.client.get("/characterassets/zoeken/?q=tritanium")

        self.assertContains(r, 'id="ca-bezig"')
        self.assertContains(r, "is-onbekend")

    def test_boompagina_heeft_de_bezig_balk_ook(self):
        cache.set("ca_assets_1", [])
        for p in self._patches():
            p.start()
            self.addCleanup(p.stop)

        r = self.client.get("/characterassets/boom/")

        self.assertContains(r, 'id="ca-bezig"')
