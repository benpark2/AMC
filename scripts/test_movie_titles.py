#!/usr/bin/env python3
"""Regression tests for AMC title, RT-score, and poster-quality fixes."""

from __future__ import annotations

import ast
import gzip
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
import scripts.postprocess_report as postprocess
import scripts.finalize_posters as posters

# Production defaults to 10-second AMC browser spacing plus a 60-second
# post-block cooldown. Unit tests exercise the same logic without wall-clock
# delays unless a test explicitly opts into timing behavior.
os.environ.setdefault("AMC_LIVE_SPACING_SECONDS", "0")
os.environ.setdefault("AMC_EXPLICIT_BLOCK_COOLDOWN_SECONDS", "0")


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

    def test_parenthesized_anniversary_is_generic_presentation_metadata(self):
        info = analyze_movie_title("Example Palace (20th Anniversary)", reference_year=2026)
        self.assertEqual(info.canonical_title, "Example Palace")
        self.assertEqual(info.inferred_release_year, 2006)
        self.assertEqual(
            metadata_release_year_hint("Example Palace (20th Anniversary)", reference_year=2026),
            2006,
        )

    def test_trailing_creator_credit_is_secondary_generic_candidate(self):
        variants = candidate_title_variants("Example Play by Alex Morgan")
        self.assertEqual(variants[0], "Example Play by Alex Morgan")
        self.assertIn("Example Play", variants)

    def test_ordinary_by_phrase_is_not_stripped_when_credit_is_not_person_like(self):
        variants = candidate_title_variants("Designed by Committee")
        self.assertNotIn("Designed", variants)

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

# Structural copy of the notebook's movie lookup loop. ``movie_titles`` is
# empty so this compiles/executes without invoking network stubs.
session = None
movie_titles = []
rt_cache = {}
imdb_cache = {}
movie_rows = []
for title in movie_titles:
    rt_aud, rt_crit, rt_url = rt_get_scores(session, title, rt_cache, debug=DEBUG_RT)
    imdb, imdb_runtime_min, imdb_url = imdb_rating_runtime_url(session, title, imdb_cache)

    rank, prim_score, prim_src = pick_primary(rt_aud, imdb, rt_crit)

    # Priority: AMC runtime first, IMDb runtime second
    amc_rt = df_show[df_show["movie_title"] == title]["runtime_min"].dropna()
    rt_min = int(amc_rt.iloc[0]) if not amc_rt.empty else imdb_runtime_min

    h, m = divmod(rt_min, 60) if rt_min else (0, 0)
    fmt_rt = f"{h}h {m}m" if h else (f"{m}m" if m else None)

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
sat, sun = upcoming_weekend_pacific()
dates = [sat, sun]
# Structural copy of the production scrape loop. Temporarily empty THEATRES so
# the fixture executes without network work while still exercising loop patching.
_scrape_order_fixture_theatres = THEATRES
THEATRES = []
for th in THEATRES:
    for d in dates:
        try:
            showtimes.extend(scrape_amc_showtimes_for_date(session, th["name"], th["url"], d))
            time.sleep(0.2)
        except Exception as e:
            print(f"[WARN] Failed scraping {th['name']} {d}: {e}")
