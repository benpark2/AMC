#!/usr/bin/env python3
"""Regression tests for AMC title, RT-score, and poster-quality fixes."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from bs4 import BeautifulSoup

from scripts.movie_titles import (
    analyze_movie_title,
    candidate_title_variants,
    canonical_movie_title,
    is_non_movie_title,
    metadata_release_year_hint,
    wikipedia_title_candidates,
)
from scripts.patch_notebook_titles import (
    AMC_BROWSER_CHILD_SCRIPT,
    patch_notebook,
    patch_postprocess_source,
)
from scripts.postprocess_report import parse_showtimes_blob
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

    def test_special_presentation_titles_use_original_feature_for_metadata(self):
        cases = {
            "Example Feature Q&A with Director Jane Doe/Actor John Roe & Cast": "Example Feature",
            "Example Feature: Encore": "Example Feature",
            "Example Feature The Sing-Along Version": "Example Feature",
            "Example Feature 10th Anniversary Remastered": "Example Feature",
        }
        for display, expected in cases.items():
            with self.subTest(display=display):
                self.assertEqual(canonical_movie_title(display), expected)

    def test_anniversary_metadata_year_hint_targets_original_release(self):
        self.assertEqual(
            metadata_release_year_hint(
                "Example Feature 10th Anniversary Remastered", reference_year=2026
            ),
            2016,
        )

    def test_year_tagged_event_wrapper_is_presentation_not_release_year(self):
        info = analyze_movie_title(
            "Example Classic (2026 Event)",
            reference_year=2026,
        )
        self.assertEqual(info.canonical_title, "Example Classic")
        self.assertEqual(info.event_year, 2026)
        self.assertIsNone(info.explicit_year)
        self.assertIsNone(info.inferred_release_year)
        self.assertIsNone(
            metadata_release_year_hint(
                "Example Classic (2026 Event)",
                reference_year=2026,
            )
        )

    def test_event_year_can_anchor_a_stacked_anniversary(self):
        info = analyze_movie_title(
            "Example Classic 20th Anniversary (2026 Event)",
            reference_year=2025,
        )
        self.assertEqual(info.canonical_title, "Example Classic")
        self.assertEqual(info.event_year, 2026)
        self.assertEqual(info.inferred_release_year, 2006)

    def test_creator_branded_anniversary_adds_underlying_metadata_candidate(self):
        display = "Alex Morgan's The Hidden Garden 20th Anniversary"
        variants = candidate_title_variants(display)
        self.assertEqual(variants[0], "Alex Morgan's The Hidden Garden")
        self.assertIn("The Hidden Garden", variants)
        self.assertIn(
            "The Hidden Garden (2006 film)",
            wikipedia_title_candidates(display, reference_year=2026),
        )
        self.assertEqual(metadata_release_year_hint(display, reference_year=2026), 2006)

    def test_ordinary_possessive_title_is_not_unbranded_without_event_shape(self):
        display = "Alex Morgan's The Hidden Garden"
        self.assertEqual(candidate_title_variants(display), [display, "Alex Morgan’s The Hidden Garden"])

    def test_one_word_possessive_prefix_is_not_treated_as_presenter(self):
        display = "Someone's Story 20th Anniversary"
        variants = candidate_title_variants(display)
        self.assertNotIn("Story", variants)


class NotebookPatcherTests(unittest.TestCase):
    def setUp(self):
        cache_dir = Path("build/amc_combo_cache")
        if cache_dir.exists():
            for child in cache_dir.glob("*"):
                if child.is_file():
                    child.unlink()

    def _fixture_notebook(self) -> dict:
        source = r'''from typing import Dict, List, Optional, Tuple
from datetime import date, datetime, timedelta
import html as html_lib
import json
import re
import requests
import pandas as pd
from bs4 import BeautifulSoup

TIME_RE = re.compile(r"(\b\d{1,2}:\d{2}\s*(?:am|pm)\b)", re.I)
OVERRIDE_SATURDAY = ""

def upcoming_weekend_pacific() -> Tuple[date, date]:
    """Return upcoming Saturday/Sunday in America/Los_Angeles."""
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    except Exception:
        today = date.today()

    if OVERRIDE_SATURDAY:
        sat = datetime.strptime(OVERRIDE_SATURDAY, "%Y-%m-%d").date()
        return sat, sat + timedelta(days=1)

    wd = today.weekday()  # Mon=0 .. Sun=6
    sat = today + timedelta(days=(5 - wd)) if wd <= 5 else today + timedelta(days=6)
    return sat, sat + timedelta(days=1)

def _normalize_space(txt: str) -> str:
    return re.sub(r"\s+", " ", (txt or "")).strip()

def looks_like_title_text(txt: str) -> bool:
    return bool(_normalize_space(txt))

def _iter_json_showtime_rows(obj, d, theatre_name, inherited_title=None):
    return []

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
                'startYear': 2026,
            },
            {
                'primaryTitle': base,
                'originalTitle': '',
                'primaryNorm': normalize_title_for_match(base),
                'originalNorm': '',
                'startYear': 2016,
            },
        ]
    current_year = date.today().year
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
            score_key = (
                exact,
                fuzz_score,
            )
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

RT_BASE = "https://www.rottentomatoes.com"

def rt_slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.casefold()).strip("_")

def fetch_html(session, url, params=None, tries=2):
    return 404, ""

def rt_parse_scores(decoded_html: str, soup: BeautifulSoup) -> Tuple[Optional[int], Optional[int]]:
    return 1, 2

def rt_get_scores(session, title, cache, debug=False):
    # Replaced by the production patcher. Keep a structurally representative
    # fixture so the patcher must continue to match the notebook function.
    return None, None, None

def extract_showtimes_from_json_scripts(html_txt: str, theatre_name: str, d: date) -> List[dict]:
    return []

def fetch_amc_html(session, url, params=None):
    return 200, ""

def fetch_html_with_browser(url, params=None, timeout_ms=30000):
    return 200, ""

def _collect_movie_blocks(soup):
    return []

def _iter_block_tags(block, stop_tag):
    return []

def _likely_showtime_tag(el, txt):
    return False

def _extract_local_format_near_tag(el):
    return None

def is_a_list_excluded_near_tag(el):
    return False

AMC_RUNTIME_RE = re.compile(r"(\d+)\s*hr?\s*(\d+)\s*min|(\d+)\s*min", re.I)

def scrape_amc_showtimes_for_date(session, theatre_name, showtimes_url, d):
    return []

THEATRES = [
    {'name': 'AMC Tustin 14 @ The District'},
    {'name': 'AMC Woodbridge 5'},
    {'name': 'AMC Orange 30'},
]
dates = [date(2026, 9, 26), date(2026, 9, 27)]
_fixture_theatres = [
    'AMC Tustin 14 @ The District',
    'AMC Woodbridge 5',
    'AMC Orange 30',
]
showtimes = [
    {
        'movie_title': f'Example Movie {i}',
        'format_label': '',
        'theatre': _fixture_theatres[i % 3],
        'show_date': '2026-09-26' if (i // 3) % 2 == 0 else '2026-09-27',
    }
    for i in range(60)
]
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
        self.assertIn("next two weekend calendar days after today", source)
        self.assertIn("cursor = today + timedelta(days=1)", source)
        self.assertIn("metadata_release_year_hint", source)
        self.assertIn("lookup_year_hint", source)
        self.assertIn("year_match", source)
        self.assertIn("hinted_year", source)
        self.assertIn("candidate_literal_norms = [", source)
        self.assertIn("fuzz.ratio(candidate_norm, lookup_target)", source)
        self.assertIn('df_display["rt_c/a"]', source)
        self.assertIn('"rt_c/a",', source)
        self.assertIn('"showDateTimeUtc"', source)
        self.assertIn('showtime_id', source)
        self.assertIn('aria_count == 0', source)
        self.assertIn('Merging rendered AMC DOM for', source)
        self.assertNotIn('_minimum_unique_movies = 10', source)
        self.assertNotIn('_screen_count * 0.45', source)
        self.assertNotIn('_previous_movie_count * 0.40', source)
        self.assertIn('_configured_theatre_names', source)
        self.assertNotIn('_combo_min_movies', source)
        self.assertIn('_display_duplicate_cols', source)
        self.assertIn('_clock_duplicate_mask', source)
        self.assertIn('NOALIST', source)
        self.assertIn('_a_list_excluded_for_showtime', source)
        self.assertIn('_cache_dir = _AMCPath("build/amc_combo_cache")', source)
        self.assertIn('_max_rounds = 2', source)
        self.assertIn('_inspect_source_evidence', source)
        self.assertIn('_coverage_state', source)
        self.assertIn('_clock_evidence_key', source)
        self.assertIn('_best_source_snapshot', source)
        self.assertIn('missing_showtime_slots', source)
        self.assertIn('unmatched_source_showtime_ids', source)
        self.assertIn('amc_combo_status', source)
        self.assertIn('.evidence.json', source)
        self.assertNotIn('_usable_threshold', source)
        self.assertIn('AMC_CHROMIUM_EXECUTABLE', source)
        self.assertIn('base64.b64decode', source)
        self.assertIn('HeadlessChrome/', AMC_BROWSER_CHILD_SCRIPT)
        self.assertNotIn('Chrome/123.0.0.0', AMC_BROWSER_CHILD_SCRIPT)
        self.assertIn('_previous_report_rows', source)
        self.assertIn('amc_previous_report_fallback.txt', source)
        self.assertIn('maximum age', Path('scripts/postprocess_report.py').read_text(encoding='utf-8'))
        identifiers = {
            node.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)
        }
        self.assertNotIn("targets", identifiers)

    def test_next_two_weekend_days_are_strictly_after_today(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        real_datetime = datetime

        cases = {
            # Monday-Friday all target the immediately upcoming weekend.
            "2026-10-05": ("2026-10-10", "2026-10-11"),
            "2026-10-06": ("2026-10-10", "2026-10-11"),
            "2026-10-07": ("2026-10-10", "2026-10-11"),
            "2026-10-08": ("2026-10-10", "2026-10-11"),
            "2026-10-09": ("2026-10-10", "2026-10-11"),
            # Saturday skips today: Sunday, then the following Saturday.
            "2026-10-10": ("2026-10-11", "2026-10-17"),
            # Sunday skips today and targets the following weekend.
            "2026-10-11": ("2026-10-17", "2026-10-18"),
        }

        for today_text, expected in cases.items():
            with self.subTest(today=today_text):
                fixed = real_datetime.strptime(today_text, "%Y-%m-%d")

                class FakeDateTime(real_datetime):
                    @classmethod
                    def now(cls, tz=None):
                        if tz is None:
                            return cls(
                                fixed.year, fixed.month, fixed.day, 12, 0, 0
                            )
                        return cls(
                            fixed.year, fixed.month, fixed.day, 12, 0, 0, tzinfo=tz
                        )

                ns["datetime"] = FakeDateTime
                ns["OVERRIDE_SATURDAY"] = ""
                first, second = ns["upcoming_weekend_pacific"]()
                self.assertEqual(
                    (first.isoformat(), second.isoformat()), expected
                )

    def test_weekend_override_keeps_explicit_saturday_sunday_pair(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        ns["OVERRIDE_SATURDAY"] = "2026-12-19"
        first, second = ns["upcoming_weekend_pacific"]()
        self.assertEqual(
            (first.isoformat(), second.isoformat()),
            ("2026-12-19", "2026-12-20"),
        )

    def test_imdb_final_ranking_preserves_leading_articles(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        chosen_title, chosen_key = ns["build_imdb_lookup"](None, ["Example"])
        self.assertEqual(chosen_title, "Example")
        self.assertEqual(chosen_key[0], 0)  # no year hint for plain "Example"
        self.assertEqual(chosen_key[1], 1)  # exact title identity still wins

    def test_imdb_anniversary_prefers_original_release_year(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        chosen_title, chosen_key = ns["build_imdb_lookup"](
            None, ["Example Feature 10th Anniversary Remastered"]
        )
        self.assertEqual(chosen_title, "Example Feature")
        self.assertEqual(chosen_key[0], 1)

    def test_rt_anniversary_tries_original_release_year_first(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        calls = []

        def fake_fetch(session, url, params=None, tries=2):
            calls.append(url)
            return 404, ""

        ns["fetch_html"] = fake_fetch
        ns["rt_get_scores"](
            None, "Example Feature 10th Anniversary Remastered", {}, debug=False
        )
        self.assertTrue(calls)
        self.assertTrue(calls[0].endswith("/m/example_feature_2016"), calls[0])

    def test_amc_current_ssr_payload_fallback_extracts_showtimes(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        parser = ns["extract_showtimes_from_json_scripts"]

        # Current AMC pages can serialize the useful fields inside escaped
        # React/Next flight data rather than a clean JSON script.
        html = r'''
        <script>
        self.__next_f.push([1,"aria-label\":\"Showtimes for Runner\" blah
        \"showtimeId\":123456,\"status\":\"AVAILABLE\",\"showDateTimeUtc\":\"2026-09-27T02:00:00Z\",\"display\":{\"time\":\"7:00\",\"amPm\":\"PM\"}
        aria-label\":\"Showtimes for Example Two\" blah
        \"showtimeId\":789012,\"status\":\"AVAILABLE\",\"showDateTimeUtc\":\"2026-09-27T04:30:00Z\",\"display\":{\"time\":\"9:30\",\"amPm\":\"PM\"}"])
        </script>
        '''
        rows = parser(html, "AMC Example 10", ns["date"](2026, 9, 26))
        self.assertEqual(
            [(r["movie_title"], r["show_time"]) for r in rows],
            [("Runner", "7:00 pm"), ("Example Two", "9:30 pm")],
        )
        self.assertTrue(all(r["show_date"] == "2026-09-26" for r in rows))

    def test_amc_ssr_fallback_preserves_noalist_attribute(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        parser = ns["extract_showtimes_from_json_scripts"]
        html = r'''
        aria-label\":\"Showtimes for Special Event\"
        \"showtimeId\":555,\"status\":\"AVAILABLE\",\"showDateTimeUtc\":\"2026-09-27T02:00:00Z\",\"display\":{\"time\":\"7:00\",\"amPm\":\"PM\"},\"attributes\":[{\"code\":\"NOALIST\",\"name\":\"Excluded from A-List\"}]
        aria-label\":\"Showtimes for Regular Movie\"
        \"showtimeId\":556,\"status\":\"AVAILABLE\",\"showDateTimeUtc\":\"2026-09-27T04:00:00Z\",\"display\":{\"time\":\"9:00\",\"amPm\":\"PM\"},\"attributes\":[{\"code\":\"RESERVEDSEATING\"}]
        '''
        rows = parser(html, "AMC Example 10", ns["date"](2026, 9, 26))
        by_id = {r["showtime_id"]: r for r in rows}
        self.assertTrue(by_id["555"]["a_list_excluded"])
        self.assertFalse(by_id["556"]["a_list_excluded"])

    def test_rendered_dom_uses_format_block_noalist_without_marking_sibling_format(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]
        rendered = r'''
        <div aria-label="Showtimes for Special Event">
          <section><span>Excluded from A-List</span><a href="/showtimes/701">7:00 PM</a></section>
          <section><span>Reserved Seating</span><a href="/showtimes/702">9:00 PM</a></section>
        </div>
        <div aria-label="Showtimes for Movie Two"><a href="/showtimes/703">7:10 PM</a></div>
        <div aria-label="Showtimes for Movie Three"><a href="/showtimes/704">7:20 PM</a></div>
        <div aria-label="Showtimes for Movie Four"><a href="/showtimes/705">7:30 PM</a></div>
        <div aria-label="Showtimes for Movie Five"><a href="/showtimes/706">7:40 PM</a></div>
        <div aria-label="Showtimes for Movie Six"><a href="/showtimes/707">7:50 PM</a></div>
        '''
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, "")
        ns["fetch_html_with_browser"] = lambda url, params=None, timeout_ms=30000: (200, rendered)
        rows = scraper(None, "AMC Example 10", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
        by_id = {r.get("showtime_id"): r for r in rows}
        self.assertTrue(by_id["701"]["a_list_excluded"])
        self.assertFalse(by_id["702"]["a_list_excluded"])

    def test_amc_ssr_fallback_rejects_wrong_local_date(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        parser = ns["extract_showtimes_from_json_scripts"]
        html = r'''
        aria-label\":\"Showtimes for Example\"
        \"showtimeId\":123,\"status\":\"AVAILABLE\",\"showDateTimeUtc\":\"2026-09-28T03:00:00Z\",\"display\":{\"time\":\"8:00\",\"amPm\":\"PM\"}
        '''
        rows = parser(html, "AMC Example 10", ns["date"](2026, 9, 26))
        self.assertEqual(rows, [])

    def test_amc_ssr_fallback_rejects_unbounded_reordered_records(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        parser = ns["extract_showtimes_from_json_scripts"]
        html = r'''
        aria-label\":\"Showtimes for Hanuman Ansh\"
        \"showtimeId\":444,\"status\":\"AVAILABLE\",\"showDateTimeUtc\":\"2026-09-27T02:15:00Z\",\"display\":{\"time\":\"7:15\",\"amPm\":\"PM\"}
        \"showtimeId\":445,\"display\":{\"time\":\"7:25\",\"amPm\":\"PM\"},\"showDateTimeUtc\":\"2026-09-27T02:25:00Z\",\"status\":\"AVAILABLE\"
        \"showtimeId\":446,\"showDateTimeUtc\":\"2026-09-27T02:35:00Z\",\"status\":\"AVAILABLE\",\"display\":{\"time\":\"7:35\",\"amPm\":\"PM\"}
        '''
        rows = parser(html, "AMC Example 10", ns["date"](2026, 9, 26))
        self.assertEqual(
            [(r["showtime_id"], r["movie_title"], r["show_time"]) for r in rows],
            [("444", "Hanuman Ansh", "7:15 pm")],
        )

    def test_non_200_static_still_uses_browser(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        ns["fetch_amc_html"] = lambda session, url, params=None: (403, "blocked")
        rendered = "".join(
            f'<div aria-label="Showtimes for Movie {i}"><a href="/showtimes/{100+i}">8:{i:02d} PM</a></div>'
            for i in range(1, 7)
        )
        calls = []
        def browser(url, params=None, timeout_ms=30000):
            calls.append(1)
            return 200, rendered
        ns["fetch_html_with_browser"] = browser

        rows = scraper(None, "AMC Example 10", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({r["movie_title"] for r in rows}), 6)

    def test_complete_static_page_still_gets_one_rendered_merge_for_missing_times(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        static = "".join(
            f'<div aria-label="Showtimes for Movie {i}"><a href="/showtimes/{100+i}">7:00 PM</a></div>'
            for i in range(1, 7)
        )
        rendered = static + '<div aria-label="Showtimes for Movie 1"><a href="/showtimes/999">9:45 PM</a></div>'
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, static)
        calls = []
        def browser(url, params=None, timeout_ms=30000):
            calls.append(1)
            return 200, rendered
        ns["fetch_html_with_browser"] = browser

        rows = scraper(None, "AMC Example 10", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
        self.assertEqual(len(calls), 1)
        movie1 = [r for r in rows if r["movie_title"] == "Movie 1"]
        self.assertEqual({r["showtime_id"] for r in movie1}, {"101", "999"})

    def test_browser_only_small_schedule_is_corroborated_and_merged_across_rounds(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        # With no usable static evidence, a single browser snapshot is not
        # enough to call a low-count response complete. A second independent
        # browser observation is merged rather than replacing the first.
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, "")
        rendered_pages = [
            "".join(
                f'<div aria-label="Showtimes for Movie {i}"><a href="/showtimes/{100+i}">7:{i:02d} PM</a></div>'
                for i in range(1, 4)
            ),
            "".join(
                f'<div aria-label="Showtimes for Movie {i}"><a href="/showtimes/{100+i}">8:{i:02d} PM</a></div>'
                for i in range(4, 7)
            ),
        ]
        calls = []
        def browser(url, params=None, timeout_ms=30000):
            calls.append(1)
            return 200, rendered_pages[min(len(calls)-1, 1)]
        ns["fetch_html_with_browser"] = browser

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                rows = scraper(None, "AMC Example 10", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
            finally:
                os.chdir(old_cwd)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({r["movie_title"] for r in rows}), 6)

    def _reordered_flight_payload(self, total=4):
        """Synthetic source where only the first record matches the strict parser order."""
        chunks = []
        for i in range(1, total + 1):
            chunks.append(f'aria-label\\\":\\\"Showtimes for Source Movie {i}\\\"')
            if i == 1:
                chunks.append(
                    f'\\\"showtimeId\\\":{100+i},\\\"status\\\":\\\"AVAILABLE\\\",'
                    f'\\\"showDateTimeUtc\\\":\\\"2026-09-27T02:{i:02d}:00Z\\\",'
                    f'\\\"display\\\":{{\\\"time\\\":\\\"7:{i:02d}\\\",\\\"amPm\\\":\\\"PM\\\"}}'
                )
            else:
                # Same information, deliberately reordered. The independent
                # source-evidence scanner must still see it even if the strict
                # row parser does not.
                chunks.append(
                    f'\\\"showtimeId\\\":{100+i},'
                    f'\\\"display\\\":{{\\\"time\\\":\\\"7:{i:02d}\\\",\\\"amPm\\\":\\\"PM\\\"}},'
                    f'\\\"showDateTimeUtc\\\":\\\"2026-09-27T02:{i:02d}:00Z\\\",'
                    f'\\\"status\\\":\\\"AVAILABLE\\\"'
                )
        return '<script>self.__next_f.push([1,"' + ' '.join(chunks) + '"])</script>'

    def test_source_parser_gap_is_recorded_and_not_accepted_as_complete(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]
        partial_source = self._reordered_flight_payload(total=4)
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, partial_source)
        browser_calls = []
        def browser(url, params=None, timeout_ms=30000):
            browser_calls.append(1)
            return 200, partial_source
        ns["fetch_html_with_browser"] = browser

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                rows = scraper(None, "AMC Example 10", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                status_path = Path("build/amc_combo_status/amc-example-10-2026-09-26.json")
                status = json.loads(status_path.read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(len(browser_calls), 2)
        self.assertEqual(len({r["movie_title"] for r in rows}), 1)
        self.assertFalse(status["complete"])
        self.assertEqual(status["source_titles"], 4)
        self.assertEqual(status["parsed_titles"], 1)
        self.assertGreaterEqual(len(status["missing_titles"]), 3)

    def test_source_parser_gap_can_be_recovered_by_second_rendered_round(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]
        partial_source = self._reordered_flight_payload(total=4)
        rendered_full = "".join(
            f'<div aria-label="Showtimes for Source Movie {i}"><a href="/showtimes/{100+i}">7:{i:02d} PM</a></div>'
            for i in range(1, 5)
        )
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, partial_source)
        calls = []
        def browser(url, params=None, timeout_ms=30000):
            calls.append(1)
            return (200, partial_source) if len(calls) == 1 else (200, rendered_full)
        ns["fetch_html_with_browser"] = browser

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                rows = scraper(None, "AMC Example 10", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                status = json.loads(Path(
                    "build/amc_combo_status/amc-example-10-2026-09-26.json"
                ).read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(len(calls), 2)
        self.assertEqual(len({r["movie_title"] for r in rows}), 4)
        self.assertTrue(status["complete"])
        self.assertEqual(status["missing_titles"], [])
        self.assertEqual(status["missing_showtime_slots"], [])

    def test_persisted_source_evidence_survives_a_later_smaller_response(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]
        rich_source = self._reordered_flight_payload(total=4)
        tiny_source = self._reordered_flight_payload(total=1)

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                ns["fetch_amc_html"] = lambda session, url, params=None: (200, rich_source)
                ns["fetch_html_with_browser"] = lambda url, params=None, timeout_ms=30000: (200, rich_source)
                scraper(None, "AMC Example 10", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))

                # Simulate the next Papermill attempt getting a smaller source.
                ns["fetch_amc_html"] = lambda session, url, params=None: (200, tiny_source)
                ns["fetch_html_with_browser"] = lambda url, params=None, timeout_ms=30000: (200, tiny_source)
                scraper(None, "AMC Example 10", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                status = json.loads(Path(
                    "build/amc_combo_status/amc-example-10-2026-09-26.json"
                ).read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertFalse(status["complete"])
        self.assertEqual(status["source_titles"], 4)
        self.assertEqual(status["parsed_titles"], 1)

    def test_one_movie_schedule_is_valid_when_static_and_browser_both_expose_one(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]
        one = '<div aria-label="Showtimes for Only Feature"><a href="/showtimes/901">7:00 PM</a></div>'
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, one)
        browser_calls = []
        def browser(url, params=None, timeout_ms=30000):
            browser_calls.append(1)
            return 200, one
        ns["fetch_html_with_browser"] = browser

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                rows = scraper(None, "AMC Example 5", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                status = json.loads(Path(
                    "build/amc_combo_status/amc-example-5-2026-09-26.json"
                ).read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(len(browser_calls), 1)
        self.assertEqual(len({r["movie_title"] for r in rows}), 1)
        self.assertTrue(status["complete"])

    def _duplicate_id_same_slot_payload(self):
        """Two AMC IDs that describe the same visible movie/time slot."""
        return (
            r'aria-label\":\"Showtimes for Stable Feature\" '
            r'\"showtimeId\":501,\"status\":\"AVAILABLE\",\"showDateTimeUtc\":\"2026-09-27T02:00:00Z\",\"display\":{\"time\":\"7:00\",\"amPm\":\"PM\"} '
            r'\"showtimeId\":999,\"display\":{\"time\":\"7:00\",\"amPm\":\"PM\"},\"showDateTimeUtc\":\"2026-09-27T02:00:00Z\",\"status\":\"AVAILABLE\"'
        )

    def test_raw_showtime_id_mismatch_for_same_visible_slot_is_not_fatal(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]
        payload = self._duplicate_id_same_slot_payload()
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, payload)
        ns["fetch_html_with_browser"] = lambda url, params=None, timeout_ms=30000: (200, payload)

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                rows = scraper(None, "AMC Example 10", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                status = json.loads(Path(
                    "build/amc_combo_status/amc-example-10-2026-09-26.json"
                ).read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(len({r["movie_title"] for r in rows}), 1)
        self.assertTrue(status["complete"])
        # Serialized React slots are diagnostic only in v27.
        self.assertEqual(status["source_showtime_slots"], 0)
        self.assertEqual(status["source_serialized_showtime_slots"], 1)
        self.assertEqual(status["parsed_showtime_slots"], 1)
        self.assertGreaterEqual(len(status["unmatched_source_showtime_ids"]), 1)


    def test_full_movie_coverage_with_one_extra_raw_id_is_complete(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]
        chunks = []
        for i in range(1, 14):
            chunks.append(f'aria-label\":\"Showtimes for Coverage Movie {i}\"')
            chunks.append(
                f'\"showtimeId\":{600+i},\"status\":\"AVAILABLE\",'
                f'\"showDateTimeUtc\":\"2026-09-27T02:{i:02d}:00Z\",'
                f'\"display\":{{\"time\":\"7:{i:02d}\",\"amPm\":\"PM\"}}'
            )
            if i == 1:
                chunks.append(
                    f'\"showtimeId\":9999,\"display\":{{\"time\":\"7:{i:02d}\",\"amPm\":\"PM\"}},'
                    f'\"showDateTimeUtc\":\"2026-09-27T02:{i:02d}:00Z\",\"status\":\"AVAILABLE\"'
                )
        payload = " ".join(chunks)
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, payload)
        ns["fetch_html_with_browser"] = lambda url, params=None, timeout_ms=30000: (200, payload)

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                rows = scraper(None, "AMC Example 30", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                status = json.loads(Path(
                    "build/amc_combo_status/amc-example-30-2026-09-26.json"
                ).read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(len({r["movie_title"] for r in rows}), 13)
        self.assertTrue(status["complete"])
        self.assertEqual(status["source_titles"], 13)
        self.assertEqual(status["parsed_titles"], 13)
        self.assertEqual(status["source_showtime_slots"], 0)
        self.assertEqual(status["source_serialized_showtime_slots"], 13)
        self.assertEqual(status["parsed_showtime_slots"], 13)
        self.assertEqual(len(status["unmatched_source_showtime_ids"]), 1)


    def test_serialized_slot_surplus_is_diagnostic_not_fatal(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]
        payload = (
            r'aria-label\":\"Showtimes for Stable Feature\" '
            r'\"showtimeId\":501,\"status\":\"AVAILABLE\",\"showDateTimeUtc\":\"2026-09-27T02:00:00Z\",\"display\":{\"time\":\"7:00\",\"amPm\":\"PM\"} '
            r'\"showtimeId\":999,\"display\":{\"time\":\"9:00\",\"amPm\":\"PM\"},\"showDateTimeUtc\":\"2026-09-27T04:00:00Z\",\"status\":\"AVAILABLE\"'
        )
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, payload)
        ns["fetch_html_with_browser"] = lambda url, params=None, timeout_ms=30000: (200, payload)

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                scraper(None, "AMC Example 10", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                status = json.loads(Path(
                    "build/amc_combo_status/amc-example-10-2026-09-26.json"
                ).read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertTrue(status["complete"])
        self.assertEqual(status["source_titles"], 1)
        self.assertEqual(status["parsed_titles"], 1)
        self.assertEqual(status["source_showtime_slots"], 0)
        self.assertEqual(status["source_serialized_showtime_slots"], 2)
        self.assertEqual(status["parsed_showtime_slots"], 1)
        self.assertEqual(status["missing_showtime_slots"], [])

    def test_missing_distinct_direct_dom_showtime_slot_remains_fatal(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]
        # Same internal ID on two directly rendered clock links forces the row
        # deduper to retain one row. The independent DOM evidence must still
        # detect that a genuinely different visible clock time was dropped.
        rendered = (
            '<div aria-label="Showtimes for Stable Feature">'
            '<a href="/showtimes/501">7:00 PM</a>'
            '<a href="/showtimes/501">9:00 PM</a>'
            '</div>'
        )
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, rendered)
        ns["fetch_html_with_browser"] = lambda url, params=None, timeout_ms=30000: (200, rendered)

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                scraper(None, "AMC Example 10", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                status = json.loads(Path(
                    "build/amc_combo_status/amc-example-10-2026-09-26.json"
                ).read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertFalse(status["complete"])
        self.assertEqual(status["source_titles"], 1)
        self.assertEqual(status["parsed_titles"], 1)
        self.assertEqual(status["source_showtime_slots"], 2)
        self.assertEqual(status["parsed_showtime_slots"], 1)
        self.assertEqual(len(status["missing_showtime_slots"]), 1)


    def test_embedded_react_slot_surplus_does_not_outvote_complete_rendered_dom(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        # Reproduce the v26 production shape with two independent snapshots:
        # static React data contains many serialized records, while the browser
        # DOM exposes the smaller set of actually clickable time links. The
        # serialized surplus must be diagnostic, not a fatal completeness gate.
        dom_parts = []
        sid = 1000
        for i in range(1, 14):
            times = [f"5:{i:02d} PM"]
            if i <= 10:  # 10*2 + 3*1 = 23 directly rendered slots.
                times.append(f"8:{i:02d} PM")
            links = []
            for tm in times:
                sid += 1
                links.append(f'<a href="/showtimes/{sid}">{tm}</a>')
            dom_parts.append(
                f'<div aria-label="Showtimes for Coverage Movie {i}">' + "".join(links) + "</div>"
            )
        rendered = "".join(dom_parts)

        serialized = []
        serial_id = 5000
        total_serialized = 0
        for i in range(1, 14):
            per_movie = 4 if i <= 3 else 3  # 3*4 + 10*3 = 42 serialized slots.
            for j in range(per_movie):
                total_serialized += 1
                serial_id += 1
                minute = (i * 4 + j) % 60
                hour = 7 + (j % 4)
                # Repeating the movie anchor per record mirrors how fragmented
                # Next-flight chunks can restate a movie region during hydration.
                serialized.append(f'aria-label\\":\\"Showtimes for Coverage Movie {i}\\"')
                serialized.append(
                    f'\\"showtimeId\\":{serial_id},'
                    f'\\"display\\":{{\\"time\\":\\"{hour}:{minute:02d}\\",\\"amPm\\":\\"PM\\"}},'
                    f'\\"showDateTimeUtc\\":\\"2026-09-27T0{2+j}:{minute:02d}:00Z\\",'
                    f'\\"status\\":\\"AVAILABLE\\"'
                )
        self.assertEqual(total_serialized, 42)
        static_payload = '<script>self.__next_f.push([1,"' + " ".join(serialized) + '"])</script>'

        ns["fetch_amc_html"] = lambda session, url, params=None: (200, static_payload)
        ns["fetch_html_with_browser"] = lambda url, params=None, timeout_ms=30000: (200, rendered)

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                rows = scraper(None, "AMC Example 30", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                status = json.loads(Path(
                    "build/amc_combo_status/amc-example-30-2026-09-26.json"
                ).read_text(encoding="utf-8"))
                evidence = json.loads(Path(
                    "build/amc_combo_cache/amc-example-30-2026-09-26.evidence.json"
                ).read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(len({r["movie_title"] for r in rows}), 13)
        self.assertTrue(status["complete"])
        self.assertEqual(status["source_titles"], 13)
        self.assertEqual(status["parsed_titles"], 13)
        self.assertEqual(status["source_showtime_slots"], 23)
        self.assertEqual(status["parsed_showtime_slots"], 23)
        self.assertEqual(status["missing_showtime_slots"], [])
        # At least one saved snapshot contains the richer serialized-only view;
        # it does not outvote the trusted rendered snapshot.
        self.assertGreaterEqual(
            max(len(snap.get("serialized_slots") or []) for snap in evidence["snapshots"]),
            40,
        )


    def test_aggregate_guard_rejects_recorded_source_parser_gap(self):
        source = self._patched_source()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_dir = root / "build" / "amc_combo_status"
            status_dir.mkdir(parents=True)
            (status_dir / "amc-tustin-14-the-district-2026-09-26.json").write_text(
                json.dumps({"complete": False, "reason": "source exposed 8 but parser covered 3"}),
                encoding="utf-8",
            )
            old_cwd = os.getcwd()
            os.chdir(root)
            try:
                with self.assertRaisesRegex(RuntimeError, "source exposed 8 but parser covered 3"):
                    exec(compile(source, "<source-gap-aggregate-test>", "exec"), {})
            finally:
                os.chdir(old_cwd)

    def test_fresh_combo_cache_is_rechecked_on_next_papermill_attempt(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        rendered = "".join(
            f'<div aria-label="Showtimes for Movie {i}"><a href="/showtimes/{100+i}">8:{i:02d} PM</a></div>'
            for i in range(1, 7)
        )
        network = {"static": 0, "browser": 0}
        def static(session, url, params=None):
            network["static"] += 1
            return 200, ""
        def browser(url, params=None, timeout_ms=30000):
            network["browser"] += 1
            return 200, rendered
        ns["fetch_amc_html"] = static
        ns["fetch_html_with_browser"] = browser

        args = (None, "AMC Example 10", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
        first = scraper(*args)
        first_counts = dict(network)
        second = scraper(*args)
        self.assertEqual(len(first), len(second))
        self.assertGreater(network["static"], first_counts["static"])
        self.assertGreater(network["browser"], first_counts["browser"])

    def test_recent_same_weekend_report_is_last_good_fallback_and_keeps_noalist(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        # Force the live/static sources to remain empty so recovery must come
        # from the checked-in prior report for this exact theatre/date.
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, "")
        ns["fetch_html_with_browser"] = lambda url, params=None, timeout_ms=30000: (200, "")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "last_run_utc.txt").write_text(
                datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ") + "\n",
                encoding="utf-8",
            )

            showtimes = []
            table_rows = []
            for i in range(1, 7):
                title = f"Cached Movie {i}"
                time_txt = f"7:{i:02d} pm"
                showtimes.append({
                    "movie_id": i,
                    "movie": title,
                    "theater": "AMC Example 10",
                    "format": "",
                    "date": "2026-09-26",
                    "start": time_txt,
                    "runtime_min": 100 + i,
                    # Deliberately omit a_list_excluded to verify backward
                    # compatibility with reports generated before that field.
                })
                marker = "⛔" if i == 1 else ""
                table_rows.append(
                    f'<tr data-movie-id="{i}"><td><div class="movie-cell-title">{title}</div></td>'
                    f'<td>AMC Example 10<br>• 2026-09-26: {time_txt}{marker}</td></tr>'
                )

            payload = json.dumps({"movies": [], "showtimes": showtimes})
            report_html = (
                "<html><body><table><tbody>" + "".join(table_rows) + "</tbody></table>"
                f'<script id="showtimes-data" type="application/json">{payload}</script>'
                "</body></html>"
            )
            (root / "docs" / "index.html").write_text(report_html, encoding="utf-8")

            old_cwd = os.getcwd()
            os.chdir(root)
            try:
                rows = scraper(
                    None,
                    "AMC Example 10",
                    "https://example.invalid/showtimes",
                    ns["date"](2026, 9, 26),
                )
            finally:
                os.chdir(old_cwd)

            self.assertEqual(len({r["movie_title"] for r in rows}), 6)
            first = next(r for r in rows if r["movie_title"] == "Cached Movie 1")
            self.assertTrue(first["a_list_excluded"])
            marker_path = root / "build" / "amc_previous_report_fallback.txt"
            self.assertTrue(marker_path.exists())
            self.assertIn("AMC Example 10 | 2026-09-26", marker_path.read_text(encoding="utf-8"))

    def test_large_theatre_five_movies_still_gets_one_browser_enrichment_pass(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        static = "".join(
            f'<div aria-label="Showtimes for Static {i}"><a href="/showtimes/{i}">7:{i:02d} PM</a></div>'
            for i in range(1, 6)
        )
        rendered = "".join(
            f'<div aria-label="Showtimes for Movie {i}"><a href="/showtimes/{100+i}">8:{i:02d} PM</a></div>'
            for i in range(1, 13)
        )
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, static)
        browser_calls = []

        def browser(url, params=None, timeout_ms=30000):
            browser_calls.append((url, params, timeout_ms))
            return 200, rendered

        ns["fetch_html_with_browser"] = browser
        rows = scraper(
            None,
            "AMC Example 30",
            "https://example.invalid/showtimes",
            ns["date"](2026, 9, 26),
        )
        self.assertEqual(len(browser_calls), 1)
        self.assertGreaterEqual(len({r["movie_title"] for r in rows}), 12)

    def test_low_count_advance_schedule_across_all_theatres_is_accepted(self):
        source = self._patched_source()
        source = source.replace(
            "dates = [date(2026, 9, 26), date(2026, 9, 27)]",
            "dates = [date(2026, 10, 10), date(2026, 10, 11)]",
            1,
        )
        rows = []
        movie_num = 0
        for theatre, per_day in [
            ("AMC Tustin 14 @ The District", 5),
            ("AMC Woodbridge 5", 1),
            ("AMC Orange 30", 10),
        ]:
            for show_date in ("2026-10-10", "2026-10-11"):
                for _ in range(per_day):
                    movie_num += 1
                    rows.append({
                        "movie_title": f"Advance Movie {movie_num}",
                        "format_label": "",
                        "theatre": theatre,
                        "show_date": show_date,
                    })
        replacement = "showtimes = " + repr(rows) + "\ndf_show = pd.DataFrame(showtimes)"
        source = re.sub(
            r"_fixture_theatres = \[.*?\]\nshowtimes = \[.*?\]\ndf_show = pd.DataFrame\(showtimes\)",
            replacement,
            source,
            count=1,
            flags=re.S,
        )
        ns = {}
        exec(compile(source, "<low-count-advance-schedule-test>", "exec"), ns)
        self.assertEqual(ns["_unique_movie_count"], len(rows))

    def test_asymmetric_advance_schedule_is_warning_not_failure(self):
        source = self._patched_source()
        source = source.replace(
            "dates = [date(2026, 9, 26), date(2026, 9, 27)]",
            "dates = [date(2026, 10, 10), date(2026, 10, 11)]",
            1,
        )
        rows = []
        movie_num = 0
        schedule = [
            ("AMC Tustin 14 @ The District", "2026-10-11", 5),
            ("AMC Woodbridge 5", "2026-10-10", 1),
            ("AMC Woodbridge 5", "2026-10-11", 1),
            ("AMC Orange 30", "2026-10-10", 10),
            ("AMC Orange 30", "2026-10-11", 10),
        ]
        for theatre, show_date, count in schedule:
            for _ in range(count):
                movie_num += 1
                rows.append({
                    "movie_title": f"Advance Movie {movie_num}",
                    "format_label": "",
                    "theatre": theatre,
                    "show_date": show_date,
                })
        replacement = "showtimes = " + repr(rows) + "\ndf_show = pd.DataFrame(showtimes)"
        source = re.sub(
            r"_fixture_theatres = \[.*?\]\nshowtimes = \[.*?\]\ndf_show = pd.DataFrame\(showtimes\)",
            replacement,
            source,
            count=1,
            flags=re.S,
        )
        ns = {}
        exec(compile(source, "<asymmetric-advance-schedule-test>", "exec"), ns)
        self.assertEqual(ns["_unique_movie_count"], len(rows))

    def test_rows_from_unconfigured_theatre_only_are_rejected(self):
        source = self._patched_source()
        source = re.sub(
            r"showtimes = \[.*?\]\ndf_show = pd.DataFrame\(showtimes\)",
            "showtimes = ["
            "{'movie_title': f'Partial Movie {i}', 'format_label': '', "
            "'theatre': 'AMC Example 30', "
            "'show_date': '2026-09-26' if i % 2 == 0 else '2026-09-27'} "
            "for i in range(5)]\ndf_show = pd.DataFrame(showtimes)",
            source,
            count=1,
            flags=re.S,
        )
        with self.assertRaisesRegex(RuntimeError, "missing AMC Tustin 14 @ The District entirely"):
            exec(compile(source, "<partial-report-test>", "exec"), {})


    def test_missing_tustin_is_rejected_even_when_other_theatres_cover_both_dates(self):
        source = self._patched_source()
        replacement = """showtimes = [
    {
        'movie_title': f'Partial Movie {i}',
        'format_label': '',
        'theatre': 'AMC Orange 30' if i % 2 == 0 else 'AMC Woodbridge 5',
        'show_date': '2026-09-26' if (i // 2) % 2 == 0 else '2026-09-27',
    }
    for i in range(40)
]
df_show = pd.DataFrame(showtimes)"""
        source = re.sub(
            r"_fixture_theatres = \[.*?\]\nshowtimes = \[.*?\]\ndf_show = pd.DataFrame\(showtimes\)",
            replacement,
            source,
            count=1,
            flags=re.S,
        )
        with self.assertRaisesRegex(RuntimeError, "missing AMC Tustin 14 @ The District entirely"):
            exec(compile(source, "<missing-tustin-test>", "exec"), {})

    def test_one_day_partial_matrix_is_rejected(self):
        source = self._patched_source()
        replacement = """showtimes = [
    {
        'movie_title': f'Partial Movie {i}',
        'format_label': '',
        'theatre': [
            'AMC Tustin 14 @ The District',
            'AMC Woodbridge 5',
            'AMC Orange 30',
        ][i % 3],
        'show_date': '2026-09-27',
    }
    for i in range(45)
]
df_show = pd.DataFrame(showtimes)"""
        source = re.sub(
            r"_fixture_theatres = \[.*?\]\nshowtimes = \[.*?\]\ndf_show = pd.DataFrame\(showtimes\)",
            replacement,
            source,
            count=1,
            flags=re.S,
        )
        with self.assertRaisesRegex(RuntimeError, r"missing requested weekend date\(s\): 2026-09-26"):
            exec(compile(source, "<missing-saturday-test>", "exec"), {})

    def test_sparse_static_page_merges_rendered_aria_regions(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        static = '''
        <div aria-label="Showtimes for Static One"><a href="/showtimes/1">7:00 PM</a></div>
        '''
        rendered = '''
        <div aria-label="Showtimes for Static One"><a href="/showtimes/1">7:00 PM</a></div>
        <div aria-label="Showtimes for Movie Two"><a href="/showtimes/2">7:10 PM</a></div>
        <div aria-label="Showtimes for Movie Three"><a href="/showtimes/3">7:20 PM</a></div>
        <div aria-label="Showtimes for Movie Four"><a href="/showtimes/4">7:30 PM</a></div>
        <div aria-label="Showtimes for Movie Five"><a href="/showtimes/5">7:40 PM</a></div>
        <div aria-label="Showtimes for Movie Six"><a href="/showtimes/6">7:50 PM</a></div>
        '''
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, static)
        browser_calls = []

        def browser(url, params=None, timeout_ms=30000):
            browser_calls.append((url, params, timeout_ms))
            return 200, rendered

        ns["fetch_html_with_browser"] = browser
        rows = scraper(
            None,
            "AMC Example 10",
            "https://example.invalid/showtimes",
            ns["date"](2026, 9, 26),
        )
        self.assertEqual(len({r["movie_title"] for r in rows}), 6)
        self.assertEqual(len(browser_calls), 1)

    def test_amc_same_showtime_id_is_merged_across_ssr_and_dom(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        ns["Path"] = Path
        scraper = ns["scrape_amc_showtimes_for_date"]

        html = r'''
        <script>
        self.__next_f.push([1,"aria-label\":\"Showtimes for Runner\"
        \"showtimeId\":42,\"status\":\"AVAILABLE\",\"showDateTimeUtc\":\"2026-09-27T02:00:00Z\",\"display\":{\"time\":\"7:00\",\"amPm\":\"PM\"}"])
        </script>
        <div aria-label="Showtimes for Runner">
          <a href="/showtimes/42">7:00 PM</a>
          <a href="/showtimes/42">7:00 PM</a>
        </div>
        '''
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, html)
        ns["_collect_movie_blocks"] = lambda soup: (_ for _ in ()).throw(
            AssertionError("legacy parser should not run when aria showtime links exist")
        )
        rows = scraper(
            None,
            "AMC Example 5",
            "https://example.invalid/showtimes",
            ns["date"](2026, 9, 26),
        )
        runner_rows = [r for r in rows if r["movie_title"] == "Runner"]
        self.assertEqual(len(runner_rows), 1)
        self.assertEqual(runner_rows[0]["showtime_id"], "42")
        self.assertEqual(runner_rows[0]["show_time"], "7:00 pm")

    def test_amc_distinct_showtime_ids_at_same_clock_time_are_preserved(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        ns["Path"] = Path
        scraper = ns["scrape_amc_showtimes_for_date"]

        html = '''
        <div aria-label="Showtimes for Example Movie">
          <a href="/showtimes/42">7:00 PM</a>
          <a href="/showtimes/43">7:00 PM</a>
        </div>
        '''
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, html)
        rows = scraper(
            None,
            "AMC Example 5",
            "https://example.invalid/showtimes",
            ns["date"](2026, 9, 26),
        )
        example_rows = [r for r in rows if r["movie_title"] == "Example Movie"]
        self.assertEqual({r["showtime_id"] for r in example_rows}, {"42", "43"})
        self.assertEqual(len(example_rows), 2)

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


class PublicReportScrubTests(unittest.TestCase):
    def _patched_remove(self):
        source = '''
import re
from bs4 import BeautifulSoup

def remove_noisy_output(soup: BeautifulSoup) -> None:
    pass
'''
        patched = patch_postprocess_source(source)
        ns = {}
        exec(compile(patched, "<postprocess-scrub-test>", "exec"), ns)
        return ns["remove_noisy_output"]

    def test_public_scrub_removes_info_warn_but_preserves_user_output(self):
        remove = self._patched_remove()
        soup = BeautifulSoup(
            '<div class="jp-OutputArea-child"><pre>[WARN] blocked\n[INFO] retry\nUpcoming weekend: Sat/Sun\n</pre></div>',
            'html.parser',
        )
        remove(soup)
        text = soup.get_text("\n")
        self.assertNotIn("[WARN]", text)
        self.assertNotIn("[INFO]", text)
        self.assertIn("Upcoming weekend", text)

    def test_public_scrub_removes_diagnostic_only_output_container(self):
        remove = self._patched_remove()
        soup = BeautifulSoup(
            '<div class="jp-OutputArea-child"><pre>[INFO] Merging rendered AMC DOM\n</pre></div>',
            'html.parser',
        )
        remove(soup)
        self.assertNotIn("Merging rendered AMC DOM", soup.get_text())
        self.assertIsNone(soup.find("div", class_="jp-OutputArea-child"))

    def test_showtimes_blob_serializes_a_list_exclusion(self):
        rows = parse_showtimes_blob(
            "AMC Example 10\n• 2026-09-26: 7:00 pm⛔ [Laser at AMC], 9:00 pm"
        )
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["a_list_excluded"])
        self.assertFalse(rows[1]["a_list_excluded"])


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

    def test_imdb_bridge_requires_exact_p345_value(self):
        wrong = self.entity("Q1", "Example", imdb_id="tt1111111")
        right = self.entity("Q2", "Example", imdb_id="tt2222222")
        self.assertFalse(posters._entity_has_imdb_id(wrong, "tt2222222"))
        self.assertTrue(posters._entity_has_imdb_id(right, "tt2222222"))

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

    def test_infobox_image_filename_accepts_bare_and_file_link(self):
        self.assertEqual(
            posters._infobox_image_filename(
                "{{Infobox film\n| name = Example\n| image = Example theatrical poster.jpg\n}}"
            ),
            "Example theatrical poster.jpg",
        )
        self.assertEqual(
            posters._infobox_image_filename(
                "{{Infobox film\n| image = [[File:Example poster.png|220px|Theatrical poster]]\n}}"
            ),
            "Example poster.png",
        )

    def test_infobox_fallback_uses_validated_page_when_pageimages_is_empty(self):
        ctx = self.context(
            display_title="Example",
            canonical_title="Example",
            runtime_min=122,
        )
        page = {
            "pageid": 77,
            "title": "Example (2026 film)",
            "pageprops": {"wikibase_item": "Q77"},
        }
        entity = self.entity("Q77", "Example", year=2026, runtime=122)
        with patch.object(posters, "_wikipedia_pages_for_titles", return_value=[page]), \
             patch.object(posters, "_wikipedia_search_pages", return_value=[]), \
             patch.object(posters, "_wikidata_entities", return_value=[entity]), \
             patch.object(posters, "_image_from_validated_wikipedia_infobox", return_value="https://upload.wikimedia.org/example-poster.jpg") as fallback:
            image, page_title, source, rejected = posters._image_via_wikipedia(ctx, 2026)

        self.assertEqual(image, "https://upload.wikimedia.org/example-poster.jpg")
        self.assertEqual(page_title, "Example (2026 film)")
        self.assertEqual(source, "wikipedia-exact-infobox")
        self.assertEqual(rejected, [])
        fallback.assert_called_once_with("Example (2026 film)")

    def test_infobox_fallback_does_not_scan_arbitrary_body_file(self):
        wikitext = (
            "{{Infobox film\n| name = Example\n}}\n"
            "Some article text. [[File:Premiere event photo.jpg|thumb|Cast at premiere]]"
        )
        self.assertIsNone(posters._infobox_image_filename(wikitext))

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
            "In the Heights", "Coco", "Ha-Chan, Shake Your Booty!",
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
            "Q2019": "korean",
            "Q2020": "south korea",
            "Q2021": "filipino",
            "Q2022": "philippines",
        })

    @staticmethod
    def context(*, languages=frozenset(), title="Example"):
        return posters.MovieContext(
            display_title=title,
            canonical_title=title,
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

    def test_audience_entity_uses_generic_special_presentation_candidates(self):
        """Presenter branding must not hide the underlying film's language."""
        context = posters.MovieContext(
            display_title="Alex Rivera's Night Garden 20th Anniversary",
            canonical_title="Alex Rivera's Night Garden",
            expected_year=None,
            runtime_min=100,
            spoken_languages=frozenset(),
            relax_runtime=True,
        )
        entity = self.entity(
            claims={
                "P364": [
                    {"mainsnak": {"datavalue": {"value": {"id": "Q2003"}}}}
                ],
            }
        )
        entity["labels"] = {"en": {"value": "Night Garden"}}

        searched: list[str] = []

        def qids_for_title(title: str, limit: int = 12):
            searched.append(title)
            return ["QMovie"] if title == "Night Garden" else []

        with patch.object(posters, "_wikidata_qids_for_title", side_effect=qids_for_title), \
             patch.object(
                 posters,
                 "_wikidata_entities",
                 side_effect=lambda qids: [entity] if qids else [],
             ):
            resolved = posters._audience_entity_for_context(
                context,
                imdb_id=None,
                wikipedia_page=None,
            )

        self.assertIs(resolved, entity)
        self.assertIn("Night Garden", searched)
        self.assertEqual(
            posters.classify_audience_focus(context, resolved).tags,
            ("Spanish-language",),
        )

    def test_plain_possessive_title_does_not_gain_unbranded_audience_candidate(self):
        """Ordinary possessive film titles stay intact outside event shapes."""
        context = posters.MovieContext(
            display_title="Alex Rivera's Night Garden",
            canonical_title="Alex Rivera's Night Garden",
            expected_year=None,
            runtime_min=100,
            spoken_languages=frozenset(),
            relax_runtime=False,
        )
        self.assertNotIn("Night Garden", posters._context_title_variants(context))

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

    def test_conflicting_wikidata_language_country_does_not_mislabel_market(self):
        # Same-title/stale metadata should not let one Filipino/Korean claim
        # override a contradictory US/English film identity.
        filipino_us = self.entity(
            claims={
                "P364": [{"mainsnak": {"datavalue": {"value": {"id": "Q2021"}}}}],
                "P495": [{"mainsnak": {"datavalue": {"value": {"id": "Q2005"}}}}],
            }
        )
        self.assertEqual(
            posters.classify_audience_focus(self.context(), filipino_us).tags,
            ("General",),
        )

        english_korea = self.entity(
            claims={
                "P364": [{"mainsnak": {"datavalue": {"value": {"id": "Q2002"}}}}],
                "P495": [{"mainsnak": {"datavalue": {"value": {"id": "Q2020"}}}}],
            }
        )
        self.assertEqual(
            posters.classify_audience_focus(self.context(), english_korea).tags,
            ("General",),
        )

    def test_wikipedia_film_language_context_can_mark_indian_market(self):
        result = posters.classify_audience_focus(
            self.context(),
            self.entity(),
            supplemental_focus_text="A 2026 Indian Telugu-language comedy film.",
        )
        self.assertIn("Indian", result.tags)

    def test_philippine_mythology_alone_does_not_mark_filipino_market(self):
        result = posters.classify_audience_focus(
            self.context(),
            self.entity(),
            supplemental_focus_text="An American animated film inspired by Philippine mythology.",
        )
        self.assertNotIn("Filipino", result.tags)

    def test_homosexuality_wording_can_mark_lgbtq_focus(self):
        result = posters.classify_audience_focus(
            self.context(),
            self.entity(),
            supplemental_focus_text="The drama explores masculinity, identity, and homosexuality.",
        )
        self.assertIn("LGBTQ+-focused", result.tags)

    def test_explicit_faith_title_wording_handles_sparse_new_releases(self):
        for title in ("A Biblical Journey", "The Eucharistic Miracle"):
            with self.subTest(title=title):
                result = posters.classify_audience_focus(
                    self.context(title=title),
                    self.entity(),
                )
                self.assertIn("Faith-oriented", result.tags)

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
