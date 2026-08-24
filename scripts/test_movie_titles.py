#!/usr/bin/env python3
"""
Regression tests for generic AMC title handling.

Movie names appear here ONLY as test inputs. Production code contains no
per-movie lookup table or poster override.
"""

from __future__ import annotations

import unittest

from scripts.movie_titles import (
    analyze_movie_title,
    candidate_title_variants,
    canonical_movie_title,
    is_non_movie_title,
    search_terms_for_title,
    wikipedia_title_candidates,
)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