THEATRES = _scrape_order_fixture_theatres
# Aggregate-guard fixtures below intentionally override the requested dates.
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
        self.assertIn("for th, d in balanced_amc_request_order(THEATRES, dates):", source)
        self.assertNotIn("for th in THEATRES:\n    for d in dates:", source)
        self.assertNotIn("for d in dates:\n    for th in THEATRES:", source)
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
        self.assertIn('Loading rendered AMC DOM for', source)
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
        self.assertIn('_max_rounds = 1', source)
        self.assertIn('return fetch_html(session, url, params=params, tries=1)', source)
        self.assertIn('_AMC_LIVE_HEALTH', source)
        self.assertIn('without another browser launch', source)
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

    def test_requested_dates_include_today_plus_normal_two_weekend_days(self):
        source = self._patched_source()
        self.assertIn(
            "dates = list(dict.fromkeys([current_pacific_date(), sat, sun]))",
            source,
        )
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        real_datetime = datetime
        fixed = real_datetime(2026, 10, 18, 12, 0, 0)

        class FakeDateTime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return cls(2026, 10, 18, 12, 0, 0)
                return cls(2026, 10, 18, 12, 0, 0, tzinfo=tz)

        ns["datetime"] = FakeDateTime
        ns["OVERRIDE_SATURDAY"] = ""
        today = ns["current_pacific_date"]()
        first, second = ns["upcoming_weekend_pacific"]()
        requested = list(dict.fromkeys([today, first, second]))
        self.assertEqual(
            [d.isoformat() for d in requested],
            ["2026-10-18", "2026-10-24", "2026-10-25"],
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
        # Provider search is intentionally first for speed; if it misses, the
        # anniversary-derived original release year is the first direct page.
        direct = [call for call in calls if "/m/" in call]
        self.assertTrue(direct)
        self.assertTrue(direct[0].endswith("/m/example_feature_2016"), direct[0])

    def test_rt_valid_new_page_without_scores_does_not_fall_through_to_old_title(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        current_year = ns["date"].today().year
        target_year = current_year - 1
        calls = []

        def fake_fetch(session, url, params=None, tries=2):
            calls.append(url)
            if url.endswith(f"/m/example_feature_{target_year}"):
                return 200, (
                    f"<html><h1>Example Feature</h1>"
                    f"<div>Release Date (Theaters)</div><div>Oct 1, {target_year}</div>"
                    "<div>Tomatometer 2 Reviews Popcornmeter Fewer than 50 Ratings</div>"
                    "</html>"
                )
            if url.endswith("/m/example_feature"):
                return 200, (
                    "<html><h1>Example Feature</h1>"
                    "<div>Release Date (Theaters)</div><div>Jan 1, 2007</div>"
                    '<score-board audiencescore="87"></score-board></html>'
                )
            return 404, ""

        ns["fetch_html"] = fake_fetch
        aud, crit, url = ns["rt_get_scores"](None, "Example Feature", {}, debug=False)
        self.assertIsNone(aud)
        self.assertIsNone(crit)
        self.assertTrue(url.endswith(f"/m/example_feature_{target_year}"), url)
        self.assertNotIn("/m/example_feature", calls[calls.index(url) + 1 :])

    def test_rt_recent_blank_alias_can_upgrade_to_same_release_scored_canonical(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        current_year = ns["date"].today().year
        recent_year = current_year - 1
        calls = []

        def fake_fetch(session, url, params=None, tries=2):
            calls.append(url)
            if "/napi/search/" in url:
                return 200, json.dumps({
                    "movies": [
                        {"name": "Example Feature", "year": recent_year, "url": f"/m/example_feature_{recent_year}"}
                    ]
                })
            if url.endswith(f"/m/example_feature_{recent_year}"):
                return 200, (
                    f"<html><h1>Example Feature</h1>"
                    f"<div>Release Date (Theaters)</div><div>Oct 1, {recent_year}</div>"
                    "<div>Runtime</div><div>1h 46m</div>"
                    "<div>Tomatometer 2 Reviews Popcornmeter Fewer than 50 Ratings</div>"
                    "</html>"
                )
            if url.endswith("/m/example_feature"):
                return 200, (
                    f"<html><h1>Example Feature</h1>"
                    f"<div>Release Date (Theaters)</div><div>Oct 1, {recent_year}</div>"
                    "<div>Runtime</div><div>1h 46m</div>"
                    '<score-board tomatometerscore="71" audiencescore="82"></score-board>'
                    "</html>"
                )
            return 404, ""

        ns["fetch_html"] = fake_fetch
        aud, crit, url = ns["rt_get_scores"](
            None, "Example Feature", {}, debug=False, runtime_min=106
        )
        self.assertEqual((aud, crit), (82, 71))
        self.assertTrue(url.endswith("/m/example_feature"), url)
        self.assertTrue(any(call.endswith(f"/m/example_feature_{recent_year}") for call in calls))

    def test_rt_search_infers_missing_result_year_from_movie_url(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        current_year = ns["date"].today().year
        recent_year = current_year - 1
        calls = []

        def fake_fetch(session, url, params=None, tries=2):
            calls.append(url)
            if "/napi/search/" in url:
                return 200, json.dumps({
                    "movies": [
                        {"name": "Example Feature", "year": 2007, "url": "/m/example_feature"},
                        {"name": "Example Feature", "url": f"/m/example_feature_{recent_year}"},
                    ]
                })
            if url.endswith(f"/m/example_feature_{recent_year}"):
                return 200, (
                    f"<html><h1>Example Feature</h1>"
                    f"<div>Release Date (Theaters)</div><div>Oct 1, {recent_year}</div>"
                    "<div>Runtime</div><div>2h 1m</div>"
                    "<div>Tomatometer 2 Reviews Popcornmeter Fewer than 50 Ratings</div>"
                    "</html>"
                )
            if url.endswith("/m/example_feature"):
                return 200, (
                    "<html><h1>Example Feature</h1>"
                    "<div>Release Date (Theaters)</div><div>Jan 1, 2007</div>"
                    "<div>Runtime</div><div>1h 3m</div>"
                    '<score-board audiencescore="87"></score-board></html>'
                )
            return 404, ""

        ns["fetch_html"] = fake_fetch
        aud, crit, url = ns["rt_get_scores"](
            None, "Example Feature", {}, debug=False, runtime_min=121
        )
        self.assertIsNone(aud)
        self.assertIsNone(crit)
        self.assertTrue(url.endswith(f"/m/example_feature_{recent_year}"), url)

    def test_rt_anniversary_year_search_can_recover_numeric_slug(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        current_year = ns["date"].today().year
        original_year = current_year - 20
        calls = []

        def fake_fetch(session, url, params=None, tries=2):
            calls.append(url)
            if "/napi/search/" in url:
                if str(original_year) in url:
                    return 200, json.dumps({
                        "movies": [
                            {"name": "Example Palace", "url": "/m/987654-example_palace"},
                            {"name": "Example Palace", "year": original_year, "url": "/m/example_palace_tv"},
                        ]
                    })
                return 200, json.dumps({"movies": []})
            if url.endswith("/m/987654-example_palace"):
                return 200, (
                    f"<html><h1>Example Palace</h1>"
                    f"<div>Release Date (Theaters)</div><div>Oct 1, {original_year}</div>"
                    "<div>Runtime</div><div>2h 3m</div>"
                    '<score-board tomatometerscore="58" audiencescore="56"></score-board>'
                    "</html>"
                )
            if url.endswith("/m/example_palace_tv"):
                return 200, (
                    f"<html><h1>Example Palace</h1>"
                    f"<div>Release Date (Theaters)</div><div>Jan 1, {original_year}</div>"
                    "<div>Runtime</div><div>1h 30m</div></html>"
                )
            return 404, ""

        ns["fetch_html"] = fake_fetch
        aud, crit, url = ns["rt_get_scores"](
            None,
            "Example Palace (20th Anniversary)",
            {},
            debug=False,
            runtime_min=123,
        )
        self.assertEqual((aud, crit), (56, 58))
        self.assertTrue(url.endswith("/m/987654-example_palace"), url)
        self.assertTrue(any(str(original_year) in call for call in calls if "/napi/search/" in call))

    def test_rt_reused_title_rejects_old_runtime_and_prefers_recent_identity(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        current_year = ns["date"].today().year
        recent_year = current_year - 1
        calls = []

        def fake_fetch(session, url, params=None, tries=2):
            calls.append(url)
            # Simulate a year-specific URL silently serving an older famous
            # same-title movie. Runtime must veto it.
            if url.endswith(f"/m/example_feature_{current_year}"):
                return 200, (
                    "<html><h1>Example Feature</h1>"
                    "<div>Runtime</div><div>1h 3m</div>"
                    '<score-board audiencescore="87"></score-board></html>'
                )
            if url.endswith(f"/m/example_feature_{recent_year}"):
                return 200, (
                    f"<html><h1>Example Feature</h1>"
                    f"<div>Release Date (Theaters)</div><div>Oct 1, {recent_year}</div>"
                    "<div>Runtime</div><div>2h 1m</div>"
                    "<div>Tomatometer 2 Reviews Popcornmeter Fewer than 50 Ratings</div>"
                    "</html>"
                )
            return 404, ""

        ns["fetch_html"] = fake_fetch
        aud, crit, url = ns["rt_get_scores"](
            None, "Example Feature", {}, debug=False, runtime_min=121
        )
        self.assertIsNone(aud)
        self.assertIsNone(crit)
        self.assertTrue(url.endswith(f"/m/example_feature_{recent_year}"), url)
        self.assertTrue(any(call.endswith(f"/m/example_feature_{current_year}") for call in calls))

    def test_rt_provider_search_recovers_non_derivable_movie_slug(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        current_year = ns["date"].today().year
        calls = []

        def fake_fetch(session, url, params=None, tries=2):
            calls.append(url)
            if "/napi/search/" in url:
                return 200, json.dumps({
                    "movies": [
                        {"name": "Example Palace", "year": current_year, "url": "/m/987654-example_palace"},
                        {"name": "Example Palaces", "year": current_year, "url": "/m/wrong"},
                    ]
                })
            if url.endswith("/m/987654-example_palace"):
                return 200, (
                    f"<html><h1>Example Palace</h1>"
                    f"<div>Release Date (Theaters)</div><div>Oct 1, {current_year}</div>"
                    '<score-board tomatometerscore="91" audiencescore="88"></score-board>'
                    "</html>"
                )
            return 404, ""

        ns["fetch_html"] = fake_fetch
        aud, crit, url = ns["rt_get_scores"](None, "Example Palace", {}, debug=False)
        self.assertEqual((aud, crit), (88, 91))
        self.assertTrue(url.endswith("/m/987654-example_palace"), url)
        self.assertTrue(any("/napi/search/" in call for call in calls))

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

    def test_amc_browser_date_helpers_distinguish_requested_future_day(self):
        tree = ast.parse(AMC_BROWSER_CHILD_SCRIPT)
        helper_names = {"query_date", "observed_schedule_dates", "date_matches_request"}
        helper_nodes = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in helper_names
        ]
        ns = {
            "re": re,
            "datetime": datetime,
        }
        from urllib.parse import parse_qs, urlparse
        from zoneinfo import ZoneInfo
        ns.update({"parse_qs": parse_qs, "urlparse": urlparse, "ZoneInfo": ZoneInfo})
        exec(compile(ast.Module(body=helper_nodes, type_ignores=[]), "<amc-date-helpers>", "exec"), ns)

        # 02:00 UTC on Sep 26 is still Sep 25 in Pacific time. A future-date
        # request must not accept that schedule merely because the DOM has rows.
        wrong_day_html = '<script>{"showDateTimeUtc":"2026-09-26T02:00:00Z"}</script>'
        self.assertEqual(ns["observed_schedule_dates"](wrong_day_html), ["2026-09-25"])
        self.assertFalse(ns["date_matches_request"](
            "2026-09-26",
            "https://www.amctheatres.com/example?date=2026-09-25",
            wrong_day_html,
        ))

        requested_day_html = '<script>{"showDateTimeUtc":"2026-09-27T02:00:00Z"}</script>'
        self.assertTrue(ns["date_matches_request"](
            "2026-09-26",
            "https://www.amctheatres.com/example",
            requested_day_html,
        ))

    def test_amc_browser_metadata_is_sanitized_for_actions_date_logging(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)

        payload = {
            "status": 200,
            "html": "<html>ok</html>",
            "url": "https://www.amctheatres.com/example?date=2026-09-26&queueToken=secret-token",
            "requested_date": "2026-09-26",
            "final_url_date": "2026-09-26",
            "observed_schedule_dates": ["2026-09-26"],
            "date_before_restore": None,
            "observed_before_restore": ["2026-09-25"],
            "date_restore_attempted": True,
            "date_match": True,
        }
        proc = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps(payload),
            "stderr": "",
        })()
        with patch("subprocess.run", return_value=proc):
            status, html = ns["fetch_html_with_browser"](
                "https://www.amctheatres.com/example",
                params={"date": "2026-09-26"},
            )

        self.assertEqual((status, html), (200, "<html>ok</html>"))
        meta = ns["_AMC_LAST_BROWSER_META"]
        self.assertEqual(meta["requested_date"], "2026-09-26")
        self.assertEqual(meta["final_url_date"], "2026-09-26")
        self.assertEqual(meta["final_host"], "www.amctheatres.com")
        self.assertEqual(meta["final_path"], "/example")
        self.assertTrue(meta["date_restore_attempted"])
        self.assertTrue(meta["date_match"])
        self.assertNotIn("secret-token", repr(meta))

    def test_amc_wrong_rendered_date_is_not_accepted_as_requested_day(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        # Serialized timestamp is Sep 25 Pacific, while the requested day is Sep 26.
        wrong_html = (
            '<div aria-label="Showtimes for Wrong Day Movie">'
            '<a href="/showtimes/901">7:00 PM</a></div>'
            '<script>{"showtimeId":901,"status":"AVAILABLE",'
            '"showDateTimeUtc":"2026-09-26T02:00:00Z",'
            '"display":{"time":"7:00","amPm":"PM"}}</script>'
        )
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, wrong_html)

        def browser(url, params=None, timeout_ms=30000):
            ns["_AMC_LAST_BROWSER_META"] = {
                "requested_date": "2026-09-26",
                "final_url_date": "2026-09-25",
                "final_host": "www.amctheatres.com",
                "final_path": "/example",
                "observed_schedule_dates": ["2026-09-25"],
                "date_restore_attempted": True,
                "date_match": False,
            }
            return 200, wrong_html

        ns["fetch_html_with_browser"] = browser
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"AMC_LIVE_SPACING_SECONDS": "0"}, clear=False
        ):
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                rows = scraper(
                    None,
                    "AMC Example 10",
                    "https://www.amctheatres.com/example",
                    ns["date"](2026, 9, 26),
                )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(rows, [])
        checks = ns["_AMC_LIVE_METRICS"]["date_checks"]
        self.assertTrue(checks)
        self.assertTrue(all(not check["date_valid"] for check in checks))
        self.assertTrue(any(check["kind"] == "browser" and check["restore_attempted"] for check in checks))

    def test_runner_local_retries_are_not_misreported_as_fresh_browser_results(self):
        """A blocked retry can reuse this run's rows without claiming live access."""
        source = self._patched_source()
        theatre = "AMC Generic 10"
        day = "2026-09-26"
        all_slots = {
            "Movie One": (901, "7:00 PM"),
            "Movie Two": (902, "8:00 PM"),
        }

        def live_page(titles):
            return "".join(
                f'<div aria-label="Showtimes for {title}">'
                f'<a href="/showtimes/{all_slots[title][0]}">{all_slots[title][1]}</a></div>'
                for title in titles
            )

        def attempt(html):
            # Recreate the separate Python process that Papermill launches on
            # each attempt; only build/ files survive across processes.
            ns = {}
            exec(compile(source, "<retry-provenance-test>", "exec"), ns)
            ns["fetch_html_with_browser"] = (
                lambda url, params=None, timeout_ms=30000: (200, html)
            )
            rows = ns["scrape_amc_showtimes_for_date"](
                None, theatre, "https://example.invalid/showtimes",
                ns["date"](2026, 9, 26),
            )
            metrics = ns["_AMC_LIVE_METRICS"]
            return rows, metrics, ns["_AMC_LIVE_HEALTH"]

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {
                "AMC_LIVE_SPACING_SECONDS": "0",
                "AMC_EXPLICIT_BLOCK_COOLDOWN_SECONDS": "0",
            }, clear=False,
        ):
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                rows, metrics, health = attempt(live_page(all_slots))
                self.assertEqual(len(rows), 2)
                self.assertEqual(metrics["combos_live"], 1)
                self.assertEqual(metrics["combos_reused"], 0)

                rows, metrics, health = attempt("<html>Access Denied</html>")
                self.assertEqual(len(rows), 2)
                self.assertEqual(metrics["combos_live"], 0)
                self.assertEqual(metrics["combos_reused"], 1)
                self.assertEqual(metrics["combos_cached"], 0)
                self.assertEqual(metrics["explicit_block_combos"], 1)
                self.assertEqual(len(health["failures"]), 1)

                # A partially successful navigation does not turn carried-over
                # showtimes from another retry into freshly observed showtimes.
                rows, metrics, health = attempt(live_page(["Movie One"]))
                self.assertEqual(len(rows), 2)
                self.assertEqual(metrics["combos_live"], 0)
                self.assertEqual(metrics["combos_mixed"], 1)
                self.assertEqual(metrics["combos_reused"], 0)

                rows, metrics, health = attempt(live_page(all_slots))
                self.assertEqual(len(rows), 2)
                self.assertEqual(metrics["combos_live"], 1)
                self.assertEqual(metrics["combos_mixed"], 0)
                self.assertEqual(metrics["combos_cached"], 0)
            finally:
                os.chdir(old_cwd)

    def test_workflow_resets_previous_report_fallback_marker_each_attempt(self):
        """Old public cache notices cannot leak through a successful retry."""
        workflow = Path(__file__).resolve().parents[1] / ".github/workflows/daily-AMC.yml"
        text = workflow.read_text(encoding="utf-8")
        loop_start = text.index("while true; do")
        papermill_start = text.index('AMC_PAPERMILL_ATTEMPT="${attempt}" papermill ', loop_start)
        retry_prelude = text[loop_start:papermill_start]
        self.assertIn(
            "rm -f build/amc_live_metrics.json build/amc_previous_report_fallback.txt",
            retry_prelude,
        )
        # The successful schedule cache must *not* be destroyed across retries.
        self.assertNotIn("rm -rf build/amc_combo_cache", retry_prelude)
        self.assertIn("f\"reused={data.get('combos_reused', 0)} ", text)
        self.assertIn('AMC_PAPERMILL_ATTEMPT="${attempt}" papermill', text)
        self.assertIn("f\"retry_skipped={data.get('combos_retry_skipped', 0)} ", text)
        self.assertIn("f\"mixed={data.get('combos_mixed', 0)} ", text)

    def test_amc_static_helper_does_not_launch_duplicate_browser_fallback(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)

        calls = []
        def static_fetch(session, url, params=None, tries=2):
            calls.append((url, params, tries))
            return 403, "access denied"

        ns["fetch_html"] = static_fetch
        status, html = ns["fetch_amc_html"](
            None,
            "https://example.invalid/showtimes",
            params={"date": "2026-09-26"},
        )
        self.assertEqual((status, html), (403, "access denied"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], 1)

    def test_balanced_rotating_amc_order_full_matrix_and_early_coverage(self):
        """Every slot is covered once, and early slots span theatres and dates."""
        ns = {}
        exec(compile(self._patched_source(), "<patched-notebook-test>", "exec"), ns)
        generate = ns["balanced_amc_request_order"]
        theatres = [{"name": name} for name in ("Tustin", "Orange", "Woodbridge")]
        dates = [ns["date"](2026, 10, day) for day in (22, 24, 25)]
        anchor = ns["date"](2026, 10, 23)
        all_combos = {(th["name"], day) for th in theatres for day in dates}
        starts = set()
        early_sets = set()
        for offset in range(9):
            run_day = anchor + ns["timedelta"](days=offset)
            order = [(th["name"], day) for th, day in generate(theatres, dates, run_day)]
            self.assertEqual(len(order), 9)
            self.assertEqual(set(order), all_combos)
            self.assertEqual(len({th for th, _ in order[:3]}), 3)
            self.assertEqual(len({day for _, day in order[:3]}), 3)
            for th in theatres:
                self.assertEqual(sum(item[0] == th["name"] for item in order[:6]), 2)
            for day in dates:
                self.assertEqual(sum(item[1] == day for item in order[:6]), 2)
            starts.add(order[0])
            early_sets.add(frozenset(order[:3]))
        # Over one nine-day rotation, every theatre/date gets first priority.
        self.assertEqual(starts, all_combos)
        self.assertGreater(len(early_sets), 1)

    def test_balanced_order_generalizes_to_non_square_grids_and_empty_input(self):
        ns = {}
        exec(compile(self._patched_source(), "<patched-notebook-test>", "exec"), ns)
        generate = ns["balanced_amc_request_order"]
        anchor = ns["date"](2027, 1, 3)
        for theatre_count, date_count in ((2, 3), (4, 2), (1, 3), (3, 1)):
            theatres = [{"name": f"Theatre {i}"} for i in range(theatre_count)]
            dates = [anchor + ns["timedelta"](days=j) for j in range(date_count)]
            order = list(generate(theatres, dates, anchor))
            self.assertEqual(len(order), theatre_count * date_count)
            self.assertEqual(
                {(id(th), d) for th, d in order},
                {(id(th), d) for th in theatres for d in dates},
            )
        self.assertEqual(list(generate([], [anchor], anchor)), [])
        self.assertEqual(list(generate([{"name": "T"}], [], anchor)), [])

    def test_explicit_amc_block_requires_theatre_and_date_diversity_before_cache_circuit(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        network = {"static": 0, "browser": 0}
        def static(session, url, params=None):
            network["static"] += 1
            return 200, "<html><body>Access Denied</body></html>"
        def browser(url, params=None, timeout_ms=30000):
            network["browser"] += 1
            return 200, "<html><body>Verify you are human</body></html>"
        ns["fetch_amc_html"] = static
        ns["fetch_html_with_browser"] = browser

        theatres = [
            "AMC Alpha 10", "AMC Beta 10", "AMC Gamma 10", "AMC Delta 10",
            "AMC Epsilon 10", "AMC Zeta 10", "AMC Eta 10",
        ]
        first_date = "2026-09-26"
        second_date = "2026-09-27"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "last_run_utc.txt").write_text(
                datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ") + "\n",
                encoding="utf-8",
            )
            cached_rows = []
            combos = [
                (theatres[0], first_date),
                (theatres[1], second_date),
                (theatres[2], first_date),
                (theatres[3], second_date),
                (theatres[4], first_date),
                (theatres[5], second_date),
                (theatres[6], first_date),
            ]
            for idx, (theatre, wanted) in enumerate(combos, start=1):
                cached_rows.append({
                    "movie_id": idx,
                    "movie": f"Cached Film {idx}",
                    "theater": theatre,
                    "format": "",
                    "date": wanted,
                    "start": "7:00 pm",
                    "runtime_min": 100 + idx,
                    "a_list_excluded": False,
                })
            report_html = (
                '<html><body><script id="showtimes-data" type="application/json">'
                + json.dumps({"movies": [], "showtimes": cached_rows})
                + '</script></body></html>'
            )
            (root / "docs" / "index.html").write_text(report_html, encoding="utf-8")

            old_cwd = os.getcwd()
            os.chdir(root)
            try:
                with patch.dict(os.environ, {"AMC_LIVE_SPACING_SECONDS": "0"}, clear=False):
                    first = scraper(None, theatres[0], "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                    self.assertFalse(ns["_AMC_LIVE_HEALTH"]["circuit_open"])
                    second = scraper(None, theatres[1], "https://example.invalid/showtimes", ns["date"](2026, 9, 27))
                    self.assertFalse(ns["_AMC_LIVE_HEALTH"]["circuit_open"])
                    third = scraper(None, theatres[2], "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                    # Even consecutive blocks across multiple theatres/dates
                    # must not preempt any first-pass combination.
                    self.assertFalse(ns["_AMC_LIVE_HEALTH"]["circuit_open"])
                    fourth = scraper(None, theatres[3], "https://example.invalid/showtimes", ns["date"](2026, 9, 27))
                    self.assertFalse(ns["_AMC_LIVE_HEALTH"]["circuit_open"])
                    fifth = scraper(None, theatres[4], "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                    self.assertFalse(ns["_AMC_LIVE_HEALTH"]["circuit_open"])
                    sixth = scraper(None, theatres[5], "https://example.invalid/showtimes", ns["date"](2026, 9, 27))
                    self.assertFalse(ns["_AMC_LIVE_HEALTH"]["circuit_open"])
                    before_seventh = dict(network)
                    seventh = scraper(None, theatres[6], "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(first[0]["movie_title"], "Cached Film 1")
        self.assertEqual(second[0]["movie_title"], "Cached Film 2")
        self.assertEqual(third[0]["movie_title"], "Cached Film 3")
        self.assertEqual(fourth[0]["movie_title"], "Cached Film 4")
        self.assertEqual(fifth[0]["movie_title"], "Cached Film 5")
        self.assertEqual(sixth[0]["movie_title"], "Cached Film 6")
        self.assertEqual(seventh[0]["movie_title"], "Cached Film 7")
        self.assertEqual(before_seventh["browser"] + 1, network["browser"])
        self.assertEqual(network["static"], 0)
        self.assertEqual(network["browser"], 7)
        self.assertEqual(ns["_AMC_LIVE_METRICS"]["explicit_block_combos"], 7)
        self.assertEqual(ns["_AMC_LIVE_METRICS"]["circuit_cache_skips"], 0)
        self.assertEqual(len(ns["_AMC_LIVE_HEALTH"]["probed_combos"]), 7)
        self.assertFalse(ns["_AMC_LIVE_HEALTH"]["circuit_open"])

    def test_three_explicit_blocks_from_one_theatre_do_not_open_global_circuit(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, "<html>Access Denied</html>")
        ns["fetch_html_with_browser"] = lambda url, params=None, timeout_ms=30000: (200, "<html>Verify you are human</html>")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"AMC_LIVE_SPACING_SECONDS": "0"}, clear=False
        ):
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                for day in (26, 27, 28):
                    scraper(
                        None,
                        "AMC Single Problem Theatre",
                        "https://example.invalid/showtimes",
                        ns["date"](2026, 9, day),
                    )
                    self.assertFalse(ns["_AMC_LIVE_HEALTH"]["circuit_open"])
            finally:
                os.chdir(old_cwd)

        self.assertEqual(len(ns["_AMC_LIVE_HEALTH"]["failures"]), 3)

    def test_amc_live_success_resets_explicit_block_streak(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        rendered = "".join(
            f'<div aria-label="Showtimes for Movie {i}"><a href="/showtimes/{100+i}">8:{i:02d} PM</a></div>'
            for i in range(1, 7)
        )
        state = {"blocked": True}
        network = {"static": 0, "browser": 0}

        def static(session, url, params=None):
            network["static"] += 1
            if state["blocked"]:
                return 200, "<html><body>Access Denied</body></html>"
            return 200, rendered

        def browser(url, params=None, timeout_ms=30000):
            network["browser"] += 1
            if state["blocked"]:
                return 200, "<html><body>Verify you are human</body></html>"
            return 200, rendered

        ns["fetch_amc_html"] = static
        ns["fetch_html_with_browser"] = browser
        theatres = [f"AMC Reset {i}" for i in range(1, 8)]
        combo_dates = [
            "2026-09-26", "2026-09-27", "2026-09-26",
            "2026-09-26", "2026-09-27", "2026-09-26", "2026-09-27",
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "last_run_utc.txt").write_text(
                datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ") + "\n",
                encoding="utf-8",
            )
            cached_rows = [
                {
                    "movie_id": idx,
                    "movie": f"Cached Reset Film {idx}",
                    "theater": theatre,
                    "format": "",
                    "date": combo_dates[idx - 1],
                    "start": "7:00 pm",
                    "runtime_min": 100,
                    "a_list_excluded": False,
                }
                for idx, theatre in enumerate(theatres, start=1)
            ]
            report_html = (
                '<html><body><script id="showtimes-data" type="application/json">'
                + json.dumps({"movies": [], "showtimes": cached_rows})
                + '</script></body></html>'
            )
            (root / "docs" / "index.html").write_text(report_html, encoding="utf-8")

            old_cwd = os.getcwd()
            os.chdir(root)
            try:
                with patch.dict(os.environ, {"AMC_LIVE_SPACING_SECONDS": "0"}, clear=False):
                    scraper(None, theatres[0], "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                    scraper(None, theatres[1], "https://example.invalid/showtimes", ns["date"](2026, 9, 27))
                    self.assertFalse(ns["_AMC_LIVE_HEALTH"]["circuit_open"])
                    self.assertEqual(len(ns["_AMC_LIVE_HEALTH"]["failures"]), 2)

                    # A real live success must clear the two-block streak.
                    state["blocked"] = False
                    live_rows = scraper(None, theatres[2], "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                    self.assertTrue(live_rows)
                    self.assertFalse(ns["_AMC_LIVE_HEALTH"]["circuit_open"])
                    self.assertEqual(ns["_AMC_LIVE_HEALTH"]["failures"], [])

                    # Two more diverse blocks after the success still must not open it.
                    state["blocked"] = True
                    scraper(None, theatres[3], "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                    scraper(None, theatres[4], "https://example.invalid/showtimes", ns["date"](2026, 9, 27))
                    self.assertFalse(ns["_AMC_LIVE_HEALTH"]["circuit_open"])

                    # Even the third block following a success cannot deprive
                    # the remaining combination of its first browser chance.
                    scraper(None, theatres[5], "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                    self.assertFalse(ns["_AMC_LIVE_HEALTH"]["circuit_open"])
                    before_seventh = dict(network)
                    seventh = scraper(None, theatres[6], "https://example.invalid/showtimes", ns["date"](2026, 9, 27))
                    self.assertEqual(before_seventh["browser"] + 1, network["browser"])
                    self.assertEqual(seventh[0]["movie_title"], "Cached Reset Film 7")
                    self.assertEqual(ns["_AMC_LIVE_METRICS"]["circuit_cache_skips"], 0)
            finally:
                os.chdir(old_cwd)

    def test_all_nine_combos_attempted_even_after_eight_explicit_blocks(self):
        """A late success after eight blocks must not be lost to circuit skip."""
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]
        theatres = [f"AMC Test {i}" for i in range(3)]
        dates = [ns["date"](2026, 9, day) for day in (26, 27, 28)]
        ns["THEATRES"] = [{"name": theater, "url": "https://example.invalid/showtimes"} for theater in theatres]
        ns["dates"] = dates
        order = list(ns["balanced_amc_request_order"](ns["THEATRES"], dates, dates[0]))
        browser_calls = []
        success_html = "".join(
            f'<div aria-label="Showtimes for Movie {i}"><a href="/showtimes/{100+i}">8:{i:02d} PM</a></div>'
            for i in range(1, 7)
        )

        def browser(url, params=None, timeout_ms=30000):
            browser_calls.append(params["date"])
            if len(browser_calls) < 9:
                return 200, "<html><body>Verify you are human</body></html>"
            return 200, success_html

        ns["fetch_html_with_browser"] = browser
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "last_run_utc.txt").write_text(
                datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8"
            )
            cached = [
                {"movie_id": i, "movie": f"Cached Film {i}", "theater": th["name"],
                 "format": "", "date": day.isoformat(), "start": "7:00 pm", "runtime_min": 90,
                 "a_list_excluded": False}
                for i, (th, day) in enumerate(order, start=1)
            ]
            (root / "docs" / "index.html").write_text(
                '<html><body><script id="showtimes-data" type="application/json">'
                + json.dumps({"movies": [], "showtimes": cached})
                + '</script></body></html>', encoding="utf-8"
            )
            old_cwd = os.getcwd()
            os.chdir(root)
            try:
                with patch.dict(os.environ, {"AMC_LIVE_SPACING_SECONDS": "0", "AMC_EXPLICIT_BLOCK_COOLDOWN_SECONDS": "0"}, clear=False):
                    for index, (theater, day) in enumerate(order, start=1):
                        rows = scraper(None, theater["name"], theater["url"], day)
                        self.assertTrue(rows)
                        self.assertEqual(len(browser_calls), index)
                        if index < 9:
                            self.assertFalse(ns["_AMC_LIVE_HEALTH"]["circuit_open"])
            finally:
                os.chdir(old_cwd)
        self.assertEqual(len(browser_calls), 9)
        self.assertEqual(ns["_AMC_LIVE_METRICS"]["circuit_cache_skips"], 0)
        self.assertEqual(ns["_AMC_LIVE_METRICS"]["combos_cached"], 8)
        self.assertEqual(ns["_AMC_LIVE_METRICS"]["combos_live"], 1)
        self.assertFalse(ns["_AMC_LIVE_HEALTH"]["circuit_open"])

    def test_showtime_source_badges_by_movie_not_assumed_timestamp(self):
        """One movie can mix live/cached theatres and dates; never invent dates."""
        showtimes = [
            {"movie_id": 1, "theater": "AMC Test A", "date": "2026-10-24", "start": "12:00 pm"},
            {"movie_id": 1, "theater": "AMC Test A", "date": "2026-10-24", "start": "8:00 pm"},
            {"movie_id": 1, "theater": "AMC Test B", "date": "2026-10-25", "start": "9:00 pm"},
            {"movie_id": 2, "theater": "AMC Test A", "date": "2026-10-24", "start": "2:00 pm"},
            {"movie_id": 3, "theater": "AMC Test B", "date": "2026-10-25", "start": "7:00 pm"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "amc_previous_report_fallback.txt"
            marker.write_text("AMC Test A | 2026-10-24\nmalformed entry\n", encoding="utf-8")
            cached_combos = postprocess.read_cached_showtime_combos(marker)
            self.assertEqual(cached_combos, {("amc test a", "2026-10-24")})
        statuses = postprocess.movie_showtime_freshness(showtimes, cached_combos)
        self.assertEqual(statuses[1][0], "mixed")
        self.assertIn("1/2", statuses[1][1])
        self.assertEqual(statuses[2][0], "cached")
        self.assertNotIn("updated", statuses[2][1].lower())
        self.assertEqual(statuses[3][0], "fresh")

        soup = BeautifulSoup(
            '<table><thead><tr><th>#</th><th>Movie</th><th>RT_C/A</th><th>IMDb</th>'
            '<th>Showtimes</th><th>Runtime</th></tr></thead><tbody>'
            '<tr data-movie-id="1"><td>1</td><td>Movie Example</td><td></td><td></td>'
            '<td>AMC Test A 2026-10-24</td><td>100 min</td></tr></tbody></table>',
            "html.parser",
        )
        postprocess.apply_final_table_schema(soup, soup.table, {1: "Movie Example"}, {}, statuses)
        # Theatre/date-level fetch indicators replace the redundant summary.
        self.assertIsNone(soup.select_one('span.showtimes-freshness'))
        self.assertEqual(len(soup.select("thead th")), 6)  # no extra wide column

    def test_amc_live_loads_are_spaced_and_metrics_written_outside_notebook(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        rendered = "".join(
            f'<div aria-label="Showtimes for Movie {i}"><a href="/showtimes/{100+i}">8:{i:02d} PM</a></div>'
            for i in range(1, 7)
        )
        static_calls = []
        ns["fetch_amc_html"] = lambda *args, **kwargs: static_calls.append(1) or (200, rendered)
        ns["fetch_html_with_browser"] = lambda url, params=None, timeout_ms=30000: (200, rendered)

        clock = [100.0]
        sleeps = []
        def monotonic():
            return clock[0]
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {
                 "AMC_LIVE_SPACING_SECONDS": "10",
                 "AMC_EXPLICIT_BLOCK_COOLDOWN_SECONDS": "30",
             }, clear=False), \
             patch("time.monotonic", side_effect=monotonic), \
             patch("time.sleep", side_effect=sleep):
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                first = scraper(None, "AMC Example 10", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                second = scraper(None, "AMC Example 11", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                metrics_path = Path("build/amc_live_metrics.json")
                self.assertTrue(metrics_path.exists())
                metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(static_calls, [])
        self.assertEqual(metrics["spacing_seconds"], 10.0)
        self.assertEqual(metrics["block_cooldown_seconds"], 30.0)
        self.assertEqual(metrics["static_requests"], 0)
        self.assertEqual(metrics["browser_launches"], 2)
        self.assertEqual(metrics["combos_live"], 2)
        self.assertEqual(metrics["live_page_loads"], 2)
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 10.0, places=3)
        # The notebook source writes JSON only; the Actions workflow prints the
        # human-readable diagnostics after Papermill, so they cannot enter HTML.
        self.assertNotIn("[AMC-METRICS]", source)
        workflow = Path(".github/workflows/daily-AMC.yml").read_text(encoding="utf-8")
        self.assertIn("cooldown_slept=", workflow)
        self.assertIn("date_valid=", workflow)
        self.assertNotIn("accepted={check.get('accepted'", workflow)

    def test_explicit_block_cooldown_precedes_next_browser_attempt(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        rendered = "".join(
            f'<div aria-label="Showtimes for Movie {i}"><a href="/showtimes/{100+i}">8:{i:02d} PM</a></div>'
            for i in range(1, 7)
        )
        browser_results = [
            (200, "<html><body>Verify you are human</body></html>"),
            (200, rendered),
        ]
        browser_calls = []
        def browser(url, params=None, timeout_ms=30000):
            browser_calls.append((url, params))
            return browser_results[len(browser_calls) - 1]
        ns["fetch_html_with_browser"] = browser

        clock = [200.0]
        sleeps = []
        def monotonic():
            return clock[0]
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {
                 "AMC_LIVE_SPACING_SECONDS": "10",
                 "AMC_EXPLICIT_BLOCK_COOLDOWN_SECONDS": "60",
             }, clear=False), \
             patch("time.monotonic", side_effect=monotonic), \
             patch("time.sleep", side_effect=sleep):
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "last_run_utc.txt").write_text(
                datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ") + "\n",
                encoding="utf-8",
            )
            cached = [{
                "movie_id": 1, "movie": "Cached Film", "theater": "AMC First 10",
                "format": "", "date": "2026-09-26", "start": "7:00 pm",
                "runtime_min": 100, "a_list_excluded": False,
            }]
            (root / "docs" / "index.html").write_text(
                '<script id="showtimes-data" type="application/json">'
                + json.dumps({"movies": [], "showtimes": cached}) + "</script>",
                encoding="utf-8",
            )
            old_cwd = os.getcwd()
            os.chdir(root)
            try:
                cached_rows = scraper(None, "AMC First 10", "https://example.invalid/first", ns["date"](2026, 9, 26))
                live_rows = scraper(None, "AMC Second 10", "https://example.invalid/second", ns["date"](2026, 9, 27))
                metrics = json.loads(Path("build/amc_live_metrics.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(cached_rows[0]["movie_title"], "Cached Film")
        self.assertTrue(live_rows)
        self.assertEqual(len(browser_calls), 2)
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 60.0, places=3)
        self.assertAlmostEqual(metrics["block_cooldown_sleep_seconds"], 60.0, places=3)

    def test_static_preflight_is_not_called_and_browser_runs_once(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        ns["fetch_amc_html"] = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("static AMC preflight must not run")
        )
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
        self.assertEqual(len(calls), 1)
        self.assertEqual(len({r["movie_title"] for r in rows}), 6)
        self.assertEqual(ns["_AMC_LIVE_METRICS"]["static_requests"], 0)
        self.assertEqual(ns["_AMC_LIVE_METRICS"]["browser_launches"], 1)

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

    def test_browser_only_small_schedule_uses_single_snapshot(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        # Browser-only mode intentionally performs one helper call per combo.
        # A structurally complete small schedule is accepted without a second
        # request solely to corroborate its size.
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
        self.assertEqual(len(calls), 1)
        self.assertEqual(len({r["movie_title"] for r in rows}), 3)

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

    def test_serialized_only_title_surplus_is_diagnostic_not_fatal(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]
        # React/Next payloads can contain title shells/records that never become
        # clickable showtimes. They are useful diagnostics, but not hard proof
        # that the row parser missed a bookable film.
        payload = self._reordered_flight_payload(total=4)
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, payload)
        browser_calls = []
        def browser(url, params=None, timeout_ms=30000):
            browser_calls.append(1)
            return 200, payload
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

        self.assertEqual(len(browser_calls), 1)
        self.assertEqual(len({r["movie_title"] for r in rows}), 1)
        self.assertTrue(status["complete"])
        self.assertEqual(status["source_titles"], 0)
        self.assertEqual(status["source_detected_titles"], 4)
        self.assertEqual(status["parsed_titles"], 1)
        self.assertEqual(status["missing_titles"], [])

    def test_actionable_source_parser_gap_does_not_trigger_second_browser_round(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        # Four rendered movie regions intentionally reuse one AMC showtime ID.
        # The independent evidence scanner sees four actionable movie/time
        # pairs, while add_row() retains only the last title for that shared ID.
        # This is a genuine hard coverage gap, but browser-only mode must not
        # make a second AMC request just to try to repair it.
        partial_rendered = "".join(
            f'<div aria-label="Showtimes for Source Movie {i}"><a href="/showtimes/501">7:{i:02d} PM</a></div>'
            for i in range(1, 5)
        )
        rendered_full = "".join(
            f'<div aria-label="Showtimes for Source Movie {i}"><a href="/showtimes/{600+i}">7:{i:02d} PM</a></div>'
            for i in range(1, 5)
        )
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, partial_rendered)
        calls = []
        def browser(url, params=None, timeout_ms=30000):
            calls.append(1)
            return (200, partial_rendered) if len(calls) == 1 else (200, rendered_full)
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

        self.assertEqual(len(calls), 1)
        self.assertEqual(len({r["movie_title"] for r in rows}), 1)
        self.assertFalse(status["complete"])
        self.assertEqual(status["source_titles"], 4)
        self.assertEqual(len(status["missing_titles"]), 3)

    def test_persisted_actionable_evidence_survives_a_later_smaller_response(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]
        rich_source = "".join(
            f'<div aria-label="Showtimes for Source Movie {i}"><a href="/showtimes/501">7:{i:02d} PM</a></div>'
            for i in range(1, 5)
        )
        tiny_source = '<div aria-label="Showtimes for Source Movie 1"><a href="/showtimes/701">7:01 PM</a></div>'

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                ns["fetch_amc_html"] = lambda session, url, params=None: (200, rich_source)
                ns["fetch_html_with_browser"] = lambda url, params=None, timeout_ms=30000: (200, rich_source)
                scraper(None, "AMC Example 10", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))

                # Simulate the next Papermill attempt getting a smaller source.
                # The earlier richer *actionable* snapshot must remain the hard
                # comparison target rather than being erased by the tiny page.
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
        self.assertEqual(status["source_showtime_slots"], 4)
        self.assertLess(status["parsed_titles"], 4)

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
        # Serialized React slots and title shells are diagnostic only.
        self.assertEqual(status["source_titles"], 0)
        self.assertEqual(status["source_detected_titles"], 1)
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
        self.assertEqual(status["source_titles"], 0)
        self.assertEqual(status["source_detected_titles"], 13)
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
        self.assertEqual(status["source_titles"], 0)
        self.assertEqual(status["source_detected_titles"], 1)
        self.assertEqual(status["parsed_titles"], 1)
        self.assertEqual(status["source_showtime_slots"], 0)
        self.assertEqual(status["source_serialized_showtime_slots"], 2)
        self.assertEqual(status["parsed_showtime_slots"], 1)
        self.assertEqual(status["missing_showtime_slots"], [])

    def test_orphan_title_shell_with_no_rendered_showtime_is_nonfatal(self):
        source = self._patched_source()
        ns = {}
        exec(compile(source, "<patched-notebook-test>", "exec"), ns)
        scraper = ns["scrape_amc_showtimes_for_date"]

        # Reproduce the production failure numerically: AMC exposes 15
        # aria-labelled title shells, but only 14 are attached to clickable
        # rendered showtimes. Those 14 actionable movies contain 58 trusted
        # movie/time slots, and the parser recovers all 58. The orphan title
        # shell must therefore be diagnostic, not a fatal "missing movie".
        actionable = []
        sid = 800
        expected_slots = 0
        for i in range(1, 15):
            per_movie = 5 if i <= 2 else 4  # 2*5 + 12*4 = 58
            links = []
            for j in range(per_movie):
                sid += 1
                hour = 1 + j
                minute = (i * 3) % 60
                links.append(f'<a href="/showtimes/{sid}">{hour}:{minute:02d} PM</a>')
                expected_slots += 1
            actionable.append(
                f'<div aria-label="Showtimes for Actionable Movie {i}">' + "".join(links) + '</div>'
            )
        self.assertEqual(expected_slots, 58)
        orphan = '<div aria-label="Showtimes for Placeholder Card"></div>'
        rendered = "".join(actionable) + orphan
        ns["fetch_amc_html"] = lambda session, url, params=None: (200, rendered)
        ns["fetch_html_with_browser"] = lambda url, params=None, timeout_ms=30000: (200, rendered)

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                rows = scraper(None, "AMC Example 14", "https://example.invalid/showtimes", ns["date"](2026, 9, 26))
                status = json.loads(Path(
                    "build/amc_combo_status/amc-example-14-2026-09-26.json"
                ).read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(len({r["movie_title"] for r in rows}), 14)
        self.assertEqual(len(rows), 58)
        self.assertTrue(status["complete"])
        self.assertEqual(status["source_detected_titles"], 15)
        self.assertEqual(status["source_titles"], 14)
        self.assertEqual(status["source_showtime_slots"], 58)
        self.assertEqual(status["parsed_showtime_slots"], 58)
        self.assertEqual(len(status["source_diagnostic_only_titles"]), 1)
        self.assertEqual(status["missing_titles"], [])
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
        ns["fetch_html_with_browser"] = lambda url, params=None, timeout_ms=30000: (200, rendered + static_payload)

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
        self.assertEqual(network["static"], 0)
        self.assertEqual(first_counts["static"], 0)
        self.assertGreater(network["browser"], first_counts["browser"])

    def test_retry_skips_only_prior_verified_amc_combos(self):
        """Papermill retry avoids redundant browser loads but retains safeguards."""
        source = self._patched_source()
        rendered = ''.join(
            f'<div aria-label="Showtimes for Movie {i}"><a href="/showtimes/{100+i}">8:{i:02d} PM</a></div>'
            for i in range(1, 7)
        )
        for mutation, should_skip in (
            ("none", True),
            ("fallback-status", False),
            ("missing-live-marker", False),
            ("missing-combo-status", False),
            ("corrupt-combo-cache", False),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                cwd = Path.cwd()
                os.chdir(tmp)
                try:
                    calls = []
                    def run(attempt):
                        ns = {}
                        exec(compile(source, "<retry-reduced-browser-tests>", "exec"), ns)
                        ns["fetch_html_with_browser"] = (
                            lambda url, params=None, timeout_ms=30000: calls.append(1) or (200, rendered)
                        )
                        with patch.dict(os.environ, {"AMC_PAPERMILL_ATTEMPT": str(attempt)}):
                            rows = ns["scrape_amc_showtimes_for_date"](
                                None, "AMC Example 10", "https://example.invalid/showtimes",
                                ns["date"](2026, 9, 26),
                            )
                        return rows, ns["_AMC_LIVE_METRICS"]
                    first, first_metrics = run(1)
                    self.assertEqual(len(first), 6)
                    self.assertEqual(len(calls), 1)
                    status_file = Path("build/amc_combo_status/amc-example-10-2026-09-26.json")
                    marker_file = Path("build/amc_live_fetch_dates/amc-example-10-2026-09-26.json")
                    cache_file = Path("build/amc_combo_cache/amc-example-10-2026-09-26.json")
                    self.assertTrue(status_file.exists())
                    self.assertTrue(marker_file.exists())
                    if mutation == "fallback-status":
                        status = json.loads(status_file.read_text())
                        status["fallback"] = True
                        status_file.write_text(json.dumps(status))
                    elif mutation == "missing-live-marker":
                        marker_file.unlink()
                    elif mutation == "missing-combo-status":
                        status_file.unlink()
                    elif mutation == "corrupt-combo-cache":
                        cache_file.write_text("not json")
                    second, second_metrics = run(2)
                    self.assertEqual(len(second), 6)
                    self.assertEqual(len(calls), 1 if should_skip else 2)
                    self.assertEqual(second_metrics["combos_retry_skipped"], int(should_skip))
                    self.assertEqual(second_metrics["combos_reused"], int(should_skip))
                finally:
                    os.chdir(cwd)

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
        ns["fetch_html_with_browser"] = lambda url, params=None, timeout_ms=30000: (200, html)
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
        ns["fetch_html_with_browser"] = lambda url, params=None, timeout_ms=30000: (200, html)
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
        posters._TEXT_CACHE.clear()
        posters._PROVIDER_BUDGET_USED.clear()
        posters._LABEL_CACHE.clear()
        posters._LABEL_CACHE.update({
            "Q1001": "japanese",
            "Q1002": "hindi",
            "Q1003": "english",
            "Q1004": "catalan",
        })
        posters._LETTERBOXD_RESULT_CACHE.clear()
        posters._PREVIOUS_POSTER_INDEX = None

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
        rt_id: str | None = None,
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
        if rt_id:
            claims["P1258"] = [{"mainsnak": {"datavalue": {"value": rt_id}}}]
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

    def test_rt_metadata_runtime_rejects_wrong_same_title_page(self):
        ctx = self.context(canonical_title="Example", runtime_min=121)
        old_html = (
            "<html><h1>Example</h1>"
            "<div>Release Date (Theaters)</div><div>Jan 1, 2007</div>"
            "<div>Runtime</div><div>1h 3m</div>"
            '<img alt="Poster for Example" src="https://resizing.flixster.com/old.jpg">'
            "</html>"
        )
        with patch.object(posters, "_fetch_text", return_value=old_html):
            meta = posters._rt_metadata_for_context(
                "https://www.rottentomatoes.com/m/example", ctx
            )
        self.assertIsNone(meta)

    def test_valid_rt_year_becomes_downstream_identity_anchor(self):
        ctx = self.context(canonical_title="Example", runtime_min=119)
        meta = posters.RtPageMetadata(
            title="Example",
            poster_url="https://resizing.flixster.com/new.jpg",
            languages=frozenset({"english"}),
            genres=frozenset({"action"}),
            release_year=2026,
            runtime_min=119,
        )
        anchored = posters._context_with_rt_identity(ctx, meta)
        self.assertEqual(anchored.expected_year, 2026)
        self.assertEqual(anchored.runtime_min, 119)

    def test_wikidata_rt_identifier_backfills_anniversary_scores(self):
        soup = BeautifulSoup(
            '<table><tbody><tr data-movie-id="1"><td>1</td><td>'
            '<div class="movie-cell-title">Example Palace (20th Anniversary)</div>'
            '<div class="movie-poster-wrap"><img class="movie-poster" src="x.jpg"></div></td>'
            '<td></td><td data-source-url="https://www.imdb.com/title/tt12345678/">7.0</td>'
            '<td>AMC Example</td><td>2h 3m</td></tr></tbody></table>',
            "html.parser",
        )
        row = soup.find("tr")
        ctx = self.context(
            display_title="Example Palace (20th Anniversary)",
            canonical_title="Example Palace",
            expected_year=2006,
            runtime_min=123,
        )
        entity = self.entity(
            "Q1", "Example Palace", year=2006, runtime=123,
            imdb_id="tt12345678", rt_id="m/987654-example_palace",
        )
        meta = posters.RtPageMetadata(
            title="Example Palace",
            poster_url=None,
            languages=frozenset({"english"}),
            genres=frozenset({"drama"}),
            release_year=2006,
            runtime_min=123,
        )
        with patch.object(posters, "_wikidata_qids_for_imdb", return_value=["Q1"]), \
             patch.object(posters, "_wikidata_entities", return_value=[entity]), \
             patch.object(posters, "_rt_metadata_for_context", return_value=meta), \
             patch.object(posters, "_rt_scores_for_context", return_value=(56, 58)):
            anchored, chosen, changed = posters._repair_rt_identity_from_wikidata(
                soup, row, ctx
            )
        self.assertTrue(changed)
        self.assertEqual(chosen.release_year, 2006)
        self.assertEqual(anchored.expected_year, 2006)
        rt_cell = row.find_all("td", recursive=False)[2]
        self.assertEqual(
            rt_cell.get("data-source-url"),
            "https://www.rottentomatoes.com/m/987654-example_palace",
        )
        self.assertEqual(rt_cell.get_text(" ", strip=True), "58/56")

    def test_provider_budget_counts_request_work_not_global_wall_clock(self):
        url = "https://www.rottentomatoes.com/m/example"
        posters._PROVIDER_BUDGET_USED["rt"] = 29.5
        self.assertTrue(posters._provider_budget_available(url))
        posters._charge_provider_budget(url, 0.6)
        self.assertFalse(posters._provider_budget_available(url))

    def test_exact_imdb_poster_fallback_uses_identity_page(self):
        html = (
            '<html><meta property="og:image" '
            'content="https://m.media-amazon.com/images/M/example._V1_.jpg"></html>'
        )
        with patch.object(posters, "_fetch_text", return_value=html):
            image = posters._imdb_poster_url("tt12345678")
        self.assertEqual(
            image,
            "https://m.media-amazon.com/images/M/example._V1_.jpg",
        )

    def test_letterboxd_fallback_prefers_recent_runtime_matching_same_title(self):
        ctx = self.context(canonical_title="Example", runtime_min=121)
        old = posters.LetterboxdPageMetadata(
            title="Example",
            poster_url="https://a.ltrbxd.com/old.jpg",
            release_year=2007,
            runtime_min=63,
            imdb_id=None,
            url="https://letterboxd.com/film/example-old/",
        )
        new = posters.LetterboxdPageMetadata(
            title="Example",
            poster_url="https://a.ltrbxd.com/new.jpg",
            release_year=2025,
            runtime_min=121,
            imdb_id=None,
            url="https://letterboxd.com/film/example-new/",
        )
        urls = [old.url, new.url]
        with patch.object(posters, "_letterboxd_direct_urls", return_value=[]), \
             patch.object(posters, "_letterboxd_search_urls", return_value=urls), \
             patch.object(
                 posters,
                 "_letterboxd_page_metadata",
                 side_effect=lambda url: old if url == old.url else new,
             ):
            image, page, rejected = posters._image_via_letterboxd(ctx, 2026)
        self.assertEqual(image, new.poster_url)
        self.assertEqual(page, new.url)
        self.assertTrue(any("runtime" in item for item in rejected))

    def test_letterboxd_direct_slug_uses_known_year_before_plain_title(self):
        ctx = self.context(canonical_title="Example Movie", expected_year=2025)
        urls = posters._letterboxd_direct_urls(ctx)
        self.assertGreaterEqual(len(urls), 2)
        self.assertEqual(urls[0], "https://letterboxd.com/film/example-movie-2025/")
        self.assertEqual(urls[1], "https://letterboxd.com/film/example-movie/")

    def test_letterboxd_embed_can_supply_poster_when_main_page_has_none(self):
        ctx_url = "https://letterboxd.com/film/example-world-tour/"
        main = (
            '<html><meta property="og:title" content="Example World Tour (2026) directed by Person">'
            '<body><h1>Example World Tour</h1><p>2026</p></body></html>'
        )
        embed = (
            '<html><meta property="og:title" content="Example World Tour (2026)">'
            '<body><div class="film-poster"><img alt="Example World Tour poster" '
            'srcset="https://a.ltrbxd.com/poster-300.jpg 300w, '
            'https://a.ltrbxd.com/poster-1000.jpg 1000w"></div>'
            '<h1>Example World Tour</h1><p>2026</p></body></html>'
        )
        with patch.object(posters, "_fetch_text", side_effect=[main, embed]):
            meta = posters._letterboxd_page_metadata(ctx_url)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.poster_url, "https://a.ltrbxd.com/poster-1000.jpg")
        self.assertEqual(meta.release_year, 2026)

    def test_wikidata_fallback_progresses_to_generic_title_variant(self):
        ctx = self.context(
            display_title="Presenter's Example 20th Anniversary",
            canonical_title="Presenter's Example",
            expected_year=2006,
        )
        entity = self.entity("Q2", "Underlying Example", year=2006)
        entity.setdefault("claims", {})["P18"] = [
            {"mainsnak": {"datavalue": {"value": "Underlying Example poster.jpg"}}}
        ]
        with patch.object(
            posters,
            "_context_title_variants",
            return_value=["Presenter's Example", "Underlying Example"],
        ), patch.object(
            posters,
            "_wikidata_qids_for_title",
            side_effect=lambda title, limit=10: [] if title == "Presenter's Example" else ["Q2"],
        ), patch.object(posters, "_wikidata_entities", return_value=[entity]):
            image, page, source, _ = posters._image_via_wikidata(ctx, None)
        self.assertIn("Underlying_Example_poster.jpg", image)
        self.assertIsNone(page)
        self.assertEqual(source, "wikidata-commons")
        self.assertIn("width=800", image)

    def test_letterboxd_empty_poster_asset_is_rejected(self):
        html = (
            '<html><div class="film-poster"><img alt="Example poster" '
            'src="https://s.ltrbxd.com/static/img/empty-poster-1000-abc.png"></div></html>'
        )
        soup = BeautifulSoup(html, "html.parser")
        self.assertIsNone(posters._letterboxd_poster_url(soup))
        self.assertTrue(posters._is_placeholder(
            "Weekend Movies_files/empty-poster-1000-abc.webp"
        ))

    def test_letterboxd_validated_identity_can_use_linked_tmdb_poster(self):
        url = "https://letterboxd.com/film/example-world-tour/"
        main = (
            '<html><meta property="og:title" content="Example World Tour (2026)">'
            '<body><div class="film-poster"><img alt="Example World Tour poster" '
            'src="https://s.ltrbxd.com/static/img/empty-poster-1000-abc.png"></div>'
            '<a href="https://www.themoviedb.org/movie/123456/">TMDB</a>'
            '<h1>Example World Tour</h1><p>2026</p></body></html>'
        )
        embed = (
            '<html><meta property="og:title" content="Example World Tour (2026)">'
            '<body><h1>Example World Tour</h1><p>2026</p></body></html>'
        )
        tmdb = (
            '<html><meta property="og:image" '
            'content="https://image.tmdb.org/t/p/original/example-poster.jpg"></html>'
        )
        with patch.object(posters, "_fetch_text", side_effect=[main, embed, tmdb]):
            meta = posters._letterboxd_page_metadata(url)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.tmdb_url, "https://www.themoviedb.org/movie/123456/")
        self.assertEqual(meta.poster_url, "https://image.tmdb.org/t/p/original/example-poster.jpg")

    def test_previous_report_poster_reused_only_for_same_provider_identity(self):
        previous_html = (
            '<table><tbody><tr data-movie-id="1"><td>1</td><td>'
            '<div class="movie-cell-title">Example</div>'
            '<div class="movie-poster-wrap"><img class="movie-poster" '
            'data-poster-source="wikidata-enwiki" data-wikipedia-page="Example (film)" '
            'src="https://upload.wikimedia.org/example.jpg"></div></td>'
            '<td data-source-url="https://www.rottentomatoes.com/m/example_2026"></td>'
            '<td data-source-url="https://www.imdb.com/title/tt12345678/"></td>'
            '<td>AMC Example</td><td>1h 40m</td></tr></tbody></table>'
        )
        current = BeautifulSoup(
            '<tr data-movie-id="1"><td>1</td><td><div class="movie-cell-title">Example</div></td>'
            '<td data-source-url="https://www.rottentomatoes.com/m/example_2026"></td>'
            '<td></td><td>AMC Example</td><td>1h 40m</td></tr>',
            "html.parser",
        ).find("tr")
        wrong = BeautifulSoup(
            '<tr data-movie-id="1"><td>1</td><td><div class="movie-cell-title">Example</div></td>'
            '<td data-source-url="https://www.rottentomatoes.com/m/example_1999"></td>'
            '<td></td><td>AMC Example</td><td>1h 40m</td></tr>',
            "html.parser",
        ).find("tr")
        ctx = self.context(canonical_title="Example", runtime_min=100)
        with tempfile.TemporaryDirectory() as tmp:
            prior = Path(tmp) / "previous.html"
            prior.write_text(previous_html, encoding="utf-8")
            with patch.object(posters, "PREVIOUS_REPORT_PATH", prior):
                posters._PREVIOUS_POSTER_INDEX = None
                chosen = posters._previous_poster_for_row(current, ctx)
                posters._PREVIOUS_POSTER_INDEX = None
                rejected = posters._previous_poster_for_row(wrong, ctx)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["src"], "https://upload.wikimedia.org/example.jpg")
        # A different RT release cannot borrow the old poster merely because
        # the title/runtime happen to be the same.
        self.assertIsNone(rejected)

    def test_rt_default_poster_asset_is_rejected(self):
        html = (
            '<html><img alt="Poster for Example" '
            'src="https://resizing.flixster.com/5d84d010--rt-poster-default.gif"></html>'
        )
        soup = BeautifulSoup(html, "html.parser")
        self.assertIsNone(posters._rt_poster_url(soup))
        self.assertTrue(
            posters._is_placeholder(
                "Weekend Movies_files/5d84d010--rt-poster-default_test.gif"
            )
        )

    def test_music_event_letterboxd_identity_relaxes_runtime_cut_difference(self):
        row = BeautifulSoup(
            '<tr><td>1</td><td><div class="movie-cell-title">Example World Tour</div></td>'
            '<td></td><td></td><td></td><td>1h 37m</td></tr>',
            "html.parser",
        ).find("tr")
        ctx = posters._row_movie_context(row, "Example World Tour", 2026)
        self.assertTrue(ctx.relax_runtime)
        meta = posters.LetterboxdPageMetadata(
            title="Example World Tour",
            poster_url="https://a.ltrbxd.com/example.jpg",
            release_year=2025,
            runtime_min=160,
            imdb_id="tt12345678",
            url="https://letterboxd.com/film/example-world-tour/",
        )
        with patch.object(posters, "_letterboxd_direct_urls", return_value=[]), \
             patch.object(posters, "_letterboxd_search_urls", return_value=[meta.url]), \
             patch.object(posters, "_letterboxd_page_metadata", return_value=meta):
            chosen, rejected = posters._best_letterboxd_metadata(ctx, 2026)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.url, meta.url)
        self.assertFalse(any("runtime" in item for item in rejected))

    def test_missing_imdb_rating_backfill_uses_official_dataset_only(self):
        html = BeautifulSoup(
            '<table class="dataframe"><thead><tr><th>#</th><th>Movie</th><th>RT_C/A</th>'
            '<th>IMDb</th><th>Showtimes</th><th>Runtime</th></tr></thead><tbody>'
            '<tr data-movie-id="1"><td>1</td><td><div class="movie-cell-title">Example</div>'
            '<div class="movie-poster-wrap"><a><img class="movie-poster" src="x.jpg"></a></div></td>'
            '<td></td><td></td><td>AMC Example</td><td>1h 40m</td></tr>'
            '</tbody></table>',
            "html.parser",
        )
        with tempfile.TemporaryDirectory() as tmp:
            ratings = Path(tmp) / "title.ratings.tsv.gz"
            with gzip.open(ratings, "wt", encoding="utf-8", newline="") as handle:
                handle.write("tconst\taverageRating\tnumVotes\n")
                handle.write("tt12345678\t7.4\t123\n")
            with patch.object(posters, "IMDB_RATINGS_GZ", ratings), \
                 patch.object(posters, "_imdb_id_for_context", return_value="tt12345678"), \
                 patch.object(posters, "_rt_metadata_for_context", return_value=None):
                changed = posters._fill_missing_imdb_scores(html, 2026)
        self.assertEqual(changed, 1)
        cell = html.find("tbody").find("tr").find_all("td", recursive=False)[3]
        self.assertEqual(cell.get_text(strip=True), "7.4")
        self.assertIn("tt12345678", cell.get("data-source-url", ""))

    def test_imdb_backfill_leaves_score_blank_when_dataset_has_no_rating(self):
        html = BeautifulSoup(
            '<table class="dataframe"><thead><tr><th>#</th><th>Movie</th><th>RT_C/A</th>'
            '<th>IMDb</th><th>Showtimes</th><th>Runtime</th></tr></thead><tbody>'
            '<tr data-movie-id="1"><td>1</td><td><div class="movie-cell-title">Example</div>'
            '<div class="movie-poster-wrap"><a><img class="movie-poster" src="x.jpg"></a></div></td>'
            '<td></td><td></td><td>AMC Example</td><td>1h 40m</td></tr>'
            '</tbody></table>',
            "html.parser",
        )
        with tempfile.TemporaryDirectory() as tmp:
            ratings = Path(tmp) / "title.ratings.tsv.gz"
            with gzip.open(ratings, "wt", encoding="utf-8", newline="") as handle:
                handle.write("tconst\taverageRating\tnumVotes\n")
            with patch.object(posters, "IMDB_RATINGS_GZ", ratings), \
                 patch.object(posters, "_imdb_id_for_context", return_value="tt12345678"), \
                 patch.object(posters, "_rt_metadata_for_context", return_value=None):
                changed = posters._fill_missing_imdb_scores(html, 2026)
        self.assertEqual(changed, 0)
        cell = html.find("tbody").find("tr").find_all("td", recursive=False)[3]
        self.assertEqual(cell.get_text(strip=True), "")
        self.assertIn("tt12345678", cell.get("data-source-url", ""))

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



class PostprocessProviderIdentityTests(unittest.TestCase):
    def test_show_blob_scoring_is_year_agnostic(self):
        older = "AMC Example 10\n• 2026-10-18: 7:00 pm"
        future = "AMC Example 10\n• 2031-10-18: 7:00 pm"
        self.assertEqual(postprocess.score_show_blob(older), postprocess.score_show_blob(future))

    def test_blank_score_preserves_resolved_source_url_without_visible_link(self):
        soup = BeautifulSoup("<html></html>", "html.parser")
        td = postprocess._linked_score_td(
            soup, "", "https://www.example.com/movie/example"
        )
        self.assertEqual(td.get_text(" ", strip=True), "")
        self.assertIsNone(td.find("a"))
        self.assertEqual(
            td.get("data-source-url"), "https://www.example.com/movie/example"
        )


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

    def test_music_concert_event_title_is_generic_category(self):
        result = posters.classify_audience_focus(
            self.context(title="Example Artist World Tour: Live Viewing"),
            None,
        )
        self.assertIn("Music/Concert", result.tags)

    def test_language_version_suffixes_are_generic_audience_evidence(self):
        cases = {
            "Echoes of Spring (Telugu)": "Indian",
            "The Final Bridge [Hindi Dubbed]": "Indian",
            "Broken Lantern - Malayalam": "Indian",
            "An Evening in the City (Korean)": "Korean",
            "A Silent Place (Mandarin Chinese Version)": "Chinese",
            "Paper Garden (Japanese with English subtitles)": "Japanese",
            "A Bright Star (Spanish-language)": "Spanish-language",
            "A Blue Lake (Filipino)": "Filipino",
            "Future Landing (Telugu) (2033)": "Indian",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                result = posters.classify_audience_focus(self.context(title=title))
                self.assertIn(expected, result.tags)
                self.assertTrue(any("AMC title version qualifier" in reason for reason in result.reasons))

    def test_language_words_in_regular_movie_titles_do_not_add_market_labels(self):
        # Do not confuse story titles, adjectives, settings, or arbitrary
        # parenthetical descriptions with explicit language/version metadata.
        ordinary_titles = (
            "A Korean Adventure", "Japanese Gardens", "The Spanish Prisoner",
            "Lost in Hindi Translation", "A Small Town (Japanese History)",
            "Stars Over India - A Telugu Story", "My Film (English)",
        )
        for title in ordinary_titles:
            with self.subTest(title=title):
                result = posters.classify_audience_focus(self.context(title=title))
                self.assertNotIn("Indian", result.tags)
                self.assertNotIn("Korean", result.tags)
                self.assertNotIn("Japanese", result.tags)
                self.assertNotIn("Spanish-language", result.tags)

    def test_kids_focused_from_explicit_wikidata_audience_or_genre(self):
        # Film-level intent, not its certification or suitability for families.
        cases = (
            "a children's animated fantasy film",
            "a film made for preschoolers",
            "a feature film aimed at young children",
            "a kids' educational movie",
        )
        for description in cases:
            with self.subTest(description=description):
                result = posters.classify_audience_focus(
                    self.context(), self.entity(description=description)
                )
                self.assertIn("Kids-focused", result.tags)
                self.assertTrue(any("Wikidata" in reason for reason in result.reasons))

        posters._LABEL_CACHE["Q2099"] = "children's film"
        entity = self.entity(claims={
            "P136": [{"mainsnak": {"datavalue": {"value": {"id": "Q2099"}}}}]
        })
        self.assertIn("Kids-focused", posters.classify_audience_focus(self.context(), entity).tags)

    def test_kids_focused_from_verified_wikipedia_context(self):
        descriptions = (
            "British children's adventure films | 2027 films",
            "The feature is targeted at preschoolers.",
            "A movie designed for children.",
        )
        for focus_text in descriptions:
            with self.subTest(focus_text=focus_text):
                result = posters.classify_audience_focus(
                    self.context(), self.entity(), supplemental_focus_text=focus_text
                )
                self.assertIn("Kids-focused", result.tags)
                self.assertTrue(any("Wikipedia" in reason for reason in result.reasons))

    def test_kids_focused_uses_verified_rt_kids_genre_but_not_family_alone(self):
        # Kids & Family is an explicit child-audience category at a matched RT
        # movie URL; "Family" alone is only a suitability/general genre cue.
        for genre in ("kids & family", "Kids and Family", "children's film"):
            with self.subTest(genre=genre):
                result = posters.classify_audience_focus(
                    self.context(), None, supplemental_genres=frozenset({genre})
                )
                self.assertIn("Kids-focused", result.tags)
                self.assertTrue(any("Rotten Tomatoes" in x for x in result.reasons))
        for genre in ("family", "family-friendly", "animation", "adventure", "pg"):
            with self.subTest(genre=genre):
                self.assertNotIn("Kids-focused", posters.classify_audience_focus(
                    self.context(), None, supplemental_genres=frozenset({genre})
                ).tags)

    def test_kids_focused_excludes_merely_family_friendly_or_child_characters(self):
        # Conservative: animation, family genres, ratings and adapted children's
        # books alone do not establish the film's intended primary audience.
        cases = (
            ("family-friendly animated comedy film", "family movie"),
            ("PG-rated cartoon feature", "animated family adventure film"),
            ("film featuring children and their parents", "children are its protagonists"),
            ("adaptation of a children's book", "based on children's literature"),
            ("American family film", "Family films | 2027 animated films"),
        )
        for description, wiki_text in cases:
            with self.subTest(description=description):
                result = posters.classify_audience_focus(
                    self.context(title="Happy Children in a Park"),
                    self.entity(description=description),
                    supplemental_focus_text=wiki_text,
                    supplemental_genres=frozenset({"family", "animation"}),
                )
                self.assertNotIn("Kids-focused", result.tags)

    def test_kids_focus_coexists_with_other_audience_tags(self):
        result = posters.classify_audience_focus(
            self.context(title="Example (Hindi)"),
            self.entity(description="a children's animated musical film"),
            supplemental_languages=frozenset({"hindi"}),
            supplemental_genres=frozenset({"music"}),
        )
        self.assertIn("Kids-focused", result.tags)
        self.assertIn("Indian", result.tags)
        self.assertIn("Music/Concert", result.tags)

    def test_rt_language_and_genre_enrich_audience_focus(self):
        result = posters.classify_audience_focus(
            self.context(),
            None,
            supplemental_languages=frozenset({"japanese"}),
            supplemental_genres=frozenset({"music"}),
        )
        self.assertIn("Japanese", result.tags)
        self.assertIn("Music/Concert", result.tags)

    def test_exact_rt_metadata_supplies_poster_language_and_genre(self):
        ctx = self.context(title="Example Feature")
        html = """
        <html><head><meta property="og:title" content="Example Feature | Rotten Tomatoes"></head>
        <body><h1>Example Feature</h1>
        <img alt="Poster for Example Feature" src="https://resizing.flixster.com/example-poster.jpg">
        <div>Genre</div><div>Music</div>
        <div>Original Language</div><div>Japanese</div>
        <div>Release Date (Theaters)</div><div>Oct 18, 2026</div>
        </body></html>
        """
        with patch.object(posters, "_fetch_text", return_value=html):
            meta = posters._rt_metadata_for_context(
                "https://www.rottentomatoes.com/m/example_feature", ctx
            )
        self.assertIsNotNone(meta)
        self.assertEqual(meta.poster_url, "https://resizing.flixster.com/example-poster.jpg")
        self.assertEqual(meta.languages, frozenset({"japanese"}))
        self.assertEqual(meta.genres, frozenset({"music"}))

    def test_verified_kids_genre_reaches_final_report_column(self):
        # A matched RT movie genre travels through the full report enrichment,
        # rather than existing only as a directly invoked classifier rule.
        html = """<table class='dataframe'><thead><tr>
          <th>Movie</th><th>RT_C/A</th><th>IMDb</th><th>Showtimes</th><th>Runtime</th>
        </tr></thead><tbody><tr>
          <td><div class='movie-cell-title'>Example Feature</div></td>
          <td data-source-url='https://www.rottentomatoes.com/m/example_feature'>90/80</td>
          <td data-source-url='https://www.imdb.com/title/tt1234567/'>7.4</td>
          <td>AMC Example 10<br/>• 2026-10-24: 7:00 pm</td><td>1h 49m</td>
        </tr></tbody></table>"""
        soup = BeautifulSoup(html, "html.parser")
        metadata = posters.RtPageMetadata(
            title="Example Feature", poster_url=None,
            languages=frozenset({"english"}), genres=frozenset({"kids & family", "animation"}),
            release_year=2026, runtime_min=109,
        )
        with patch.object(posters, "_rt_metadata_for_context", return_value=metadata), \
             patch.object(posters, "_audience_entity_for_context", return_value=None), \
             patch.object(posters, "_wikipedia_focus_pages", return_value={}), \
             patch.object(posters, "_prior_verified_language_index", return_value={}):
            self.assertEqual(posters._add_audience_focus_column(soup, 2026), 1)
        focus = soup.select_one("td.audience-focus-cell")
        self.assertIn("Kids-focused", focus["data-audience-focus"])
        self.assertIn("Rotten Tomatoes", focus["data-audience-reasons"])

    def test_hidden_imdb_source_url_remains_identity_bridge(self):
        soup = BeautifulSoup(
            '<tr><td data-source-url="https://www.imdb.com/title/tt1234567/"></td></tr>',
            "html.parser",
        )
        self.assertEqual(posters._extract_imdb_id(soup.tr), "tt1234567")

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

    def test_children_tag_in_full_report_from_wikipedia_genre(self):
        # Verify the batched page-context path actually reaches the public
        # Audience Focus cell; do not classify a merely family-friendly film.
        html = (
            '<html><head></head><body><table class="dataframe"><thead><tr>'
            '<th>#</th><th>Movie</th><th>RT_C/A</th><th>IMDb</th><th>Showtimes</th><th>Runtime</th>'
            '</tr></thead><tbody><tr data-movie-id="1"><td>1</td>'
            '<td><div class="movie-cell-title">The Little Adventure</div>'
            '<img class="movie-poster" data-wikipedia-page="The Little Adventure (film)" src="a.jpg"></td>'
            '<td>85/92</td><td>7</td><td>AMC Test<br>• 2026-09-20: 12:00 pm</td>'
            '<td>1h 20m</td></tr></tbody></table></body></html>'
        )
        soup = BeautifulSoup(html, "html.parser")
        context_page = {
            "the little adventure (film)": {
                "title": "The Little Adventure (film)",
                "extract": "The Little Adventure is a 2026 children's animated adventure film.",
                "categories": [{"title": "Category:2026 animated films"}],
            }
        }
        with patch.object(posters, "_audience_entity_for_context", return_value=self.entity()), \
             patch.object(posters, "_wikipedia_focus_pages", return_value=context_page), \
             patch.object(posters, "_rt_metadata_for_context", return_value=None), \
             patch.object(posters, "_prior_verified_language_index", return_value={}):
            self.assertEqual(posters._add_audience_focus_column(soup, 2026), 1)
        cell = soup.select_one(".audience-focus-cell")
        self.assertIn("Kids-focused", cell["data-audience-focus"])
        self.assertIn("Wikipedia", cell["data-audience-reasons"])

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


class PreviousLanguageRecoveryTests(unittest.TestCase):
    """Persist provider-verified language labels, never guesses or identities."""

    @staticmethod
    def row(title="Example Harbor", imdb="tt12345678", rt="example_harbor_2026", runtime="1h 40m",
            focus="", reason=""):
        imdb_url = f"https://www.imdb.com/title/{imdb}/" if imdb else ""
        rt_url = f"https://www.rottentomatoes.com/m/{rt}" if rt else ""
        focus_cell = (
            f'<td class="audience-focus-cell" data-audience-focus="{focus}" '
            f'data-audience-reasons="{reason}"></td>'
        ) if focus else ""
        html = (
            f'<tr data-movie-id="1"><td>1</td><td><div class="movie-cell-title">{title}</div></td>'
            f'<td data-source-url="{rt_url}"></td><td data-source-url="{imdb_url}"></td>'
            f'<td>AMC Example</td><td>{runtime}</td>{focus_cell}</tr>'
        )
        return BeautifulSoup(html, "html.parser").find("tr")

    def recover(self, previous, current, *, entity=None, rt_metadata=None, title="Example Harbor"):
        with tempfile.TemporaryDirectory() as tmp:
            previous_report = Path(tmp) / "previous.html"
            previous_report.write_text(
                '<table><tbody>' + str(previous) + '</tbody></table>', encoding="utf-8"
            )
            with patch.object(posters, "PREVIOUS_REPORT_PATH", previous_report):
                index = posters._prior_verified_language_index()
        cells = current.find_all(["th", "td"], recursive=False)
        context = posters.MovieContext(
            display_title=title, canonical_title=title, expected_year=2026,
            runtime_min=posters._runtime_minutes(cells[5].get_text(" ", strip=True)),
            spoken_languages=frozenset(), relax_runtime=False,
        )
        fresh = posters.AudienceFocusResult(
            tags=("General",), reasons=("No strong niche-audience signal found",)
        )
        return posters._retain_previous_verified_language_focus(
            current, context, fresh, index, entity=entity, rt_metadata=rt_metadata
        )

    def test_verified_wikidata_language_recovers_with_same_imdb_id(self):
        prior = self.row(focus="Filipino", reason="Filipino: Wikidata original language filipino")
        recovered = self.recover(prior, self.row(rt=None))
        self.assertEqual(recovered.tags, ("Filipino",))
        self.assertIn("Wikidata original language filipino", recovered.reasons[0])
        self.assertIn("retained from previous report", recovered.reasons[0])

    def test_rt_identity_can_recover_without_imdb(self):
        prior = self.row(imdb=None, focus="Korean", reason="Korean: Rotten Tomatoes original language korean")
        recovered = self.recover(prior, self.row(imdb=None))
        self.assertEqual(recovered.tags, ("Korean",))

    def test_different_imdb_or_rt_identity_rejects_fallback(self):
        prior = self.row(focus="Filipino", reason="Filipino: Wikidata original language filipino")
        for current in (self.row(imdb="tt99999999"), self.row(rt="unrelated_film")):
            with self.subTest(current=str(current)):
                self.assertEqual(self.recover(prior, current).tags, ("General",))

    def test_identity_only_title_or_runtime_cannot_recover(self):
        prior = self.row(focus="Japanese", reason="Japanese: AMC lists japanese")
        for current, title in (
            (self.row(imdb=None, rt=None), "Example Harbor"),
            (self.row(title="Another Harbor"), "Another Harbor"),
            (self.row(runtime="2h 25m"), "Example Harbor"),
        ):
            with self.subTest(title=title, runtime=current.get_text()):
                self.assertEqual(self.recover(prior, current, title=title).tags, ("General",))

    def test_general_country_only_or_thematic_classifications_are_not_reused(self):
        for tag, reason in (
            ("Indian", "Indian: country of origin india"),
            ("Faith-oriented", "Faith-oriented: Angel Studios production/distribution signal"),
            ("Filipino", "Filipino: inferred from title"),
            ("General", "No strong niche-audience signal found"),
        ):
            with self.subTest(tag=tag):
                prior = self.row(focus=tag, reason=reason)
                self.assertEqual(self.recover(prior, self.row()).tags, ("General",))

    def test_live_language_metadata_or_existing_market_tag_takes_precedence(self):
        prior = self.row(focus="Filipino", reason="Filipino: Wikidata original language filipino")
        english = posters.RtPageMetadata(
            title="Example Harbor", poster_url=None,
            languages=frozenset({"english"}), genres=frozenset(),
            release_year=2026, runtime_min=100,
        )
        self.assertEqual(self.recover(prior, self.row(), rt_metadata=english).tags, ("General",))
        entity = {"claims": {"P364": [
            {"mainsnak": {"datavalue": {"value": {"id": "QEnglish"}}}}
        ]}}
        with patch.object(posters, "_entity_languages", return_value={"english"}):
            self.assertEqual(self.recover(prior, self.row(), entity=entity).tags, ("General",))

    def test_repeated_runs_do_not_accumulate_provenance_suffixes(self):
        first = self.row(focus="Filipino", reason="Filipino: Wikidata original language filipino")
        second = self.recover(first, self.row())
        reused = self.row(focus="|".join(second.tags), reason=" | ".join(second.reasons))
        third = self.recover(reused, self.row())
        self.assertEqual(third.tags, ("Filipino",))
        self.assertEqual(third.reasons[0].count("retained from previous report"), 1)

    def test_integration_adds_language_without_extra_provider_calls(self):
        prior = self.row(focus="Filipino", reason="Filipino: Wikidata original language filipino")
        html = (
            '<html><head></head><body><table class="dataframe"><thead><tr>'
            '<th>#</th><th>Movie</th><th>RT</th><th>IMDb</th><th>Showtimes</th><th>Runtime</th>'
            '</tr></thead><tbody>' + str(self.row()) + '</tbody></table></body></html>'
        )
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'previous.html'
            file.write_text('<table><tbody>' + str(prior) + '</tbody></table>', encoding='utf-8')
            with patch.object(posters, 'PREVIOUS_REPORT_PATH', file), \
                 patch.object(posters, '_audience_entity_for_context', return_value=None), \
                 patch.object(posters, '_rt_metadata_for_context', return_value=None), \
                 patch.object(posters, '_wikipedia_focus_pages', return_value={}):
                soup = BeautifulSoup(html, 'html.parser')
                count = posters._add_audience_focus_column(soup, 2026)
        self.assertEqual(count, 1)
        focus = soup.select_one('td.audience-focus-cell')
        self.assertEqual(focus.get('data-audience-focus'), 'Filipino')
        self.assertIn('retained from previous report', focus.get('title'))


class AMCShowtimeFetchDateTests(unittest.TestCase):
    """Dates must travel across report generations without being backdated."""

    def setUp(self):
        self.a = ("amc orange 30", "2026-10-24")
        self.b = ("amc tustin 14 at the district", "2026-10-25")
        self.showings = [
            {"movie_id": 1, "movie": "Sample", "theater": "AMC Orange 30", "date": "2026-10-24"},
            {"movie_id": 1, "movie": "Sample", "theater": "AMC Tustin 14 @ The District", "date": "2026-10-25"},
        ]

    def test_recovered_cached_dates_keep_original_timestamp(self):
        previous = {self.a: "2026-10-22", self.b: "2026-10-23"}
        live = {self.a: "2026-10-24"}
        resolved = postprocess.resolve_showtime_fetch_dates(
            self.showings, {self.a, self.b}, previous, live
        )
        self.assertEqual(resolved[self.a], "2026-10-22")
        self.assertEqual(resolved[self.b], "2026-10-23")
        # If another run later inherits this same date, the older date persists.
        again = postprocess.resolve_showtime_fetch_dates(
            self.showings, {self.a, self.b}, resolved, {}
        )
        self.assertEqual(again, resolved)

    def test_fresh_replaces_old_but_unknown_stays_old(self):
        resolved = postprocess.resolve_showtime_fetch_dates(
            self.showings, {self.b}, {self.a: "2026-10-21"}, {self.a: "2026-10-24"}
        )
        self.assertEqual(resolved[self.a], "2026-10-24")
        self.assertIsNone(resolved[self.b])

    def test_reads_previous_report_payload_and_rejects_bad_dates(self):
        data = {"amc_fetch_dates": [
            {"theater": "AMC Orange 30", "date": "2026-10-24", "fetched_on": "2026-10-22"},
            {"theater": "AMC Tustin 14 @ The District", "date": "2026-10-25", "fetched_on": "2026-13-55"},
        ]}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "old.html"
            p.write_text('<html><script id="showtimes-data" type="application/json">'
                         + json.dumps(data) + '</script></html>', encoding="utf-8")
            prior = postprocess.read_previous_fetch_dates(p, "2026-10-24")
            self.assertEqual(prior, {self.a: "2026-10-22"})
            data["amc_fetch_dates"][0]["fetched_on"] = "2026-10-30"
            p.write_text('<script id="showtimes-data" type="application/json">'
                         + json.dumps(data) + '</script>', encoding="utf-8")
            self.assertEqual(postprocess.read_previous_fetch_dates(p, "2026-10-24"), {})
            p.write_text('<script id="showtimes-data" type="application/json">'
                         + json.dumps({"showtimes": self.showings}) + '</script>', encoding="utf-8")
            self.assertEqual(postprocess.read_previous_fetch_dates(p, "2026-10-24"), {})

    def test_live_fetch_markers_survive_workflow_retry(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "amc-orange-30-2026-10-24.json"
            p.write_text(json.dumps({"theater": "AMC Orange 30", "date": "2026-10-24",
                                     "fetched_on": "2026-10-23"}), encoding="utf-8")
            dates = postprocess.read_workflow_fetch_dates(Path(d), "2026-10-24")
            self.assertEqual(dates[self.a], "2026-10-23")
            (Path(d) / "corrupt.json").write_text("not json", encoding="utf-8")
            self.assertEqual(postprocess.read_workflow_fetch_dates(Path(d), "2026-10-24"), dates)

    def test_current_run_amc_hides_badge_but_preserves_cached_and_scenef(self):
        soup = BeautifulSoup("<html></html>", "html.parser")
        td = BeautifulSoup(
            "<td>AMC Orange 30<br/>• 2026-10-24: 7:00 pm⛔<br/>"
            "AMC Tustin 14 @ The District<br/>• 2026-10-25: 8:00 pm</td>",
            "html.parser",
        ).td
        before = td.get_text("\n", strip=True)
        postprocess.label_showtime_fetch_dates(
            soup, td, {self.a: "2026-10-24", self.b: "2026-10-22"},
            current_live_combos={self.a},
        )
        self.assertEqual(td.get_text("\n", strip=True), before)
        labels = td.select(".showtime-fetch-date")
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["data-label"], "Fetched 10/22/26")
        self.assertTrue(postprocess.parse_showtimes_blob(before)[0]["a_list_excluded"])

        td2 = BeautifulSoup(
            "<td>AMC Orange 30<br/>• 2026-10-24: 7:00 pm</td>", "html.parser"
        ).td
        postprocess.label_showtime_fetch_dates(
            soup, td2, {self.a: None},
            scenef_provenance={self.a: {"verified_on": "2026-10-24", "scenef_fetched_on": "2026-10-24"}},
            current_live_combos={self.a},
        )
        self.assertEqual(td2.select_one(".showtime-fetch-date")["data-source"], "scenef")

    def test_rendered_labels_preserve_original_blob_for_cache_and_a_list(self):
        soup = BeautifulSoup("<html></html>", "html.parser")
        td = soup.new_tag("td")
        original = (
            "AMC Orange 30<br/>• 2026-10-24: 7:00 pm⛔ [Laser at AMC]<br/>"
            "AMC Tustin 14 @ The District<br/>• 2026-10-25: 8:00 pm"
        )
        td.append(BeautifulSoup(original, "html.parser"))
        # A parsed HTML fragment under a soup can have children nested inside
        # that fragment; BeautifulSoup stringification/reparse mimics table cells.
        td = BeautifulSoup(f"<td>{original}</td>", "html.parser").td
        before = td.get_text("\n", strip=True)
        postprocess.label_showtime_fetch_dates(soup, td, {self.a: "2026-10-23", self.b: None})
        self.assertEqual(td.get_text("\n", strip=True), before)
        self.assertEqual(len(td.select(".showtime-fetch-date")), 2)
        self.assertIn("max-width: 100%", postprocess.CSS)
        self.assertIn("white-space: normal", postprocess.CSS)
        self.assertNotIn(".showtimes-freshness{", postprocess.CSS)
        self.assertEqual(td.select(".showtime-fetch-date")[0]["data-label"], "Fetched 10/23/26")
        self.assertEqual(td.select(".showtime-fetch-date")[1]["data-label"], "Old")
        self.assertIn("2026-10-23", td.select(".showtime-fetch-date")[0]["title"])
        self.assertIn("unknown", td.select(".showtime-fetch-date")[1]["aria-label"])
        parsed = postprocess.parse_showtimes_blob(td.get_text("\n", strip=True))
        self.assertEqual(len(parsed), 2)
        self.assertTrue(parsed[0]["a_list_excluded"])

    def test_postprocess_footer_and_serialized_metadata(self):
        html = """<html><head></head><body><main>
            <table class="dataframe"><thead><tr>
              <th>movie_title</th><th>rt_c/a</th><th>imdb_rating</th><th>showtimes</th><th>runtime</th>
            </tr></thead><tbody><tr>
              <td>Test Feature</td><td>81/70</td><td>7.2</td>
              <td>AMC Orange 30<br/>• 2026-10-24: 7:00 pm⛔ [Laser at AMC]<br/>
              AMC Tustin 14 @ The District<br/>• 2026-10-25: 8:00 pm</td><td>1h 45m</td>
            </tr></tbody></table></main></body></html>"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "docs").mkdir()
            (root / "build/amc_live_fetch_dates").mkdir(parents=True)
            (root / "docs/index.html").write_text(html, encoding="utf-8")
            (root / "build/amc_previous_report_fallback.txt").write_text(
                "AMC Tustin 14 @ The District | 2026-10-25\n", encoding="utf-8"
            )
            (root / "build/amc_live_fetch_dates/live.json").write_text(json.dumps({
                "theater": "AMC Orange 30", "date": "2026-10-24", "fetched_on": "2026-10-24"
            }), encoding="utf-8")
            (root / "build/previous_report.html").write_text(
                '<script id="showtimes-data" type="application/json">'
                + json.dumps({"amc_fetch_dates": [
                    {"theater": "AMC Tustin 14 @ The District", "date": "2026-10-25", "fetched_on": "2026-10-22"}
                ]}) + '</script>', encoding="utf-8"
            )
            oldcwd = Path.cwd()
            try:
                os.chdir(root)
                with patch.object(postprocess, 'HTML_PATH', Path('docs/index.html')), \
                     patch.object(postprocess, 'wikipedia_thumbnail_url', return_value=None):
                    postprocess.main()
            finally:
                os.chdir(oldcwd)
            page = BeautifulSoup((root / 'docs/index.html').read_text(encoding='utf-8'), 'html.parser')
            js = json.loads(page.find('script', id='showtimes-data').string)
            prov = {(x['theater'], x['date']): x['fetched_on'] for x in js['amc_fetch_dates']}
            self.assertEqual(prov[self.a], "2026-10-24")
            self.assertEqual(prov[self.b], "2026-10-22")
            labels = page.select('.showtime-fetch-date')
            # The current-run AMC label is intentionally invisible, but its
            # exact fetch date is retained in serialized JSON for later runs.
            self.assertEqual(len(labels), 1)
            self.assertEqual(labels[0]['data-label'], 'Fetched 10/22/26')
            warning = page.find(id='amc-cache-notice')
            self.assertEqual(warning.name, 'details')
            self.assertTrue(warning.parent.name in {'main', 'body'})
            self.assertGreater(list(warning.parent.descendants).index(warning),
                               list(warning.parent.descendants).index(page.find('table')))
            self.assertIsNone(warning.get('open'))


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



class SceneFFallbackTests(unittest.TestCase):
    """No actual SceneF network access: contract-fixture and failure tests."""

    def _rows(self, mapping):
        return [{"movie_title": title, "show_time": at} for title, times in mapping.items() for at in times]

    def test_only_strict_improvement_can_replace_cached_schedule(self):
        from scripts.scenef_showtimes import improves_coverage
        old = self._rows({"Test Movie A": ["1:00 pm", "6:00 pm"],
                          "Test Movie B": ["2:00 pm"]})
        sparse = self._rows({"Test Movie A": ["1:00 pm"]})
        equal = list(old)
        better = old + self._rows({"Test Movie B": ["9:00 pm"]})
        missing_film = self._rows({"Test Movie A": ["1:00 pm", "6:00 pm", "9:00 pm", "10:00 pm"]})
        self.assertFalse(improves_coverage(sparse, old))
        self.assertFalse(improves_coverage(equal, old))
        self.assertFalse(improves_coverage(missing_film, old))
        self.assertTrue(improves_coverage(better, old))
        # Repeated records, including format variations, aren't more showtimes.
        self.assertFalse(improves_coverage(old + [dict(old[0])], old))
        self.assertFalse(improves_coverage(self._rows({"One Movie": ["8:00 pm"]}), []))
        self.assertTrue(improves_coverage(self._rows({
            "One Movie": ["1:00 pm", "8:00 pm"], "Second": ["4:00 pm"]
        }), []))

    def _feed(self, *, verified="2026-10-24T14:00:00Z", name="AMC Orange 30"):
        return {
            "region": "orangecounty", "timezone": "America/Los_Angeles",
            "data_as_of": "2026-10-24T15:00:00Z",
            "venues": [{"id": "amc-orange", "name": name}],
            "films": [{"key": "film-a", "title": "Test Movie A", "runtimeMin": 114},
                      {"key": "film-b", "title": "Test Movie B", "runtimeMin": 95}],
            "screenings": [
                {"venueId": "amc-orange", "filmKey": "film-a",
                 "startsAt": "2026-10-25T19:00:00-07:00", "nightOf": "2026-10-25",
                 "verified_at": verified, "confidence": "single-source",
                 "source_tier": "licensed-feed", "freshness": {"status": "current", "source_status": "ok"}},
                {"venueId": "amc-orange", "filmKey": "film-b",
                 "startsAt": "2026-10-25T21:00:00-07:00", "nightOf": "2026-10-25",
                 "verified_at": verified, "confidence": "single-source",
                 "source_tier": "licensed-feed", "freshness": {"status": "current", "source_status": "ok"}},
            ],
        }

    def test_contract_parser_rejects_wrong_date_venue_and_staleness(self):
        import copy
        from datetime import timezone
        from scripts.scenef_showtimes import parse_combo
        now = datetime(2026, 10, 24, 16, 0, tzinfo=timezone.utc)
        rows, meta = parse_combo(self._feed(), "AMC Orange 30", "2026-10-25", now=now)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["show_date"], "2026-10-25")
        self.assertTrue(all(r["scenef_alist_unverified"] for r in rows))
        self.assertIn('scenef.com', meta['scenef_url'])
        self.assertEqual(parse_combo(self._feed(), "AMC Woodbridge 5", "2026-10-25", now=now)[0], [])
        self.assertEqual(parse_combo(self._feed(), "AMC Orange 30", "2026-10-24", now=now)[0], [])
        stale = copy.deepcopy(self._feed(verified="2026-10-15T00:00:00Z"))
        self.assertEqual(parse_combo(stale, "AMC Orange 30", "2026-10-25", now=now)[0], [])
        wrong = copy.deepcopy(self._feed())
        wrong['screenings'][0]['confidence'] = 'disputed'
        wrong['screenings'][1]['freshness']['retained'] = True
        self.assertEqual(parse_combo(wrong, "AMC Orange 30", "2026-10-25", now=now)[0], [])
        # Two chain candidates with the same name must not choose randomly.
        ambiguous = copy.deepcopy(self._feed())
        ambiguous['venues'].append({"id":"amc-orange-other", "name":"AMC Orange 30"})
        self.assertEqual(parse_combo(ambiguous, "AMC Orange 30", "2026-10-25", now=now)[0], [])

    def test_scenef_provenance_preserves_third_party_identity_across_cache(self):
        showing = [{"theater": "AMC Orange 30", "date": "2026-10-25"}]
        key = postprocess._combo_key('AMC Orange 30', '2026-10-25')
        today = '2026-10-25'
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source_dir = root / 'scenef'
            source_dir.mkdir()
            record = {"provider": "scenef", "theater": "AMC Orange 30", "date": today,
                      "fetched_on": today, "verified_at": "2026-10-24T14:00:00Z",
                      "scenef_url": "https://scenef.com/theater/amc-orange?region=orangecounty"}
            (source_dir / 'accepted.json').write_text(json.dumps(record), encoding='utf-8')
            current = postprocess.read_scenef_provenance(source_dir, today)
            self.assertEqual(current[key]['verified_on'], '2026-10-24')
            self.assertEqual(postprocess.resolve_scenef_provenance(showing, set(), {}, current, {}), current)
            prior_file = root / 'previous.html'
            prior_file.write_text('<script id="showtimes-data" type="application/json">'
                                  + json.dumps({'amc_fetch_dates': [dict(record, verified_on='2026-10-24')]})
                                  + '</script>', encoding='utf-8')
            previous = postprocess.read_previous_scenef_provenance(prior_file, today)
            self.assertEqual(postprocess.resolve_scenef_provenance(showing, {key}, previous, {}, {}), previous)
            # A fresh AMC fetch takes precedence over any old SceneF metadata.
            self.assertFalse(postprocess.resolve_scenef_provenance(showing, {key}, previous, current,
                                                                   {key: '2026-10-25'}))
            td = BeautifulSoup('<td>AMC Orange 30<br/>• 2026-10-25: 7:00 pm</td>', 'html.parser').td
            before = td.get_text('\n', strip=True)
            soup = BeautifulSoup('<html></html>', 'html.parser')
            postprocess.label_showtime_fetch_dates(soup, td, {key: None}, current)
            self.assertEqual(before, td.get_text('\n', strip=True))
            self.assertEqual(td.select_one('.showtime-fetch-date')['data-label'], 'SceneF 10/24/26')
            self.assertIn('verified 2026-10-24', td.select_one('.showtime-fetch-date')['title'])

    def test_blocked_amc_uses_scenef_only_when_replacement_is_richer(self):
        from scripts import scenef_showtimes
        source = NotebookPatcherTests()._patched_source()
        ns = {}
        exec(compile(source, "<scenef-live-fallback-test>", "exec"), ns)
        scraper = ns['scrape_amc_showtimes_for_date']
        ns['fetch_html_with_browser'] = lambda url, params=None, timeout_ms=30000: (200, '<html>Access Denied</html>')
        movie_times = [
            ('Example Feature A', '7:00 pm', True),
            ('Example Feature B', '8:00 pm', False),
        ]
        base = [
            {"movie_title": t, "theatre": "AMC Orange 30", "show_date": "2026-10-25",
             "show_time": clock, "a_list_excluded": excluded}
            for t, clock, excluded in movie_times
        ]
        extra = {"movie_title": "Example Feature C", "theatre": "AMC Orange 30",
                 "show_date": "2026-10-25", "show_time": "9:00 pm", "a_list_excluded": False}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'docs').mkdir()
            (root / 'docs/last_run_utc.txt').write_text(datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'))
            payload = {'showtimes': [
                {'movie': r['movie_title'], 'theater': 'AMC Orange 30', 'date': '2026-10-25',
                 'start': r['show_time'], 'a_list_excluded': r['a_list_excluded']}
                for r in base
            ]}
            (root / 'docs/index.html').write_text('<html><script id="showtimes-data" type="application/json">'
                                                  + json.dumps(payload) + '</script></html>')
            meta = {'verified_at': '2026-10-25T13:00:00Z',
                    'scenef_url': 'https://scenef.com/theater/amc-orange?region=orangecounty'}
            oldcwd = Path.cwd()
            try:
                os.chdir(root)
                with patch.object(scenef_showtimes, 'get_combo', return_value=(list(base), meta)):
                    rows = scraper(None, 'AMC Orange 30', 'https://example.invalid', ns['date'](2026, 10, 25))
                self.assertEqual(len(rows), 2)
                self.assertTrue((root/'build/amc_previous_report_fallback.txt').exists())
                # The second isolated run sees a fuller SceneF feed.
                (root/'build/amc_previous_report_fallback.txt').unlink()
                with patch.object(scenef_showtimes, 'get_combo', return_value=(base+[extra], dict(meta))):
                    rows = scraper(None, 'AMC Orange 30', 'https://example.invalid', ns['date'](2026, 10, 25))
                self.assertEqual(len(rows), 3)
                self.assertTrue(any(r['a_list_excluded'] for r in rows))
                self.assertEqual(ns['_AMC_LIVE_METRICS']['combos_scenef'], 1)
                self.assertTrue((root/'build/scenef_combo_sources/amc-orange-30-2026-10-25.json').exists())
                self.assertFalse((root/'build/amc_previous_report_fallback.txt').exists())
            finally:
                os.chdir(oldcwd)

    def test_public_report_marks_scenef_source_and_preserves_origin(self):
        template = """<html><head></head><body><main><table class="dataframe"><thead><tr>
        <th>movie_title</th><th>rt_c/a</th><th>imdb_rating</th><th>showtimes</th><th>runtime</th>
        </tr></thead><tbody><tr><td>Example Feature</td><td>81/70</td><td>7.2</td>
        <td>AMC Orange 30<br/>• 2026-10-25: 7:00 pm⛔ [Laser at AMC]</td><td>1h 45m</td>
        </tr></tbody></table></main></body></html>"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'docs').mkdir()
            (root/'build/scenef_combo_sources').mkdir(parents=True)
            (root/'docs/index.html').write_text(template, encoding='utf-8')
            record = {
                'provider': 'scenef', 'theater': 'AMC Orange 30', 'date': '2026-10-25',
                'verified_at': '2026-10-23T21:00:00Z', 'fetched_on': '2026-10-24',
                'scenef_url': 'https://scenef.com/theater/amc-orange?region=orangecounty'
            }
            (root/'build/scenef_combo_sources/a.json').write_text(json.dumps(record), encoding='utf-8')
            cwd = Path.cwd()
            try:
                os.chdir(root)
                with patch.object(postprocess, 'HTML_PATH', Path('docs/index.html')), \
                     patch.object(postprocess, 'wikipedia_thumbnail_url', return_value=None):
                    postprocess.main()
            finally:
                os.chdir(cwd)
            html = BeautifulSoup((root/'docs/index.html').read_text(encoding='utf-8'), 'html.parser')
            payload = json.loads(html.select_one('#showtimes-data').string)
            slot = payload['amc_fetch_dates'][0]
            self.assertEqual(slot['provider'], 'scenef')
            self.assertEqual(slot['verified_on'], '2026-10-23')
            self.assertIsNone(slot['fetched_on'])
            self.assertEqual(html.select_one('.showtime-fetch-date')['data-label'], 'SceneF 10/23/26')
            self.assertIn('fetched 2026-10-24, verified 2026-10-23', html.select_one('.showtime-fetch-date')['title'])
            self.assertIsNone(html.select_one('.showtimes-freshness'))
            self.assertIn('SceneF third-party showtimes', html.select_one('.showtime-fetch-date')['title'])
            self.assertIsNotNone(html.find(id='scenef-schedule-notice'))
            self.assertTrue(html.select_one('a[href="https://scenef.com/week?region=orangecounty"]'))
            self.assertEqual(len(payload['showtimes']), 1)
            self.assertTrue(payload['showtimes'][0]['a_list_excluded'])

    def test_patch_adds_comparison_without_calling_scenef_for_healthy_amc(self):
        from scripts.patch_notebook_titles import AMC_SCRAPE_REPLACEMENT
        self.assertIn('if _coverage_problem or _no_rows:', AMC_SCRAPE_REPLACEMENT)
        self.assertIn('_scenef_improves(_scenef_rows, final_rows)', AMC_SCRAPE_REPLACEMENT)
        self.assertIn('_scenef_improves(_scenef_rows, _old)', AMC_SCRAPE_REPLACEMENT)
        self.assertIn('_finish_combo("scenef"', AMC_SCRAPE_REPLACEMENT)

    def test_v50_parse_diagnostics_reveal_why_scenef_has_no_rows(self):
        import copy
        from datetime import timezone
        from scripts.scenef_showtimes import parse_combo
        now = datetime(2026, 10, 24, 16, 0, tzinfo=timezone.utc)

        def check(feed, theater, day, reason):
            diag = {}
            rows, _ = parse_combo(feed, theater, day, now=now, diagnostic=diag)
            self.assertEqual(rows, [])
            self.assertEqual(diag["reason"], reason)
            return diag

        feed = self._feed()
        check({}, "AMC Orange 30", "2026-10-25", "region_mismatch")
        wrong_timezone = dict(feed, timezone="America/New_York")
        check(wrong_timezone, "AMC Orange 30", "2026-10-25", "timezone_mismatch")
        old_board = dict(feed, data_as_of="2026-10-15T13:00:00Z")
        check(old_board, "AMC Orange 30", "2026-10-25", "board_stale_or_undated")
        no_arrays = dict(feed, screenings=None)
        check(no_arrays, "AMC Orange 30", "2026-10-25", "feed_schema_missing_arrays")
        check(feed, "AMC Woodbridge 5", "2026-10-25", "theater_not_found")
        ambiguous = copy.deepcopy(feed)
        ambiguous['venues'].append({"id": "amc-orange-copy", "name": "AMC Orange 30"})
        check(ambiguous, "AMC Orange 30", "2026-10-25", "theater_ambiguous")
        wrong_date = check(feed, "AMC Orange 30", "2026-11-01", "requested_date_absent")
        self.assertEqual(wrong_date['available_dates'], ['2026-10-25'])
        self.assertEqual(wrong_date['venue_screenings'], 2)
        self.assertEqual(wrong_date['date_screenings'], 0)
        stale_screenings = copy.deepcopy(feed)
        for entry in stale_screenings['screenings']:
            entry['verified_at'] = "2026-10-10T14:00:00Z"
        stale = check(stale_screenings, "AMC Orange 30", "2026-10-25", "date_screenings_rejected_by_validation")
        self.assertEqual(stale['unverified_screenings'], 2)
        disputed = copy.deepcopy(feed)
        for entry in disputed['screenings']:
            entry['confidence'] = 'disputed'
        d = check(disputed, "AMC Orange 30", "2026-10-25", "date_screenings_rejected_by_validation")
        self.assertEqual(d['disputed_screenings'], 2)
        valid = {}
        rows, _ = parse_combo(feed, "AMC Orange 30", "2026-10-25", now=now, diagnostic=valid)
        self.assertEqual(len(rows), 2)
        self.assertEqual(valid['reason'], 'valid_candidate')

    def test_v50_fetch_diagnostics_report_http_and_content_failures_without_secrets(self):
        from scripts import scenef_showtimes
        import requests as _requests
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            oldcwd = Path.cwd()
            try:
                os.chdir(tmp)
                errors = [
                    (_requests.Timeout('time out'), 'request_timeout'),
                    (_requests.ConnectionError('sensitive URL token'), 'network_error'),
                ]
                for error, code in errors:
                    scenef_showtimes.fetch_board.cache_clear()
                    with patch.object(scenef_showtimes.requests, 'get', side_effect=error):
                        self.assertIsNone(scenef_showtimes.fetch_board())
                    diagnostic = json.loads(Path('build/scenef_fetch_diagnostic.json').read_text())
                    self.assertEqual(diagnostic['reason'], code)
                    self.assertNotIn('sensitive', json.dumps(diagnostic))
                for content_type, payload, code in [
                    ('text/html', '<html>Blocked</html>', 'non_json_response'),
                    ('application/json', 'not-json', 'invalid_json'),
                ]:
                    scenef_showtimes.fetch_board.cache_clear()
                    response = SimpleNamespace(status_code=200, headers={'content-type':content_type},
                                               raise_for_status=lambda: None,
                                               json=lambda: (_ for _ in ()).throw(ValueError('not-json')))
                    with patch.object(scenef_showtimes.requests, 'get', return_value=response):
                        self.assertIsNone(scenef_showtimes.fetch_board())
                    diagnostic = json.loads(Path('build/scenef_fetch_diagnostic.json').read_text())
                    self.assertEqual(diagnostic['reason'], code)
                scenef_showtimes.fetch_board.cache_clear()
                response = SimpleNamespace(status_code=403, headers={'content-type':'application/json'},
                                           raise_for_status=lambda: (_ for _ in ()).throw(_requests.HTTPError('403')))
                with patch.object(scenef_showtimes.requests, 'get', return_value=response):
                    self.assertIsNone(scenef_showtimes.fetch_board())
                diagnostic = json.loads(Path('build/scenef_fetch_diagnostic.json').read_text())
                self.assertEqual((diagnostic['reason'],diagnostic['http_status']), ('http_error',403))
            finally:
                os.chdir(oldcwd)
                scenef_showtimes.fetch_board.cache_clear()

    def test_v50_get_combo_and_comparison_persist_actions_only_diagnostics(self):
        from scripts import scenef_showtimes
        from datetime import timezone
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            oldcwd = Path.cwd()
            try:
                os.chdir(tmp)
                # Stub a currently fresh board that has no screenings on the
                # requested day. The parser's date filtering remains unchanged.
                feed = self._feed()
                feed['data_as_of'] = now.isoformat()
                with patch.object(scenef_showtimes, 'fetch_board', return_value=feed):
                    rows, _ = scenef_showtimes.get_combo('AMC Orange 30', '2026-11-01')
                self.assertEqual(rows, [])
                path = Path('build/scenef_combo_diagnostics/amc-orange-30-2026-11-01.json')
                item = json.loads(path.read_text())
                self.assertEqual(item['reason'], 'requested_date_absent')
                old_rows = self._rows({'Feature': ['7:00 pm', '9:00 pm']})
                scenef_showtimes.record_combo_decision('AMC Orange 30', '2026-11-01',
                    accepted=False, reason='previous_report_not_improved', candidate=list(old_rows),
                    runner=[], previous=list(old_rows))
                updated = json.loads(path.read_text())
                self.assertEqual(updated['decision_reason'], 'previous_report_not_improved')
                self.assertEqual(updated['candidate_slots'], 2)
                self.assertEqual(updated['previous_slots'], 2)
                self.assertEqual(updated['reason'], 'requested_date_absent')
            finally:
                os.chdir(oldcwd)

    def test_v50_incomplete_coverage_and_retry_reason_agree(self):
        from scripts.patch_notebook_titles import DF_SHOW_INSERT
        workflow = (Path(__file__).parent.parent / '.github/workflows/daily-AMC.yml').read_text()
        self.assertIn('AMC/SceneF report coverage incomplete for requested dates.', DF_SHOW_INSERT)
        self.assertIn('Missing or rejected theater/date schedules:', DF_SHOW_INSERT)
        self.assertIn('grep -Fq "AMC/SceneF report coverage incomplete for requested dates."', workflow)
        self.assertIn('build/scenef_combo_diagnostics', workflow)
        self.assertIn('[SCENEF-FETCH]', workflow)
        self.assertIn('[SCENEF-COMBO]', workflow)
        self.assertLess(workflow.index('pm_status=${PIPESTATUS[0]}'), workflow.index('[SCENEF-COMBO]'))
        self.assertNotIn('No fresh AMC showtimes were parsed for the requested dates.', DF_SHOW_INSERT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
