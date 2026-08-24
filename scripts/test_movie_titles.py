#!/usr/bin/env python3
"""
Regression tests for generic AMC title handling and notebook patch safety.

Movie names below are test inputs only. Production lookup logic contains no
per-movie mappings or poster overrides.
"""

from __future__ import annotations

import ast
import unittest

from scripts.movie_titles import (
    analyze_movie_title,
    candidate_title_variants,
    canonical_movie_title,
    is_non_movie_title,
    search_terms_for_title,
    wikipedia_title_candidates,
)
from scripts.patch_notebook_titles import patch_notebook


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
