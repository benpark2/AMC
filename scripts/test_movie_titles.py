#!/usr/bin/env python3
"""Regression tests for AMC title, RT-score, and poster-quality fixes."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import pandas as pd
from bs4 import BeautifulSoup

from scripts.movie_titles import (
    analyze_movie_title,
    candidate_title_variants,
    canonical_movie_title,
    is_non_movie_title,
    wikipedia_title_candidates,
)
from scripts.patch_notebook_titles import patch_notebook
import scripts.finalize_posters as posters


class MovieTitleTests(unittest.TestCase):
    def test_stacked_anniversary_and_festival(self):
        self.assertEqual(
            canonical_movie_title(
                "Castle in the Sky 40th Anniversary - Studio Ghibli Fest 2026"
            ),
            "Castle in the Sky",
        )

    def test_generic_anniversary(self):
        self.assertEqual(
            canonical_movie_title("The Fast and the Furious 25th Anniversary"),
            "The Fast and the Furious",
        )

    def test_accessibility_and_qa(self):
        self.assertEqual(
            canonical_movie_title(
                "PAW Patrol: The Dino Movie: Sensory Friendly Screening"
            ),
            "PAW Patrol: The Dino Movie",
        )
        self.assertEqual(
            canonical_movie_title("Paper Flowers – Special In-Person Q&A"),
            "Paper Flowers",
        )

    def test_composite_early_access_qa(self):
        self.assertEqual(
            canonical_movie_title(
                "Forgotten Island - Early Access Screening with Cast Member Q&A"
            ),
            "Forgotten Island",
        )

    def test_program_code(self):
        self.assertEqual(
            canonical_movie_title(
                "Harry Potter And The Order Of The Phoenix (HPD26)"
            ),
            "Harry Potter And The Order Of The Phoenix",
        )

    def test_real_parenthetical_year_is_preserved_but_analyzed(self):
        info = analyze_movie_title("Batman (1989)", reference_year=2026)
        self.assertEqual(info.canonical_title, "Batman (1989)")
        self.assertEqual(info.explicit_year, 1989)
        self.assertEqual(
            wikipedia_title_candidates("Batman (1989)", reference_year=2026)[0],
            "Batman (1989 film)",
        )

    def test_private_rental_filter(self):
        self.assertTrue(is_non_movie_title("\tPrivate Theatre Rental\n"))
        self.assertTrue(is_non_movie_title("Private Theater Rental - 2 Hours"))
        self.assertFalse(is_non_movie_title("Private Life"))

    def test_anniversary_year_inference(self):
        info = analyze_movie_title(
            "The Fast and the Furious 25th Anniversary",
            reference_year=2026,
        )
        self.assertEqual(info.inferred_release_year, 2001)


class NotebookPatcherTests(unittest.TestCase):
    def _fixture_notebook(self) -> dict:
        source = r'''from typing import List, Optional, Tuple
import re
import pandas as pd
from bs4 import BeautifulSoup

class FakeFuzz:
    @staticmethod
    def token_set_ratio(a, b):
        return 100 if a == b else 10

    @staticmethod
    def ratio(a, b):
        if a == b:
            return 100
        # Enough fidelity for the article-preservation regression: a leading
        # article is similar, but must not be treated as an exact identity.
        return 75 if a.removeprefix("the ") == b or b.removeprefix("the ") == a else 10
fuzz = FakeFuzz()

def normalize_title_for_match(title: str) -> str:
    s = title.lower()
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    s = re.sub(r'\b(the|a|an)\b', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

def candidate_title_variants(title: str) -> List[str]:
    return [title]

def unrelated_function(title):
    target = normalize_title_for_match(title)
    return target

def build_imdb_lookup(session, titles):
    title_variants = {
        title: [normalize_title_for_match(v) for v in candidate_title_variants(title)]
        for title in titles
    }
    candidate_map = {}
    for title in titles:
        base = candidate_title_variants(title)[0]
        candidate_map[title] = [
            {
                'primaryTitle': 'The ' + base,
                'originalTitle': '',
                'primaryNorm': normalize_title_for_match('The ' + base),
                'originalNorm': '',
            },
            {
                'primaryTitle': base,
                'originalTitle': '',
                'primaryNorm': normalize_title_for_match(base),
                'originalNorm': '',
            },
        ]
    for title in titles:
        target = normalize_title_for_match(title)
        chosen_key = None
        chosen_title = None
        for cand in candidate_map.get(title, []):
            exact = int(target in {cand['primaryNorm'], cand['originalNorm']})
            fuzz_score = max(
                fuzz.token_set_ratio(cand['primaryNorm'], target) if cand['primaryNorm'] else 0,
                fuzz.token_set_ratio(cand['originalNorm'], target) if cand['originalNorm'] else 0,
            )
            score_key = (exact, fuzz_score)
            if chosen_key is None or score_key > chosen_key:
                chosen_key = score_key
                chosen_title = cand['primaryTitle']
    return chosen_title, chosen_key

def _int0_100(x):
    if x is None:
        return None
    try:
        n = int(str(x).strip())
    except Exception:
        return None
    return n if 0 <= n <= 100 else None

def rt_parse_scores(decoded_html: str, soup: BeautifulSoup) -> Tuple[Optional[int], Optional[int]]:
    return 1, 2

showtimes = [{'movie_title': 'Example Movie', 'format_label': ''}]
df_show = pd.DataFrame(showtimes)
df_show["format_label"] = df_show["format_label"].fillna("")

df_summary = pd.DataFrame([
    {'movie_title': 'A', 'runtime': '1h 20m', 'rt_critic': 100, 'rt_audience': None},
    {'movie_title': 'B', 'runtime': '2h 30m', 'rt_critic': None, 'rt_audience': 98},
    {'movie_title': 'C', 'runtime': '1h 30m', 'rt_critic': None, 'rt_audience': None},
])
df_display = df_summary.copy()

desired = [
    "movie_title",
    "runtime",
    "rt_critic",
    "rt_audience",
    "imdb_rating",
    "showtimes",
    "rt_url",
    "imdb_url",
]
'''
        return {
            "cells": [{
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": source.splitlines(keepends=True),
            }],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }

    def _patched_source(self) -> str:
        patched = patch_notebook(self._fixture_notebook())
        return "".join(patched["cells"][0]["source"])

    def test_patcher_scopes_imdb_and_adds_rt_changes(self):
        source = self._patched_source()
        ast.parse(source)
        self.assertIn("lookup_literal_targets = [", source)
        self.assertIn("candidate_literal_norms = [", source)
        self.assertIn("fuzz.ratio(candidate_norm, lookup_target)", source)
        self.assertIn('df_display["rt_c/a"]', source)
        self.assertIn('"rt_c/a",', source)
        identifiers = {
            node.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)
        }
        self.assertNotIn("targets", identifiers)

    def test_imdb_final_ranking_preserves_leading_articles(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        chosen_title, chosen_key = ns["build_imdb_lookup"](None, ["Example"])
        self.assertEqual(chosen_title, "Example")
        self.assertEqual(chosen_key[0], 1)

    def test_strict_rt_parser_preserves_score_type(self):
        source = self._patched_source()
        tree = ast.parse(source)
        nodes = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "rt_parse_scores"
        ]
        self.assertEqual(len(nodes), 1)
        module = ast.Module(body=nodes, type_ignores=[])
        ast.fix_missing_locations(module)
        ns = {
            "re": re,
            "BeautifulSoup": BeautifulSoup,
            "Optional": __import__("typing").Optional,
            "Tuple": __import__("typing").Tuple,
            "_int0_100": lambda x: int(x) if x is not None and str(x).isdigit() and 0 <= int(x) <= 100 else None,
        }
        exec(compile(module, "<rt-test>", "exec"), ns)
        parser = ns["rt_parse_scores"]

        # Visible RT scoreboard says the audience percentage is unpublished.
        # A stale hidden audiencescore attribute must not be trusted.
        shaun = (
            "Watchlist Tomatometer Popcornmeter "
            "100% Tomatometer 43 Reviews "
            "Popcornmeter Fewer than 50 Verified Ratings"
        )
        shaun_html = (
            '<score-board audiencescore="100" tomatometerscore="100">'
            f'<div>{shaun}</div>'
            '</score-board>'
        )
        aud, crit = parser(shaun_html, BeautifulSoup(shaun_html, "html.parser"))
        self.assertEqual((aud, crit), (None, 100))

        weight = (
            "Watchlist Tomatometer Popcornmeter "
            "92% Tomatometer 75 Reviews "
            "Popcornmeter Fewer than 50 Verified Ratings"
        )
        weight_html = (
            '<score-board audiencescore="92" tomatometerscore="92">'
            f'<div>{weight}</div>'
            '</score-board>'
        )
        aud, crit = parser(weight_html, BeautifulSoup(weight_html, "html.parser"))
        self.assertEqual((aud, crit), (None, 92))

        hanuman = (
            "Watchlist Tomatometer Popcornmeter "
            "Tomatometer 1 Reviews 98% Popcornmeter 50+ Verified Ratings"
        )
        aud, crit = parser(hanuman, BeautifulSoup(f"<div>{hanuman}</div>", "html.parser"))
        self.assertEqual((aud, crit), (98, None))

    def test_rt_display_always_has_two_slots_when_one_exists(self):
        source = self._patched_source()
        tree = ast.parse(source)
        node = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_rt_pair_display"
        )
        module = ast.Module(body=[node], type_ignores=[])
        ast.fix_missing_locations(module)
        ns = {"pd": pd}
        exec(compile(module, "<rt-display-test>", "exec"), ns)
        fmt = ns["_rt_pair_display"]
        self.assertEqual(fmt(pd.Series({"rt_critic": 100, "rt_audience": None})), "100/-")
        self.assertEqual(fmt(pd.Series({"rt_critic": None, "rt_audience": 98})), "-/98")
        self.assertEqual(fmt(pd.Series({"rt_critic": None, "rt_audience": None})), "")


class PosterValidationTests(unittest.TestCase):
    def setUp(self):
        posters._JSON_CACHE.clear()
        posters._LABEL_CACHE.clear()
        posters._LABEL_CACHE.update({
            "Q1001": "japanese",
            "Q1002": "hindi",
            "Q1003": "english",
            "Q1004": "catalan",
        })

    @staticmethod
    def entity(
        qid: str,
        title: str,
        *,
        description: str = "film",
        year: int | None = None,
        runtime: int | None = None,
        language_qid: str | None = None,
        imdb_id: str | None = None,
    ) -> dict:
        claims: dict = {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q11424"}}}}],
        }
        if year is not None:
            claims["P577"] = [{"mainsnak": {"datavalue": {"value": {"time": f"+{year:04d}-01-01T00:00:00Z"}}}}]
        if runtime is not None:
            claims["P2047"] = [{"mainsnak": {"datavalue": {"value": {"amount": f"+{runtime}", "unit": "http://www.wikidata.org/entity/Q7727"}}}}]
        if language_qid:
            claims["P364"] = [{"mainsnak": {"datavalue": {"value": {"id": language_qid}}}}]
        if imdb_id:
            claims["P345"] = [{"mainsnak": {"datavalue": {"value": imdb_id}}}]
        return {
            "id": qid,
            "claims": claims,
            "labels": {"en": {"value": title}},
            "aliases": {},
            "descriptions": {"en": {"value": description}},
            "sitelinks": {},
        }

    def context(self, **kwargs):
        values = dict(
            display_title="Example",
            canonical_title="Example",
            expected_year=None,
            runtime_min=None,
            spoken_languages=frozenset(),
            relax_runtime=False,
        )
        values.update(kwargs)
        return posters.MovieContext(**values)

    def test_missing_metadata_never_rejects(self):
        entity = self.entity("Q1", "Example")
        self.assertEqual(posters._entity_contradictions(entity, self.context()), [])

    def test_year_veto_only_when_more_than_three_years_off(self):
        ctx = self.context(expected_year=1989)
        near = self.entity("Q1", "Example", year=1992)
        far = self.entity("Q2", "Example", year=1993)
        self.assertNotIn("year", posters._entity_contradictions(near, ctx))
        self.assertIn("year", posters._entity_contradictions(far, ctx))

    def test_runtime_veto_over_twenty_minutes(self):
        ctx = self.context(runtime_min=100)
        near = self.entity("Q1", "Example", runtime=120)
        far = self.entity("Q2", "Example", runtime=121)
        self.assertNotIn("runtime", posters._entity_contradictions(near, ctx))
        self.assertIn("runtime", posters._entity_contradictions(far, ctx))

    def test_event_runtime_is_not_a_veto(self):
        ctx = self.context(runtime_min=160, relax_runtime=True)
        entity = self.entity("Q1", "Example", runtime=109)
        self.assertNotIn("runtime", posters._entity_contradictions(entity, ctx))

    def test_language_conflict_is_a_veto(self):
        ctx = self.context(spoken_languages=frozenset({"japanese"}))
        wrong = self.entity("Q1", "Example", language_qid="Q1002")
        right = self.entity("Q2", "Example", language_qid="Q1001")
        self.assertIn("language", posters._entity_contradictions(wrong, ctx))
        self.assertNotIn("language", posters._entity_contradictions(right, ctx))

    def test_wrong_imdb_candidate_can_be_vetoed_by_context(self):
        ctx = self.context(
            canonical_title="Example",
            runtime_min=124,
            spoken_languages=frozenset({"japanese"}),
        )
        wrong = self.entity(
            "Q1", "Example", year=2016, runtime=137,
            language_qid="Q1002", imdb_id="tt1111111",
        )
        right = self.entity(
            "Q2", "Example", year=1988, runtime=124,
            language_qid="Q1001",
        )
        best, rejected = posters._best_entity_for_context(
            [wrong, right], ctx, "tt1111111"
        )
        self.assertEqual(best["id"], "Q2")
        self.assertTrue(any("Q1:language" in item for item in rejected))

    def test_runtime_and_language_break_ambiguous_title_tie(self):
        ctx = self.context(
            canonical_title="Example",
            runtime_min=97,
            spoken_languages=frozenset({"english"}),
        )
        wrong = self.entity("Q1", "Example", runtime=110, language_qid="Q1004")
        right = self.entity("Q2", "Example", runtime=97, language_qid="Q1003")
        best, _ = posters._best_entity_for_context([wrong, right], ctx, None)
        self.assertEqual(best["id"], "Q2")

    def test_nonfilm_is_vetoed(self):
        entity = self.entity("Q1", "Example")
        entity["claims"]["P31"] = []
        entity["descriptions"]["en"]["value"] = "athlete"
        self.assertIn("not-film", posters._entity_contradictions(entity, self.context()))

    def test_wikipedia_film_disambiguator_with_country_is_same_title(self):
        self.assertEqual(
            posters._normalized_match_text("Runner (2026 American film)"),
            "runner",
        )
        self.assertEqual(
            posters._candidate_identity_score(
                "Runner (2026 American film)",
                "Runner",
            ),
            1.0,
        )
        # Keep this generic too: film-description words inside the trailing
        # Wikipedia disambiguator should not become part of title identity.
        self.assertEqual(
            posters._normalized_match_text("Example (1989 British drama film)"),
            "example",
        )

    def test_wikipedia_search_accepts_country_disambiguator_when_context_matches(self):
        ctx = self.context(
            display_title="Runner",
            canonical_title="Runner",
            runtime_min=97,
        )
        page = {
            "pageid": 123,
            "title": "Runner (2026 American film)",
            "thumbnail": {"source": "https://upload.wikimedia.org/runner.jpg"},
            "pageprops": {"wikibase_item": "Q123"},
        }
        entity = self.entity("Q123", "Runner", year=2026, runtime=98)

        with patch.object(posters, "_wikipedia_pages_for_titles", return_value=[]), \
             patch.object(posters, "_wikipedia_search_pages", return_value=[page]), \
             patch.object(posters, "_wikidata_entities", return_value=[entity]):
            image, page_title, source, rejected = posters._image_via_wikipedia(ctx, 2026)

        self.assertEqual(image, "https://upload.wikimedia.org/runner.jpg")
        self.assertEqual(page_title, "Runner (2026 American film)")
        self.assertEqual(source, "wikipedia-search")
        self.assertEqual(rejected, [])

    def test_english_is_soft_tiebreak_for_ambiguous_title(self):
        ctx = self.context(canonical_title="Example", runtime_min=97)
        foreign_page = {
            "pageid": 1,
            "title": "Example (2026 film)",
            "thumbnail": {"source": "https://upload.wikimedia.org/foreign.jpg"},
            "pageprops": {"wikibase_item": "Q1"},
        }
        english_page = {
            "pageid": 2,
            "title": "Example (2026 American film)",
            "thumbnail": {"source": "https://upload.wikimedia.org/english.jpg"},
            "pageprops": {"wikibase_item": "Q2"},
        }
        foreign = self.entity("Q1", "Example", year=2026, runtime=97, language_qid="Q1004")
        english = self.entity("Q2", "Example", year=2026, runtime=98, language_qid="Q1003")
        by_qid = {"Q1": foreign, "Q2": english}

        with patch.object(posters, "_wikipedia_pages_for_titles", return_value=[foreign_page]), \
             patch.object(posters, "_wikipedia_search_pages", return_value=[english_page]), \
             patch.object(posters, "_wikidata_entities", side_effect=lambda qids: [by_qid[qids[0]]] if qids and qids[0] in by_qid else []):
            image, page_title, source, _ = posters._image_via_wikipedia(ctx, 2026)

        self.assertEqual(image, "https://upload.wikimedia.org/english.jpg")
        self.assertEqual(page_title, "Example (2026 American film)")
        self.assertEqual(source, "wikipedia-search")

    def test_other_language_remains_valid_when_no_english_candidate_exists(self):
        ctx = self.context(canonical_title="Example", runtime_min=97)
        foreign_page = {
            "pageid": 1,
            "title": "Example (2026 film)",
            "thumbnail": {"source": "https://upload.wikimedia.org/foreign.jpg"},
            "pageprops": {"wikibase_item": "Q1"},
        }
        foreign = self.entity("Q1", "Example", year=2026, runtime=97, language_qid="Q1004")
        with patch.object(posters, "_wikipedia_pages_for_titles", return_value=[foreign_page]), \
             patch.object(posters, "_wikipedia_search_pages", return_value=[]), \
             patch.object(posters, "_wikidata_entities", return_value=[foreign]):
            image, page_title, source, _ = posters._image_via_wikipedia(ctx, 2026)
        self.assertEqual(image, "https://upload.wikimedia.org/foreign.jpg")
        self.assertEqual(page_title, "Example (2026 film)")
        self.assertEqual(source, "wikipedia-exact")

    def test_stronger_runtime_evidence_can_override_english_tiebreak(self):
        ctx = self.context(canonical_title="Example", runtime_min=100)
        foreign = self.entity("Q1", "Example", runtime=100, language_qid="Q1004")
        english = self.entity("Q2", "Example", runtime=118, language_qid="Q1003")
        best, _ = posters._best_entity_for_context([english, foreign], ctx, None)
        self.assertEqual(best["id"], "Q1")

    def test_short_titles_are_revalidated_but_long_working_titles_can_be_preserved(self):
        short = self.context(canonical_title="Runner")
        long = self.context(canonical_title="A Very Specific Long Movie Title")
        soup = BeautifulSoup('<img class="movie-poster" src="https://example.test/x.jpg">', 'html.parser')
        self.assertTrue(posters._needs_validation(soup.img, short))
        self.assertFalse(posters._needs_validation(soup.img, long))

    def test_production_files_have_no_regression_title_exceptions(self):
        production = "\n".join(
            Path(path).read_text(encoding="utf-8")
            for path in (
                posters.__file__,
                Path(__file__).with_name("movie_titles.py"),
                Path(__file__).with_name("patch_notebook_titles.py"),
            )
        )
        # These strings may be used as tests here, but not as production rules.
        for title in (
            "Akira", "Runner", "Batman (1989)", "Forgotten Island",
            "Shaun the Sheep", "Hanuman Ansh", "One of Them Days",
            "In the Heights", "Coco",
        ):
            self.assertNotIn(title, production)



class AudienceFocusTests(unittest.TestCase):
    def setUp(self):
        posters._JSON_CACHE.clear()
        posters._LABEL_CACHE.clear()
        posters._LABEL_CACHE.update({
            "Q2001": "hindi",
            "Q2002": "english",
            "Q2003": "spanish",
            "Q2004": "india",
            "Q2005": "united states of america",
            "Q2006": "angel studios",
            "Q2007": "lgbt themes",
            "Q2008": "african-american culture",
            "Q2009": "latino culture",
            "Q2010": "bet studios",
            "Q2011": "codeblack films",
            "Q2012": "pantelion films",
            "Q2013": "telemundo studios",
            "Q2014": "vix",
            "Q2015": "exile content studio",
            "Q2016": "tyler perry studios",
            "Q2017": "macro",
            "Q2018": "mucho mas media",
        })

    @staticmethod
    def context(*, languages=frozenset()):
        return posters.MovieContext(
            display_title="Example",
            canonical_title="Example",
            expected_year=None,
            runtime_min=100,
            spoken_languages=languages,
            relax_runtime=False,
        )

    @staticmethod
    def entity(*, description="film", claims=None):
        base_claims = {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q11424"}}}}],
        }
        if claims:
            base_claims.update(claims)
        return {
            "id": "QMovie",
            "claims": base_claims,
            "labels": {"en": {"value": "Example"}},
            "aliases": {},
            "descriptions": {"en": {"value": description}},
            "sitelinks": {},
        }

    def test_general_when_no_strong_signal_exists(self):
        result = posters.classify_audience_focus(self.context(), self.entity())
        self.assertEqual(result.tags, ("General",))

    def test_amc_hindi_is_indian_without_remote_metadata(self):
        result = posters.classify_audience_focus(
            self.context(languages=frozenset({"hindi"})),
            None,
        )
        self.assertEqual(result.tags, ("Indian",))
        self.assertTrue(any("AMC lists hindi" in reason for reason in result.reasons))

    def test_angel_studios_is_soft_faith_signal_not_political_label(self):
        entity = self.entity(
            claims={
                "P750": [{"mainsnak": {"datavalue": {"value": {"id": "Q2006"}}}}],
            }
        )
        result = posters.classify_audience_focus(self.context(), entity)
        self.assertIn("Faith-oriented", result.tags)
        self.assertFalse(any("polit" in reason.casefold() for reason in result.reasons))

    def test_bet_and_codeblack_are_soft_black_focus_signals(self):
        for qid, expected_name in (("Q2010", "BET Studios"), ("Q2011", "Codeblack Films")):
            with self.subTest(qid=qid):
                entity = self.entity(
                    claims={
                        "P272": [{"mainsnak": {"datavalue": {"value": {"id": qid}}}}],
                    }
                )
                result = posters.classify_audience_focus(self.context(), entity)
                self.assertIn("Black-focused", result.tags)
                self.assertTrue(any(expected_name in reason for reason in result.reasons))

    def test_latino_focused_studios_are_soft_latino_signals(self):
        for qid, expected_name in (
            ("Q2012", "Pantelion Films"),
            ("Q2013", "Telemundo Studios"),
            ("Q2014", "ViX"),
            ("Q2015", "Exile Content Studio"),
        ):
            with self.subTest(qid=qid):
                entity = self.entity(
                    claims={
                        "P750": [{"mainsnak": {"datavalue": {"value": {"id": qid}}}}],
                    }
                )
                result = posters.classify_audience_focus(self.context(), entity)
                self.assertIn("Latino/Hispanic-focused", result.tags)
                self.assertTrue(any(expected_name in reason for reason in result.reasons))

    def test_broad_studios_do_not_trigger_identity_focus_by_themselves(self):
        entity = self.entity(
            claims={
                "P272": [
                    {"mainsnak": {"datavalue": {"value": {"id": "Q2016"}}}},
                    {"mainsnak": {"datavalue": {"value": {"id": "Q2017"}}}},
                    {"mainsnak": {"datavalue": {"value": {"id": "Q2018"}}}},
                ],
            }
        )
        result = posters.classify_audience_focus(self.context(), entity)
        self.assertEqual(result.tags, ("General",))

    def test_explicit_subject_metadata_can_add_identity_focus_tags(self):
        entity = self.entity(
            claims={
                "P921": [
                    {"mainsnak": {"datavalue": {"value": {"id": "Q2007"}}}},
                    {"mainsnak": {"datavalue": {"value": {"id": "Q2008"}}}},
                    {"mainsnak": {"datavalue": {"value": {"id": "Q2009"}}}},
                ],
            }
        )
        result = posters.classify_audience_focus(self.context(), entity)
        self.assertIn("LGBTQ+-focused", result.tags)
        self.assertIn("Black-focused", result.tags)
        self.assertIn("Latino/Hispanic-focused", result.tags)

    def test_spanish_language_does_not_by_itself_infer_latino_identity(self):
        result = posters.classify_audience_focus(
            self.context(languages=frozenset({"spanish"})),
            None,
        )
        self.assertEqual(result.tags, ("Spanish-language",))
        self.assertNotIn("Latino/Hispanic-focused", result.tags)

    def test_wikipedia_african_american_category_can_add_black_focus(self):
        entity = self.entity()
        result = posters.classify_audience_focus(
            self.context(),
            entity,
            supplemental_focus_text="African-American comedy films | American buddy comedy films",
        )
        self.assertIn("Black-focused", result.tags)

    def test_wikipedia_mexican_cultural_context_can_add_latino_focus(self):
        entity = self.entity()
        result = posters.classify_audience_focus(
            self.context(),
            entity,
            supplemental_focus_text=(
                "The concept is inspired by the Mexican holiday Day of the Dead "
                "and was praised for its respect for Mexican culture."
            ),
        )
        self.assertIn("Latino/Hispanic-focused", result.tags)

    def test_generic_mexico_setting_alone_does_not_add_latino_focus(self):
        entity = self.entity()
        result = posters.classify_audience_focus(
            self.context(),
            entity,
            supplemental_focus_text="Films set in Mexico",
        )
        self.assertEqual(result.tags, ("General",))

    def test_cast_demographics_alone_do_not_add_identity_focus(self):
        entity = self.entity()
        result = posters.classify_audience_focus(
            self.context(),
            entity,
            supplemental_focus_text=(
                "The film features an all-Latino principal cast and stars an "
                "African-American actor."
            ),
        )
        self.assertEqual(result.tags, ("General",))

    def test_wikipedia_focus_page_extracts_intro_and_categories(self):
        page = {
            "title": "Example",
            "extract": "A film centered on Mexican culture.",
            "categories": [
                {"title": "Category:African-American comedy films"},
                {"title": "Category:2026 films"},
            ],
        }
        text = posters._page_focus_text(page)
        self.assertIn("mexican culture", text)
        self.assertIn("african-american comedy films", text)
        self.assertNotIn("category:", text)

    def test_country_and_original_language_can_mark_international_market(self):
        entity = self.entity(
            claims={
                "P364": [{"mainsnak": {"datavalue": {"value": {"id": "Q2001"}}}}],
                "P495": [{"mainsnak": {"datavalue": {"value": {"id": "Q2004"}}}}],
            }
        )
        result = posters.classify_audience_focus(self.context(), entity)
        self.assertEqual(result.tags, ("Indian",))

    def test_generated_column_uses_batched_wikipedia_context(self):
        html = (
            '<html><head></head><body><table class="dataframe"><thead><tr>'
            '<th>#</th><th>Movie</th><th>RT_C/A</th><th>IMDb</th><th>Showtimes</th><th>Runtime</th>'
            '</tr></thead><tbody><tr data-movie-id="1"><td>1</td>'
            '<td><div class="movie-cell-title">Example</div>'
            '<img class="movie-poster" data-wikipedia-page="Example (film)" src="x.jpg"></td>'
            '<td>90/95</td><td></td><td>AMC Example<br>• 2026-09-20: 1:00 pm</td>'
            '<td>1h 40m</td></tr></tbody></table></body></html>'
        )
        soup = BeautifulSoup(html, "html.parser")
        entity = self.entity()
        focus_pages = {
            "example (film)": {
                "title": "Example (film)",
                "extract": "An American buddy comedy film.",
                "categories": [{"title": "Category:African-American comedy films"}],
            }
        }
        with patch.object(posters, "_audience_entity_for_context", return_value=entity), \
             patch.object(posters, "_wikipedia_focus_pages", return_value=focus_pages) as fetch:
            self.assertEqual(posters._add_audience_focus_column(soup, 2026), 1)
        fetch.assert_called_once_with(["Example (film)"])
        cell = soup.select_one("td.audience-focus-cell")
        self.assertIsNotNone(cell)
        self.assertIn("Black-focused", cell.get_text(" ", strip=True))

    def test_generated_column_is_appended_and_idempotent(self):
        html = (
            '<html><head></head><body><table class="dataframe"><thead><tr>'
            '<th>#</th><th>Movie</th><th>RT_C/A</th><th>IMDb</th><th>Showtimes</th><th>Runtime</th>'
            '</tr></thead><tbody><tr data-movie-id="1"><td>1</td>'
            '<td><div class="movie-cell-title">Example</div></td><td>90/95</td><td></td>'
            '<td>AMC Example<br>• 2026-09-20: 1:00 pm [Hindi Spoken with English Subtitles]</td>'
            '<td>1h 40m</td></tr></tbody></table></body></html>'
        )
        soup = BeautifulSoup(html, "html.parser")
        with patch.object(posters, "_audience_entity_for_context", return_value=None):
            self.assertEqual(posters._add_audience_focus_column(soup, 2026), 1)
            self.assertEqual(posters._add_audience_focus_column(soup, 2026), 1)
        headers = [x.get_text(" ", strip=True) for x in soup.select("thead th")]
        self.assertEqual(headers.count("Audience Focus"), 1)
        cell = soup.select_one("td.audience-focus-cell")
        self.assertIsNotNone(cell)
        self.assertEqual(cell.get_text(" ", strip=True), "Indian")
        self.assertIn("AMC lists hindi", cell.get("title", ""))

    def test_production_classifier_does_not_inspect_cast_identity(self):
        production = Path(posters.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"P161"', production)


class AttachedOutputContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = Path("/mnt/data/Weekend Movies(20260918-034838).htm")
        if not cls.path.exists():
            raise unittest.SkipTest("Attached report is not mounted")
        cls.soup = BeautifulSoup(cls.path.read_text(encoding="utf-8"), "html.parser")
        cls.rows = {
            row.select_one(".movie-cell-title").get_text(" ", strip=True): row
            for row in cls.soup.select("tr[data-movie-id]")
            if row.select_one(".movie-cell-title")
        }

    def test_ambiguous_rows_supply_useful_context(self):
        row = self.rows["Akira"]
        ctx = posters._row_movie_context(row, "Akira", 2026)
        self.assertEqual(ctx.runtime_min, 124)
        self.assertIn("japanese", ctx.spoken_languages)

        row = self.rows["Runner"]
        ctx = posters._row_movie_context(row, "Runner", 2026)
        self.assertEqual(ctx.runtime_min, 97)

        row = self.rows["Batman (1989)"]
        ctx = posters._row_movie_context(row, "Batman (1989)", 2026)
        self.assertEqual(ctx.expected_year, 1989)

    def test_forgotten_island_event_canonicalizes_and_relaxes_runtime(self):
        title = "Forgotten Island - Early Access Screening with Cast Member Q&A"
        row = self.rows.get(title)
        if row is None:
            self.skipTest("Forgotten Island is not in this attached report")
        ctx = posters._row_movie_context(row, title, 2026)
        self.assertEqual(ctx.canonical_title, "Forgotten Island")
        self.assertEqual(ctx.runtime_min, 160)
        self.assertTrue(ctx.relax_runtime)


if __name__ == "__main__":
    unittest.main(verbosity=2)
