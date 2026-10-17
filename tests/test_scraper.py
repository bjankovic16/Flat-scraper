"""Offline testovi — bez ijednog pravog zahteva ka sajtovima i bez mejla.

Pokretanje: python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scraper  # noqa: E402


def listing(**kwargs) -> scraper.Listing:
    base = dict(source="Halo Oglasi", title="Trosoban stan",
                url="https://www.halooglasi.com/nekretnine/prodaja-stanova/x/123456",
                price_eur=200_000, area_m2=80.0, rooms=3.0, location="Novi Beograd")
    base.update(kwargs)
    return scraper.Listing(**base)


class FakeResponse:
    def __init__(self, status_code: int, text: str = "", headers: dict = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class FloorFilterTest(unittest.TestCase):
    def test_first_and_fifth_floor_pass(self):
        for floor in (1, 5):
            self.assertTrue(
                scraper.passes_filters(listing(floor=floor, total_floors=8)),
                f"{floor}. sprat treba da prođe")

    def test_ground_floor_and_sixth_floor_rejected(self):
        for floor in (0, 6):
            self.assertFalse(
                scraper.passes_filters(listing(floor=floor, total_floors=8)),
                f"{floor}. sprat treba da ispadne")

    def test_top_floor_excluded(self):
        self.assertFalse(scraper.passes_filters(listing(floor=5, total_floors=5)))

    def test_unknown_floor_still_passes(self):
        self.assertTrue(scraper.passes_filters(listing(floor=None,
                                                       total_floors=None)))
        self.assertTrue(scraper.passes_filters(listing(floor=3,
                                                       total_floors=None)))

    def test_email_heading_shows_configured_range(self):
        body = scraper.build_email_body([], [], [], [])
        self.assertIn(f"{scraper.MIN_FLOOR}-{scraper.MAX_FLOOR}. sprat", body)


class FetchFailureTest(unittest.TestCase):
    def setUp(self):
        scraper.reset_fetch_state()
        self.addCleanup(scraper.reset_fetch_state)
        self.sleeps: list[float] = []
        sleep_patch = mock.patch.object(scraper.time, "sleep", self.sleeps.append)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        warm_patch = mock.patch.object(scraper, "_warm_up", lambda url: None)
        warm_patch.start()
        self.addCleanup(warm_patch.stop)

    def test_repeated_denial_stops_calling_host(self):
        calls: list[str] = []

        def fake_get(url):
            calls.append(url)
            return FakeResponse(403)

        with mock.patch.object(scraper, "_get", fake_get):
            for i in range(8):  # osam kategorija, kao u pravom pokretanju
                self.assertIsNone(
                    scraper.fetch(f"https://www.halooglasi.com/k/{i}"))

        self.assertEqual(len(calls), scraper.MAX_DENIALS_PER_HOST)
        self.assertIn("www.halooglasi.com", scraper.GIVEN_UP_HOSTS)
        problems = scraper.FETCH_PROBLEMS["www.halooglasi.com"]
        self.assertEqual(problems["HTTP 403"], scraper.MAX_DENIALS_PER_HOST)
        self.assertTrue(any(r.startswith("preskočeno") for r in problems))

    def test_other_host_keeps_working_after_denial(self):
        def fake_get(url):
            if "halooglasi" in url:
                return FakeResponse(403)
            return FakeResponse(200, "<html><body>ok</body></html>")

        with mock.patch.object(scraper, "_get", fake_get):
            for i in range(5):
                scraper.fetch(f"https://www.halooglasi.com/k/{i}")
            soup = scraper.fetch("https://www.4zida.rs/prodaja-stanova/x")

        self.assertIsNotNone(soup)
        self.assertNotIn("www.4zida.rs", scraper.GIVEN_UP_HOSTS)

    def test_transient_error_is_retried_with_backoff(self):
        responses = [FakeResponse(500), FakeResponse(500),
                     FakeResponse(200, "<html>ok</html>")]

        with mock.patch.object(scraper, "_get", lambda url: responses.pop(0)):
            soup = scraper.fetch("https://www.4zida.rs/prodaja-stanova/x")

        self.assertIsNotNone(soup)
        self.assertEqual(self.sleeps, [scraper.RETRY_BACKOFF_SECONDS,
                                       scraper.RETRY_BACKOFF_SECONDS * 2])
        self.assertNotIn("www.4zida.rs", scraper.GIVEN_UP_HOSTS)

    def test_retry_after_is_respected_when_it_fits_the_budget(self):
        responses = [FakeResponse(429, headers={"Retry-After": "12"}),
                     FakeResponse(200, "<html>ok</html>")]

        with mock.patch.object(scraper, "_get", lambda url: responses.pop(0)):
            soup = scraper.fetch("https://www.4zida.rs/prodaja-stanova/x")

        self.assertIsNotNone(soup)
        self.assertEqual(self.sleeps, [12.0])

    def test_too_long_retry_after_defers_source_instead_of_waiting(self):
        wait = scraper.MAX_RETRY_AFTER_SECONDS + 10
        calls: list[str] = []

        def fake_get(url):
            calls.append(url)
            return FakeResponse(429, headers={"Retry-After": str(wait)})

        with mock.patch.object(scraper, "_get", fake_get):
            self.assertIsNone(scraper.fetch("https://www.4zida.rs/a"))
            self.assertIsNone(scraper.fetch("https://www.4zida.rs/b"))

        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleeps, [])
        self.assertIn("www.4zida.rs", scraper.GIVEN_UP_HOSTS)

    def test_permanent_error_is_not_retried(self):
        calls: list[str] = []

        def fake_get(url):
            calls.append(url)
            return FakeResponse(404)

        with mock.patch.object(scraper, "_get", fake_get):
            self.assertIsNone(scraper.fetch("https://www.4zida.rs/a"))

        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleeps, [])
        self.assertNotIn("www.4zida.rs", scraper.GIVEN_UP_HOSTS)


class WarmUpTest(unittest.TestCase):
    def setUp(self):
        scraper.reset_fetch_state()
        self.addCleanup(scraper.reset_fetch_state)
        sleep_patch = mock.patch.object(scraper.time, "sleep", lambda s: None)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def test_denied_warm_up_counts_without_doubling_requests(self):
        warm_calls: list[str] = []
        page_calls: list[str] = []

        class FakeSession:
            def get(self, url, **kwargs):
                warm_calls.append(url)
                return FakeResponse(403)

        def fake_get(url):
            page_calls.append(url)
            return FakeResponse(403)

        with mock.patch.object(scraper, "ImpersonateSession", FakeSession), \
                mock.patch.object(scraper, "_impersonate_session_for",
                                  lambda host: FakeSession()), \
                mock.patch.object(scraper, "_get", fake_get):
            for i in range(6):
                scraper.fetch(f"https://www.halooglasi.com/k/{i}")

        # Zagrevanje ide jednom po domenu i njegov 403 se vidi u izveštaju.
        self.assertEqual(warm_calls, ["https://www.halooglasi.com/"])
        problems = scraper.FETCH_PROBLEMS["www.halooglasi.com"]
        self.assertEqual(problems["HTTP 403 (zagrevanje)"], 1)
        # Odbijanje na zagrevanju se broji, pa ostaje mesta za još dva zahteva.
        self.assertEqual(len(page_calls), scraper.MAX_DENIALS_PER_HOST - 1)
        self.assertIn("www.halooglasi.com", scraper.GIVEN_UP_HOSTS)


class RunStatusTest(unittest.TestCase):
    def test_status_values(self):
        self.assertEqual(scraper.run_status([]), "complete")
        self.assertEqual(scraper.run_status(["Halo Oglasi"]), "partial")
        self.assertEqual(scraper.run_status(list(scraper.SOURCE_NAMES)), "failed")

    def test_email_reports_incomplete_run(self):
        scraper.reset_fetch_state()
        self.addCleanup(scraper.reset_fetch_state)
        scraper.note_problem("https://www.halooglasi.com/k/1", "HTTP 403")
        scraper.give_up_on_host("www.halooglasi.com",
                                "HTTP 403 x3 — odbijen pristup")
        body = scraper.build_email_body([listing(source="4zida.rs")], [], [],
                                        ["Halo Oglasi"])
        self.assertIn(scraper.STATUS_LABELS["partial"], body)
        self.assertIn("odbijen pristup", body)
        # Rezultati izvora koji jeste odgovorio ostaju u mejlu.
        self.assertIn("NOVI OGLASI (1)", body)


class FavoritesTest(unittest.TestCase):
    def test_missing_favorite_from_failed_source_is_not_counted(self):
        url = "https://www.halooglasi.com/nekretnine/prodaja-stanova/x/123456"
        catalog = {url: {"source": "Halo Oglasi", "missing_runs": 2,
                         "title": "Trosoban stan"}}
        events = scraper.check_favorites(catalog, [url], {}, ["Halo Oglasi"])
        self.assertEqual(events, [])
        self.assertEqual(catalog[url]["missing_runs"], 2)

    def test_missing_favorite_from_working_source_is_counted(self):
        url = "https://www.4zida.rs/prodaja-stanova/x/aaaaaaaaaaaaaaaaaaaaaaaa"
        catalog = {url: {"source": "4zida.rs", "missing_runs": 2,
                         "title": "Trosoban stan"}}
        events = scraper.check_favorites(catalog, [url], {}, ["Halo Oglasi"])
        self.assertEqual(catalog[url]["missing_runs"],
                         scraper.MISSING_RUNS_BEFORE_ALERT)
        self.assertEqual(len(events), 1)
        self.assertIn("NESTAO", events[0])


class KeywordTest(unittest.TestCase):
    KEYWORDS = ["Ivana Ribara", "Ledine", "Blok 45", "prizemlje", "prizemlju"]

    def test_ledine_in_url_is_excluded(self):
        l = listing(
            source="4zida.rs",
            url="https://www.4zida.rs/prodaja-stanova/ledine-novi-beograd-beograd"
                "/trosoban-stan/69a981ef834ca44d040aa059")
        self.assertEqual(scraper.excluded_by_keyword(l, self.KEYWORDS), "Ledine")

    def test_ledine_in_title_is_excluded(self):
        l = listing(title="Stan, Ledine, Dušana Krstića")
        self.assertEqual(scraper.excluded_by_keyword(l, self.KEYWORDS), "Ledine")

    def test_ground_floor_keywords_are_excluded(self):
        l = listing(description="Stan u prizemlju, odlična lokacija")
        self.assertEqual(scraper.excluded_by_keyword(l, self.KEYWORDS), "prizemlju")

    def test_clean_listing_is_kept(self):
        self.assertIsNone(scraper.excluded_by_keyword(
            listing(description="Stan na trećem spratu, Blok 21"), self.KEYWORDS))


if __name__ == "__main__":
    unittest.main()
