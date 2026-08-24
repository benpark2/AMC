#!/usr/bin/env python3
"""
Regression tests for generic AMC title handling and notebook patch safety.

Movie names below are test inputs only. Production lookup logic contains no
per-movie mappings or poster overrides.
"""

from __future__ import annotations

import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.movie_titles import (
    analyze_movie_title,
    candidate_title_variants,
    canonical_movie_title,
    is_non_movie_title,
    search_terms_for_title,
    wikipedia_title_candidates,
)
from scripts.patch_notebook_titles import patch_notebook
import scripts.finalize_posters as posters


class MovieTitleTests(unittest.TestCase):
    def test_stacked_anniversary_and_festival_suffixes(self):
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

    def test_sensory_friendly(self):
        self.assertEqual(
            canonical_movie_title(
                "PAW Patrol: The Dino Movie: Sensory Friendly Screening"
            ),
            "PAW Patrol: The Dino Movie",
        )

    def test_qa_suffix(self):
        self.assertEqual(
            canonical_movie_title("Paper Flowers – Special In-Person Q&A"),
            "Paper Flowers",
        )

    def test_program_code(self):
        self.assertEqual(
            canonical_movie_title(
                "Harry Potter And The Order Of The Phoenix (HPD26)"
            ),
            "Harry Potter And The Order Of The Phoenix",
        )

    def test_title_containing_number_is_not_damaged(self):
        title = "40 years of F**kin' Up"
        self.assertEqual(canonical_movie_title(title), title)

    def test_censored_title_gets_generic_search_variant(self):
        variants = candidate_title_variants("40 years of F**kin' Up")
        self.assertIn("40 years of Fkin' Up", variants)
        self.assertEqual(
            search_terms_for_title("40 years of F**kin' Up"),
            "40 years of Fkin' Up",
        )

    def test_private_theatre_rental_filter(self):
        self.assertTrue(is_non_movie_title("\tPrivate Theatre Rental\n"))
        self.assertTrue(is_non_movie_title("Private Theater Rental - 2 Hours"))
        self.assertFalse(is_non_movie_title("Private Life"))

    def test_anniversary_year_inference(self):
        fast = analyze_movie_title(
            "The Fast and the Furious 25th Anniversary",
            reference_year=2026,
        )
        self.assertEqual(fast.inferred_release_year, 2001)

        castle = analyze_movie_title(
            "Castle in the Sky 40th Anniversary - Studio Ghibli Fest 2026",
            reference_year=2026,
        )
        self.assertEqual(castle.inferred_release_year, 1986)

    def test_wikipedia_candidate_uses_inferred_year(self):
        candidates = wikipedia_title_candidates(
            "The Fast and the Furious 25th Anniversary",
            reference_year=2026,
        )
        self.assertEqual(candidates[0], "The Fast and the Furious (2001 film)")

    def test_canonical_variant_first(self):
        variants = candidate_title_variants(
            "Castle in the Sky 40th Anniversary - Studio Ghibli Fest 2026"
        )
        self.assertEqual(variants[0], "Castle in the Sky")

    def test_ampersand_variant_is_generic(self):
        variants = candidate_title_variants("Minions & Monsters")
        self.assertIn("Minions and Monsters", variants)


class NotebookPatcherTests(unittest.TestCase):
    """
    Test the exact failure mode from the first deployment.

    The fixture intentionally includes a decoy
    `target = normalize_title_for_match(title)` OUTSIDE build_imdb_lookup().
    A notebook-wide string replacement can patch the decoy and leave IMDb with
    an undefined variable. The AST-scoped patcher must not do that.
    """

    def _fixture_notebook(self) -> dict:
        source = """from typing import List

def normalize_title_for_match(title: str) -> str:
    return title.lower()

def candidate_title_variants(title: str) -> List[str]:
    return [title]

def unrelated_function(title):
    # Deliberate decoy. The patcher must leave this untouched.
    target = normalize_title_for_match(title)
    return target

class FakeFuzz:
    @staticmethod
    def token_set_ratio(a, b):
        return 100 if a == b else 10

fuzz = FakeFuzz()

def build_imdb_lookup(session, titles):
    title_variants = {
        title: [normalize_title_for_match(v) for v in candidate_title_variants(title)]
        for title in titles
    }
    candidate_map = {
        title: [
            {
                'primaryNorm': normalize_title_for_match(
                    candidate_title_variants(title)[0]
                ),
                'originalNorm': '',
            }
        ]
        for title in titles
    }

    for title in titles:
        target = normalize_title_for_match(title)
        chosen = None
        chosen_key = None

        for cand in candidate_map.get(title, []):
            exact = int(target in {cand['primaryNorm'], cand['originalNorm']})
            fuzz_score = max(
                fuzz.token_set_ratio(cand['primaryNorm'], target) if cand['primaryNorm'] else 0,
                fuzz.token_set_ratio(cand['originalNorm'], target) if cand['originalNorm'] else 0,
            )
            key = (exact, fuzz_score)
            if chosen_key is None or key > chosen_key:
                chosen = cand
                chosen_key = key

    return chosen_key

showtimes = []
df_show = pd.DataFrame(showtimes)
df_show["format_label"] = df_show["format_label"].fillna("")
"""
        return {
            "cells": [
                {
                    "cell_type": "code",
                    "execution_count": None,
                    "metadata": {},
                    "outputs": [],
                    "source": source.splitlines(keepends=True),
                }
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }

    def test_imdb_patch_is_scoped_and_has_no_undefined_targets(self):
        patched = patch_notebook(self._fixture_notebook())
        source = "".join(patched["cells"][0]["source"])

        # Result must still be valid Python.
        ast.parse(source)

        # The previous broken variable must not be introduced anywhere.
        # Check the obsolete variable name as a standalone identifier;
        # "lookup_targets" is the new, correctly local variable.
        tree = ast.parse(source)
        identifiers = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        self.assertNotIn("targets", identifiers)

        # The new local variable is defined right in the IMDb scoring loop.
        self.assertIn(
            "lookup_targets = title_variants.get(title) or [target]",
            source,
        )

        # Decoy outside build_imdb_lookup() remains unchanged.
        self.assertIn(
            "def unrelated_function(title):\n"
            "    # Deliberate decoy. The patcher must leave this untouched.\n"
            "    target = normalize_title_for_match(title)",
            source,
        )

        # Shared generic title normalization and rental filtering are present.
        self.assertIn(
            "from scripts.movie_titles import candidate_title_variants",
            source,
        )
        self.assertIn("is_non_movie_title", source)

        # Execute the patched function through the scoring path. This is the
        # path that raised NameError in the failed GitHub Actions deployment.
        definitions_only = source.split("showtimes = []", 1)[0]
        namespace: dict = {}
        exec(compile(definitions_only, "<patched-notebook-test>", "exec"), namespace)

        chosen_key = namespace["build_imdb_lookup"](
            None,
            ["Example Movie 25th Anniversary"],
        )
        self.assertIsNotNone(chosen_key)
        self.assertEqual(chosen_key[0], 1)  # canonical variant produced exact match

    def test_patcher_refuses_missing_imdb_target_assignment(self):
        notebook = self._fixture_notebook()
        source = "".join(notebook["cells"][0]["source"])
        source = source.replace(
            "        target = normalize_title_for_match(title)\n",
            "        target = title\n",
            1,
        )
        notebook["cells"][0]["source"] = source.splitlines(keepends=True)

        with self.assertRaisesRegex(
            RuntimeError,
            "expected exactly one.*target = normalize_title_for_match",
        ):
            patch_notebook(notebook)


class PosterResolverTests(unittest.TestCase):
    """
    Exercise the actual Wikimedia resolver paths without making network calls.

    Tests use representative API payloads shaped like MediaWiki/Wikidata
    responses. Movie names are regression inputs only; production code remains
    fully generic.
    """

    def setUp(self):
        posters._JSON_CACHE.clear()

    def test_browser_saved_placeholder_is_detected(self):
        self.assertTrue(
            posters._is_placeholder(
                "Weekend%20Movies_files/images-not-found_QI6q.webp"
            )
        )
        self.assertTrue(
            posters._is_placeholder(
                "https://example.test/images-not-found.webp"
            )
        )
        self.assertFalse(
            posters._is_placeholder(
                "https://upload.wikimedia.org/poster.jpg"
            )
        )

    def test_pageimages_requests_allow_nonfree_movie_posters(self):
        """
        Wikipedia movie posters are often non-free fair-use files.
        MediaWiki PageImages defaults to free-only, so pilicense=any is
        required for the poster use case.
        """
        calls = []

        def fake_api(base, **params):
            calls.append((base, params))
            return {"query": {"pages": []}}

        with patch.object(posters, "_api_json", side_effect=fake_api):
            posters._wikipedia_pages_for_titles(["Example Movie (2026 film)"])
            posters._wikipedia_search_pages('"Example Movie" film')

        self.assertEqual(len(calls), 2)
        for base, params in calls:
            self.assertEqual(base, posters.ENWIKI_API)
            self.assertEqual(params.get("prop"), "pageimages|pageprops")
            self.assertEqual(params.get("pilicense"), "any")

    def test_pageimages_exact_candidate_resolves_current_film(self):
        """
        Verify the Action-API exact-title path used for short/current titles.
        """
        api_page = {
            "pageid": 123,
            "title": "Colony (2026 film)",
            "thumbnail": {
                "source": "https://upload.wikimedia.org/colony.jpg"
            },
            "pageprops": {},
        }

        with patch.object(
            posters,
            "_wikipedia_pages_for_titles",
            return_value=[api_page],
        ) as exact_lookup, patch.object(
            posters,
            "_wikipedia_search_pages",
            side_effect=AssertionError(
                "search fallback should not run after an exact poster match"
            ),
        ):
            image, page, source = posters.resolve_poster(
                "Colony",
                reference_year=2026,
            )

        self.assertEqual(image, "https://upload.wikimedia.org/colony.jpg")
        self.assertEqual(page, "Colony (2026 film)")
        self.assertEqual(source, "wikipedia-exact")

        candidate_titles = exact_lookup.call_args.args[0]
        self.assertIn("Colony (2026 film)", candidate_titles)

    def test_wikipedia_search_handles_title_case_differences(self):
        """
        AMC can capitalize every important word while Wikipedia does not.
        Normalized identity matching must still accept the correct article.
        """
        search_page = {
            "pageid": 456,
            "index": 1,
            "title": "Harry Potter and the Goblet of Fire (film)",
            "thumbnail": {
                "source": "https://upload.wikimedia.org/goblet.jpg"
            },
            "pageprops": {},
        }

        with patch.object(
            posters,
            "_wikipedia_pages_for_titles",
            return_value=[],
        ), patch.object(
            posters,
            "_wikipedia_search_pages",
            return_value=[search_page],
        ):
            image, page, source = posters.resolve_poster(
                "Harry Potter And The Goblet Of Fire",
                reference_year=2026,
            )

        self.assertEqual(image, "https://upload.wikimedia.org/goblet.jpg")
        self.assertEqual(
            page,
            "Harry Potter and the Goblet of Fire (film)",
        )
        self.assertEqual(source, "wikipedia-search")

    def test_imdb_id_uses_wikidata_to_bridge_regional_title(self):
        """
        The generated report already knows some IMDb IDs. We use that ID only
        against Wikidata, never by scraping IMDb. This allows an AMC regional
        title to resolve to a differently named English-Wikipedia article.
        """
        entity = {
            "id": "Q1",
            "claims": {
                "P345": [
                    {
                        "mainsnak": {
                            "datavalue": {
                                "value": "tt0241527"
                            }
                        }
                    }
                ]
            },
            "labels": {
                "en": {
                    "language": "en",
                    "value": "Harry Potter and the Philosopher's Stone",
                }
            },
            "aliases": {
                "en": [
                    {
                        "language": "en",
                        "value": "Harry Potter and the Sorcerer's Stone",
                    }
                ]
            },
            "sitelinks": {
                "enwiki": {
                    "site": "enwiki",
                    "title": "Harry Potter and the Philosopher's Stone (film)",
                }
            },
        }
        wiki_page = {
            "pageid": 789,
            "title": "Harry Potter and the Philosopher's Stone (film)",
            "thumbnail": {
                "source": "https://upload.wikimedia.org/stone.jpg"
            },
            "pageprops": {},
        }

        with patch.object(
            posters,
            "_wikidata_qids_for_imdb",
            return_value=["Q1"],
        ), patch.object(
            posters,
            "_wikidata_entities",
            return_value=[entity],
        ), patch.object(
            posters,
            "_wikipedia_pages_for_titles",
            return_value=[wiki_page],
        ), patch.object(
            posters,
            "_image_via_wikipedia_title",
            side_effect=AssertionError(
                "title fallback should not run after exact IMDb/Wikidata match"
            ),
        ):
            image, page, source = posters.resolve_poster(
                "Harry Potter and the Sorcerer’s Stone: 25th Anniversary",
                imdb_id="tt0241527",
                reference_year=2026,
            )

        self.assertEqual(image, "https://upload.wikimedia.org/stone.jpg")
        self.assertEqual(
            page,
            "Harry Potter and the Philosopher's Stone (film)",
        )
        self.assertEqual(source, "wikidata-enwiki")

    def test_wikidata_p18_can_cover_item_without_wikipedia_sitelink(self):
        """
        Newly released films sometimes get a Wikidata item before an enwiki
        article. A P18 image can still be used through Wikimedia Commons.
        """
        entity = {
            "id": "Q999",
            "claims": {
                "P31": [
                    {
                        "mainsnak": {
                            "datavalue": {
                                "value": {
                                    "entity-type": "item",
                                    "id": "Q11424",
                                }
                            }
                        }
                    }
                ],
                "P3383": [
                    {
                        "mainsnak": {
                            "datavalue": {
                                "value": "Example New Film poster.jpg"
                            }
                        }
                    }
                ],
                "P18": [
                    {
                        "mainsnak": {
                            "datavalue": {
                                "value": "Example New Film still.jpg"
                            }
                        }
                    }
                ],
            },
            "labels": {
                "en": {
                    "language": "en",
                    "value": "Example New Film",
                }
            },
            "aliases": {},
            "sitelinks": {},
        }

        with patch.object(
            posters,
            "_wikidata_qids_for_title",
            return_value=["Q999"],
        ), patch.object(
            posters,
            "_wikidata_entities",
            return_value=[entity],
        ):
            image, page, source = posters._image_via_wikidata_title(
                "Example New Film"
            )

        self.assertIsNone(page)
        self.assertEqual(source, "wikidata-commons")
        # Dedicated P3383 film-poster claim must beat the generic P18 still.
        self.assertIn(
            "Special:Redirect/file/Example_New_Film_poster.jpg?width=500",
            image,
        )
        self.assertNotIn("still.jpg", image)

    def test_disambiguation_page_is_rejected(self):
        pages = [
            {
                "pageid": 111,
                "title": "Beyond Belief",
                "thumbnail": {
                    "source": "https://upload.wikimedia.org/wrong.jpg"
                },
                "pageprops": {"disambiguation": ""},
            }
        ]

        image, page = posters._best_wikipedia_page(
            pages,
            "Beyond Belief",
            minimum_similarity=0.88,
        )
        self.assertIsNone(image)
        self.assertIsNone(page)

    def test_wrong_same_name_media_is_not_accepted_by_wikidata_title(self):
        """
        wbsearchentities descriptions are used to avoid book/song/person hits
        when a new film has a generic title.
        """
        payload = {
            "search": [
                {
                    "id": "Q1",
                    "label": "Example",
                    "description": "album by a band",
                },
                {
                    "id": "Q2",
                    "label": "Example",
                    "description": "2026 documentary film",
                },
            ]
        }

        with patch.object(posters, "_api_json", return_value=payload):
            qids = posters._wikidata_qids_for_title("Example")

        self.assertEqual(qids, ["Q2"])

    def test_generated_html_row_extracts_imdb_without_contacting_imdb(self):
        html = """
        <tr data-movie-id="1">
          <td><div class="movie-cell-title">Example Movie</div></td>
          <td><a href="https://www.imdb.com/title/tt1234567/">7.5</a></td>
        </tr>
        """
        soup = posters.BeautifulSoup(html, "html.parser")
        row = soup.select_one("tr")
        self.assertEqual(posters._extract_imdb_id(row), "tt1234567")

    def test_production_resolver_contains_no_regression_movie_names(self):
        """
        Guard the user's key requirement: no title-by-title production fixes.
        """
        source = Path(posters.__file__).read_text(encoding="utf-8")
        regression_titles = (
            "Castle in the Sky",
            "The Fast and the Furious",
            "PAW Patrol",
            "Paper Flowers",
            "Harry Potter",
            "Colony",
            "Idiots",
            "The Last Blossom",
            "Beyond Belief",
            "GHOST: 2 Big To Rig",
            "Legend of the White Dragon",
            "The King of Cannabis",
        )

        for title in regression_titles:
            self.assertNotIn(title, source)



if __name__ == "__main__":
    unittest.main(verbosity=2)
