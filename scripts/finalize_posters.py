#!/usr/bin/env python3
"""
Validate and improve movie posters after postprocess_report.py.

The resolver uses an already-validated Rotten Tomatoes movie identity plus
Wikimedia/Wikidata for primary poster resolution. When those sources cannot
produce a usable poster for a new/limited/event title, Letterboxd's TMDB-backed
film catalogue is used as a final, independently validated poster fallback.
The finalizer never uses IMDb/Fandango for title discovery. Exact IMDb IDs
resolved from validated metadata may be checked for a current published rating
when the official daily ratings dataset has not caught up yet.

Poster policy
-------------
This is intentionally permissive: a candidate is accepted unless available
metadata gives a strong reason to reject it.  Missing metadata is NOT a reason
to reject a poster. When a short/generic title is ambiguous and AMC supplies no
spoken-language tag, English-language films get only a small ranking bonus;
other-language films remain eligible and win when stronger year/runtime evidence
points to them.

Strong contradictions currently used as vetoes:
  * an explicit/inferred release year differs by more than 3 years;
  * AMC says a spoken language and Wikidata says a different original language;
  * runtime differs by more than 20 minutes for an ordinary screening;
  * the Wikidata item is clearly not a film/movie/documentary.

Runtime is deliberately NOT used as a veto for event presentations such as
Q&As, double features, marathons, or live-event packages because AMC's listed
runtime can include the event itself rather than only the feature.

There are no movie-specific title mappings or poster URLs in this file.

Audience Focus policy
---------------------
The final report also gets an ``Audience Focus`` column.  This is descriptive
context, not a prediction that only a particular group will enjoy a movie and
not an adjustment to any rating.  Multiple tags are allowed.  The classifier
uses explicit evidence only: AMC spoken-language labels, Wikidata original
language/country, explicit genre/subject/description wording, selected
production/distribution organizations, and the selected English Wikipedia
article's introductory summary/categories.  It never infers a cultural identity
from actors or other people attached to a film.  Generic geographic setting
alone is not enough for an identity-focus flag. Missing metadata defaults to
``General`` rather than guessing. Verified language metadata from the immediately
previous report may be retained during a provider outage, but only for the same
film identity and without replacing fresh contradictory evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import csv
import gzip
from datetime import datetime, timezone
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import sys
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from scripts.movie_titles import (
    analyze_movie_title,
    candidate_title_variants,
    canonical_movie_title,
    clean_amc_title,
    is_non_movie_title,
    search_terms_for_title,
    wikipedia_title_candidates,
)


HTML_PATH = Path("docs/index.html")
ENWIKI_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
COMMONS_FILE_REDIRECT = "https://commons.wikimedia.org/wiki/Special:Redirect/file/"
USER_AGENT = (
    "AMC-weekend-movie-report/6.1 "
    "(https://github.com/benpark2/AMC; Wikimedia metadata lookup)"
)
PLACEHOLDER_TOKEN = "images-not-found"
PLACEHOLDER_URL = (
    "https://4ddig.tenorshare.com/images/photo-recovery/images-not-found.webp"
)
IMDB_ID_RE = re.compile(r"/title/(tt\d{5,12})(?:/|$)", re.IGNORECASE)
QID_RE = re.compile(r"^Q\d+$")
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
MAX_API_ATTEMPTS = 2
WIKIMEDIA_POSTER_WIDTH = 800
LETTERBOXD_DIRECT_URL_LIMIT = 6
LETTERBOXD_SEARCH_RESULT_LIMIT = 4
WIKIDATA_TITLE_VARIANT_LIMIT = 3
# Provider enrichment is deliberately bounded.  These are not whole-job
# timeouts; they cap only this post-processing script's external metadata work.
# If a provider is slow or blocked, the report preserves existing data and
# moves on instead of turning poster enrichment into a many-minute crawl.
_PROVIDER_BUDGET_SECONDS = {
    "wikimedia": 45.0,
    "rt": 30.0,
    "letterboxd": 35.0,
    "imdb": 12.0,
}
_PROVIDER_BUDGET_USED: dict[str, float] = {}
_JSON_CACHE: dict[str, dict | None] = {}
_TEXT_CACHE: dict[str, str | None] = {}
_LABEL_CACHE: dict[str, str] = {}
_LETTERBOXD_RESULT_CACHE: dict[tuple[object, ...], tuple[object, list[str]]] = {}
RT_MOVIE_URL_RE = re.compile(
    r"^https?://(?:www\.)?rottentomatoes\.com/m/[a-z0-9_%\-]+/?(?:[?#].*)?$",
    re.IGNORECASE,
)
RT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
LETTERBOXD_BASE = "https://letterboxd.com"
LETTERBOXD_FILM_RE = re.compile(
    r"^https?://(?:www\.)?letterboxd\.com/film/[a-z0-9][a-z0-9-]*/?$",
    re.IGNORECASE,
)
LETTERBOXD_POSTER_HOSTS = ("ltrbxd.com", "image.tmdb.org")
IMDB_RATINGS_GZ = Path("build/imdb_datasets/title.ratings.tsv.gz")
PREVIOUS_REPORT_PATH = Path("build/previous_report.html")
TMDB_MOVIE_RE = re.compile(r"^https?://(?:www\.)?themoviedb\.org/movie/\d+/?(?:[?#].*)?$", re.IGNORECASE)
_PREVIOUS_POSTER_INDEX: dict[str, list[dict[str, str | int | None]]] | None = None

# Audience-focus labels are intentionally descriptive and evidence based.  The
# first four describe an explicit thematic/marketing focus.  The remaining
# labels describe language/market context for international films.
AUDIENCE_TAG_ORDER = (
    "Music/Concert",
    "Faith-oriented",
    "LGBTQ+-focused",
    "Black-focused",
    "Latino/Hispanic-focused",
    "Indian",
    "Korean",
    "Chinese",
    "Vietnamese",
    "Japanese",
    "Filipino",
    "Thai",
    "Spanish-language",
)

# Production/distribution-company signals are deliberately soft and narrowly
# scoped. They are used only when the organization publicly describes a clear
# cultural/audience mission; they do not override contradictory film metadata.
# The values are display names used in the report tooltip.
FAITH_ORGANIZATION_NAMES = {
    "angel studios": "Angel Studios",
}

BLACK_FOCUS_ORGANIZATION_NAMES = {
    "bet": "BET",
    "black entertainment television": "BET",
    "bet media group": "BET Media Group",
    "bet films": "BET Films",
    "bet studios": "BET Studios",
    "bet+": "BET+",
    "codeblack films": "Codeblack Films",
}

LATINO_FOCUS_ORGANIZATION_NAMES = {
    "pantelion films": "Pantelion Films",
    "pantaya": "Pantaya",
    "telemundo studios": "Telemundo Studios",
    "vix": "ViX",
    "exile content": "Exile Content Studio",
    "exile content studio": "Exile Content Studio",
}

LANGUAGE_FOCUS_GROUPS = {
    "Indian": {
        "hindi", "telugu", "tamil", "malayalam", "kannada", "bengali",
        "marathi", "gujarati", "punjabi", "odia", "assamese",
    },
    "Korean": {"korean"},
    "Chinese": {"chinese", "mandarin", "mandarin chinese", "cantonese"},
    "Vietnamese": {"vietnamese"},
    "Japanese": {"japanese"},
    "Filipino": {"filipino", "tagalog"},
    "Thai": {"thai"},
    "Spanish-language": {"spanish"},
}

COUNTRY_FOCUS_GROUPS = {
    "Indian": {"india"},
    "Korean": {"south korea", "republic of korea"},
    "Chinese": {"china", "people's republic of china", "hong kong", "taiwan"},
    "Vietnamese": {"vietnam"},
    "Japanese": {"japan"},
    "Filipino": {"philippines"},
    "Thai": {"thailand"},
}

FAITH_TEXT_PATTERNS = (
    r"\bfaith[- ]based\b",
    r"\bchristianity\b",
    r"\bchristian[- ]themed\b",
    r"\bchristian\s+(?:film|movie|drama|themes?|faith|audience|media)\b",
    r"\bbiblical\b",
    r"\bbible\b",
    r"\breligious film\b",
    r"\bcatholic(?:ism)?\b",
    r"\bevangelical(?:ism)?\b",
    r"\bjesus christ\b",
)
LGBTQ_TEXT_PATTERNS = (
    r"\blgbtq?\+?\b",
    r"\bgay\b",
    r"\blesbian\b",
    r"\bqueer\b",
    r"\btransgender\b",
    r"\bbisexual\b",
    # Some metadata sources use older terminology rather than the modern
    # LGBTQ umbrella term, so retain this as an evidence synonym.
    r"\bhomosexual(?:ity|s)?\b",
)
BLACK_FOCUS_TEXT_PATTERNS = (
    r"\bafrican[- ]american\b",
    r"\bblack american\b",
    r"\bblack culture\b",
    r"\bblack cinema\b",
    r"\bblack people\b",
)
LATINO_FOCUS_TEXT_PATTERNS = (
    r"\blatino\b",
    r"\blatina\b",
    r"\blatinx\b",
    r"\bhispanic\b",
    r"\bmexican[- ]american\b",
    r"\bchicano\b",
    r"\bchicana\b",
    r"\blatin american culture\b",
    r"\bmexican culture\b",
    r"\bmexican holiday\b",
    r"\bday of the dead\b",
    r"\bd[ií]a de (?:los )?muertos\b",
    r"\bdominican community\b",
    r"\bpuerto rican culture\b",
    r"\bcuban[- ]american\b",
)

# Wikipedia intro/category text needs stricter patterns than structured
# Wikidata subjects.  In particular, demographic wording about the cast must
# never be enough to classify the film's audience focus.
WIKIPEDIA_LGBTQ_FOCUS_PATTERNS = (
    r"\blgbtq?\+? (?:themes?|culture|community|communities|films?|cinema|stories|storytelling)\b",
    r"\bgay[- ]themed\b",
    r"\bgay (?:romance|community|culture|films?|cinema|stories|storytelling)\b",
    r"\blesbian (?:romance|community|culture|films?|cinema|stories|storytelling)\b",
    r"\btransgender (?:community|culture|films?|cinema|stories|storytelling)\b",
    r"\bqueer (?:culture|community|cinema|films?|stories|storytelling)\b",
    r"\bhomosexual(?:ity|s)?\b",
)

# Sparse/new releases do not always have structured religion metadata yet.
# Title fallback is intentionally limited to explicit, provider-independent
# religious vocabulary.  Do not add story phrases, character names, or wording
# taken from a particular current release here; those belong in external
# metadata (Wikidata/Wikipedia), not in source-code exceptions.
FAITH_TITLE_PATTERNS = (
    r"\b(?:jesus(?: christ)?|christian|biblical|bible|gospel|messiah)\b",
    r"\b(?:eucharist|eucharistic|sacrament|sacramental|adoration)\b",
)

# Cinema event wording that is explicit enough to identify music/concert
# programming without knowing any artist or current release.  These patterns
# describe reusable event types, never individual movie titles.
MUSIC_EVENT_TITLE_PATTERNS = (
    r"\bworld\s+tour\b",
    r"\bconcert(?:\s+film)?\b",
    r"\bcheering\s+party\b",
)
MUSIC_EVENT_METADATA_PATTERNS = (
    r"\bconcert\s+film\b",
    r"\bmusic\s+(?:film|documentary|special|concert)\b",
    r"\bconcert\b",
    r"\bgenre\s*[:=-]?\s*music\b",
)

# Wikipedia prose is useful as a second source for international-market
# classification when Wikidata is incomplete.  Patterns describe the film or
# its language; generic setting/culture references are intentionally excluded.
WIKIPEDIA_MARKET_PATTERNS = {
    "Indian": (
        r"\bindian (?:[a-z-]+ )*film\b",
        r"\b(?:hindi|telugu|tamil|malayalam|kannada|bengali|marathi|gujarati|punjabi|odia|assamese)-language\b",
    ),
    "Korean": (r"\bsouth korean (?:[a-z-]+ )*film\b", r"\bkorean-language\b"),
    "Chinese": (r"\bchinese (?:[a-z-]+ )*film\b", r"\b(?:mandarin|cantonese|chinese)-language\b"),
    "Vietnamese": (r"\bvietnamese (?:[a-z-]+ )*film\b", r"\bvietnamese-language\b"),
    "Japanese": (r"\bjapanese (?:[a-z-]+ )*film\b", r"\bjapanese-language\b"),
    # Do not treat a Philippine setting/mythology reference as a Filipino film.
    "Filipino": (r"\b(?:filipino|tagalog)-language\b",),
    "Thai": (r"\bthai (?:[a-z-]+ )*film\b", r"\bthai-language\b"),
    "Spanish-language": (r"\bspanish-language\b",),
}
WIKIPEDIA_BLACK_FOCUS_PATTERNS = (
    r"\bafrican[- ]american (?:comedy|drama|films?|cinema|culture|community|communities|experience|stories|storytelling)\b",
    r"\bblack american (?:culture|community|communities|experience|films?|cinema|stories|storytelling)\b",
    r"\bblack culture\b",
    r"\bblack communities?\b",
    r"\bblack cinema\b",
    r"\bblack sisterhood\b",
)
WIKIPEDIA_LATINO_FOCUS_PATTERNS = (
    r"\bhispanic and latino americans\b",
    r"\blatin american culture\b",
    r"\blatino (?:culture|community|communities|audiences?|films?|cinema|stories|storytelling|experience)\b",
    r"\bhispanic (?:culture|community|communities|audiences?|films?|cinema|stories|storytelling|experience)\b",
    r"\bmexican culture\b",
    r"\bmexican holiday\b",
    r"\bmexican[- ]american (?:culture|community|communities|famil(?:y|ies)|experience|films?|stories|storytelling)\b",
    r"\bday of the dead\b",
    r"\bd[ií]a de (?:los )?muertos\b",
    r"\bdominican community\b",
    r"\bpuerto rican culture\b",
    r"\bcuban[- ]american (?:culture|community|communities|experience|films?|stories|storytelling)\b",
)

AUDIENCE_STYLE = r"""
/* Audience Focus: descriptive context tags, not a rating adjustment. */
.audience-focus-cell{
  min-width: 112px;
  max-width: 180px;
  font-size: 12px !important;
  line-height: 1.25 !important;
}
.audience-focus-tag{
  display: inline-block;
  margin: 1px 3px 2px 0;
  padding: 2px 6px;
  border: 1px solid rgba(15,23,42,.16);
  border-radius: 999px;
  background: rgba(15,23,42,.045);
  white-space: nowrap;
}
"""


@dataclass(frozen=True)
class AudienceFocusResult:
    tags: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RtPageMetadata:
    title: str
    poster_url: str | None
    languages: frozenset[str]
    genres: frozenset[str]
    release_year: int | None
    runtime_min: int | None


@dataclass(frozen=True)
class MovieContext:
    display_title: str
    canonical_title: str
    expected_year: int | None
    runtime_min: int | None
    spoken_languages: frozenset[str]
    relax_runtime: bool


def _api_url(base: str, params: dict[str, object]) -> str:
    encoded = urlencode(
        [(str(k), str(v)) for k, v in params.items() if v is not None]
    )
    return f"{base}?{encoded}"


def _provider_name_for_url(url: str) -> str | None:
    low = (url or "").casefold()
    if "wikipedia.org" in low or "wikidata.org" in low or "wikimedia.org" in low:
        return "wikimedia"
    if "rottentomatoes.com" in low or "flixster.com" in low or "fandango.com" in low:
        return "rt"
    if (
        "letterboxd.com" in low
        or "ltrbxd.com" in low
        or "image.tmdb.org" in low
        or "themoviedb.org" in low
    ):
        return "letterboxd"
    if "imdb.com" in low:
        return "imdb"
    return None


def _provider_budget_available(url: str) -> bool:
    provider = _provider_name_for_url(url)
    if provider is None:
        return True
    limit = _PROVIDER_BUDGET_SECONDS.get(provider)
    if limit is None:
        return True
    # Budget actual provider/network work, not wall-clock time since the first
    # request.  The previous wall-clock implementation could disable RT for
    # movies near the bottom of the report simply because Wikimedia/Letterboxd
    # work happened in between, even when RT itself had used only a few seconds.
    return _PROVIDER_BUDGET_USED.get(provider, 0.0) < limit


def _charge_provider_budget(url: str, elapsed: float) -> None:
    provider = _provider_name_for_url(url)
    if provider is None:
        return
    _PROVIDER_BUDGET_USED[provider] = (
        _PROVIDER_BUDGET_USED.get(provider, 0.0) + max(0.0, float(elapsed))
    )


def _fetch_json(url: str, timeout: int = 8) -> dict | None:
    if url in _JSON_CACHE:
        return _JSON_CACHE[url]
    if not _provider_budget_available(url):
        return None

    budget_started = time.monotonic()
    try:
        for attempt in range(MAX_API_ATTEMPTS):
            try:
                request = Request(
                    url,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                )
                with urlopen(request, timeout=timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                    if isinstance(data, dict):
                        _JSON_CACHE[url] = data
                        return data
                    break
            except HTTPError as exc:
                if exc.code not in RETRYABLE_HTTP_CODES:
                    break
                if attempt + 1 < MAX_API_ATTEMPTS:
                    retry_after = 0.0
                    try:
                        retry_after = float(exc.headers.get("Retry-After", "0") or 0)
                    except (TypeError, ValueError):
                        pass
                    time.sleep(min(max(retry_after, 0.5 * (attempt + 1)), 3.0))
                    continue
            except (URLError, TimeoutError, ValueError, json.JSONDecodeError):
                if attempt + 1 < MAX_API_ATTEMPTS:
                    time.sleep(0.5 * (attempt + 1))
                    continue
            break
    finally:
        _charge_provider_budget(url, time.monotonic() - budget_started)

    _JSON_CACHE[url] = None
    return None


def _api_json(base: str, **params: object) -> dict | None:
    return _fetch_json(_api_url(base, params))


def _fetch_text(
    url: str, timeout: int = 20, attempts: int | None = None
) -> str | None:
    """Fetch one HTML page with bounded retries.

    Primary metadata providers keep the normal retry policy. Optional final
    fallbacks can explicitly request one short attempt so a blocked secondary
    provider never stalls or fails the report.
    """
    if url in _TEXT_CACHE:
        return _TEXT_CACHE[url]
    if not _provider_budget_available(url):
        return None
    max_attempts = MAX_API_ATTEMPTS if attempts is None else max(1, int(attempts))
    budget_started = time.monotonic()
    try:
        for attempt in range(max_attempts):
            try:
                request = Request(
                    url,
                    headers={
                        "User-Agent": RT_USER_AGENT,
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    },
                )
                with urlopen(request, timeout=timeout) as response:
                    text = response.read().decode("utf-8", errors="replace")
                    _TEXT_CACHE[url] = text
                    return text
            except HTTPError as exc:
                if exc.code not in RETRYABLE_HTTP_CODES:
                    break
            except (URLError, TimeoutError, ValueError):
                pass
            if attempt + 1 < max_attempts:
                time.sleep(0.5 * (attempt + 1))
    finally:
        _charge_provider_budget(url, time.monotonic() - budget_started)
    _TEXT_CACHE[url] = None
    return None


def _extract_rt_url(row) -> str | None:
    """Return a validated RT movie URL from visible or hidden report metadata."""
    link = row.select_one('a[href*="rottentomatoes.com/m/"]')
    candidates: list[str] = []
    if link is not None:
        candidates.append(str(link.get("href", "")))
    for cell in row.find_all(["td", "th"], recursive=False):
        value = cell.get("data-source-url")
        if isinstance(value, str):
            candidates.append(value)
    for value in candidates:
        value = value.strip()
        if RT_MOVIE_URL_RE.match(value):
            return value
    return None


def _rt_flat_strings(soup: BeautifulSoup) -> list[str]:
    return [clean_amc_title(x) for x in soup.stripped_strings if clean_amc_title(x)]


def _rt_labeled_value(strings: list[str], label: str) -> str | None:
    label_cf = label.casefold()
    known_labels = {
        "director", "producer", "screenwriter", "distributor", "production co",
        "rating", "genre", "original language", "release date (theaters)",
        "release date", "runtime", "sound mix", "aspect ratio",
    }
    for i, value in enumerate(strings):
        if value.casefold().rstrip(":") != label_cf:
            continue
        for candidate in strings[i + 1 : i + 5]:
            cleaned = clean_amc_title(candidate)
            if not cleaned:
                continue
            if cleaned.casefold().rstrip(":") in known_labels:
                break
            return cleaned
    return None


def _usable_rt_poster_url(value: str | None) -> str | None:
    """Reject RT/Fandango placeholder art and other non-poster assets.

    Rotten Tomatoes serves a generic ``rt-poster-default`` GIF for titles whose
    real key art is not available.  Treating that asset as a movie poster caused
    good Wikimedia posters to be overwritten and blocked later fallbacks.  This
    filter is provider-shape based only; it contains no title/year exceptions.
    """
    value = (value or "").strip()
    if not value.startswith(("https://", "http://")):
        return None
    low = value.casefold()
    bad_tokens = (
        "rt-poster-default",
        "poster-default",
        "default-poster",
        "placeholder",
        "images-not-found",
        "image-not-found",
        "no-poster",
        "no_poster",
    )
    if any(token in low for token in bad_tokens):
        return None
    # RT's known default art is a GIF.  Real theatrical key art from the
    # provider CDNs is JPEG/PNG/WebP, so a GIF is intentionally not accepted.
    if re.search(r"\.gif(?:[?#]|$)", low):
        return None
    return value


def _rt_poster_url(soup: BeautifulSoup) -> str | None:
    """Return usable RT key art only; avoid defaults/photos/social cards."""
    for tag in soup.find_all(attrs={"alt": re.compile(r"^\s*Poster for\b", re.I)}):
        # Prefer the largest explicit srcset candidate when available.
        srcset = str(tag.get("srcset", "") or tag.get("data-srcset", ""))
        if srcset:
            choices: list[tuple[int, str]] = []
            for piece in srcset.split(","):
                bit = piece.strip().split()
                if not bit:
                    continue
                url = bit[0]
                width = 0
                if len(bit) > 1:
                    m = re.match(r"(\d+)w$", bit[1])
                    if m:
                        width = int(m.group(1))
                if url.startswith(("https://", "http://")):
                    choices.append((width, url))
            if choices:
                for _, candidate in sorted(choices, key=lambda item: item[0], reverse=True):
                    usable = _usable_rt_poster_url(candidate)
                    if usable:
                        return usable
        for attr in ("src", "data-src", "data-lazy-src"):
            value = tag.get(attr)
            if isinstance(value, str) and value.startswith(("https://", "http://")):
                usable = _usable_rt_poster_url(value)
                if usable:
                    return usable

    # Exact RT movie pages commonly expose the key art through OpenGraph even
    # when the responsive poster component is hydrated client-side.  Restrict
    # this fallback to RT/Fandango's image CDN families so a generic site logo
    # or unrelated social image is not accepted as a poster.
    meta = soup.find("meta", attrs={"property": "og:image"})
    value = str(meta.get("content", "")) if meta else ""
    low = value.casefold()
    if value.startswith(("https://", "http://")) and any(
        host in low for host in ("flixster.com", "fandango.com")
    ):
        return _usable_rt_poster_url(value)
    return None


def _rt_page_metadata(url: str) -> RtPageMetadata | None:
    if not RT_MOVIE_URL_RE.match(url or ""):
        return None
    html_text = _fetch_text(url, timeout=5, attempts=1)
    if not html_text:
        return None
    soup = BeautifulSoup(html_text, "html.parser")
    heading = soup.find("h1")
    title = clean_amc_title(heading.get_text(" ", strip=True)) if heading else ""
    if not title:
        meta = soup.find("meta", attrs={"property": "og:title"})
        if meta and meta.get("content"):
            title = clean_amc_title(str(meta.get("content")))
            title = re.sub(r"\s*\|\s*Rotten Tomatoes\s*$", "", title, flags=re.I)
    if not title:
        return None

    strings = _rt_flat_strings(soup)
    language = _rt_labeled_value(strings, "Original Language")
    genre = _rt_labeled_value(strings, "Genre")
    release = _rt_labeled_value(strings, "Release Date (Theaters)") or _rt_labeled_value(strings, "Release Date")
    runtime = _rt_labeled_value(strings, "Runtime")

    year = None
    if release:
        match = re.search(r"\b((?:19|20)\d{2})\b", release)
        if match:
            year = int(match.group(1))

    # Current RT pages sometimes hydrate Movie Info client-side.  When the
    # labeled release field is absent, use structured date metadata or the
    # compact title/header line before giving up on release-year validation.
    if year is None:
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text(" ", strip=True)
            match = re.search(
                r'"(?:datePublished|dateCreated)"\s*:\s*"((?:19|20)\d{2})',
                raw or "",
                flags=re.IGNORECASE,
            )
            if match:
                year = int(match.group(1))
                break
    if year is None:
        flat = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        pos = flat.casefold().find(title.casefold())
        nearby = flat[pos : pos + 280] if pos >= 0 else flat[:280]
        match = re.search(r"\b((?:19|20)\d{2})\b", nearby)
        if match:
            year = int(match.group(1))

    runtime_min = _runtime_minutes(runtime or "")
    if runtime_min is None:
        flat = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        pos = flat.casefold().find(title.casefold())
        nearby = flat[pos : pos + 320] if pos >= 0 else flat[:320]
        runtime_min = _runtime_minutes(nearby)

    languages = frozenset(
        clean_amc_title(part).casefold()
        for part in re.split(r"[,;/]", language or "")
        if clean_amc_title(part)
    )
    genres = frozenset(
        clean_amc_title(part).casefold()
        for part in re.split(r"[,;/]", genre or "")
        if clean_amc_title(part)
    )
    return RtPageMetadata(
        title=title,
        poster_url=_rt_poster_url(soup),
        languages=languages,
        genres=genres,
        release_year=year,
        runtime_min=runtime_min,
    )


def _rt_metadata_for_context(url: str | None, context: MovieContext) -> RtPageMetadata | None:
    if not url:
        return None
    metadata = _rt_page_metadata(url)
    if metadata is None:
        return None
    if max(
        (_title_similarity(metadata.title, candidate) for candidate in _context_title_variants(context)),
        default=0.0,
    ) < 0.90:
        return None
    if context.expected_year is not None and metadata.release_year is not None:
        if abs(metadata.release_year - context.expected_year) > 3:
            return None
    if (
        context.runtime_min is not None
        and metadata.runtime_min is not None
        and not context.relax_runtime
        and abs(metadata.runtime_min - context.runtime_min) > 20
    ):
        return None
    return metadata


def _rt_scores_from_soup(soup: BeautifulSoup) -> tuple[int | None, int | None]:
    """Return (audience, critic) percentages from one validated RT page.

    The parser intentionally requires percentage-shaped evidence and ignores
    review/rating counts.  It is used only as a finalizer backfill when the
    notebook could not discover the correct RT identity.
    """
    def score(value: object) -> int | None:
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return number if 0 <= number <= 100 else None

    audience = None
    critic = None
    for tag in soup.find_all(True):
        if audience is None:
            for attr in ("audiencescore", "audience-score", "audience_score"):
                if tag.has_attr(attr):
                    audience = score(tag.get(attr))
                    if audience is not None:
                        break
        if critic is None:
            for attr in (
                "tomatometerscore", "tomatometer-score", "tomatometer_score",
                "criticsscore", "critics-score", "critics_score",
            ):
                if tag.has_attr(attr):
                    critic = score(tag.get(attr))
                    if critic is not None:
                        break
        if audience is not None and critic is not None:
            return audience, critic

    raw = str(soup)
    if audience is None:
        for pattern in (
            r'"audienceScore"\s*:\s*(?:\{[^{}]{0,120}?"score"\s*:\s*)?(\d{1,3})',
            r'"audience_score"\s*:\s*(\d{1,3})',
        ):
            match = re.search(pattern, raw, re.I | re.S)
            if match:
                audience = score(match.group(1))
                if audience is not None:
                    break
    if critic is None:
        for pattern in (
            r'"criticsScore"\s*:\s*(?:\{[^{}]{0,120}?"score"\s*:\s*)?(\d{1,3})',
            r'"tomatometerScore"\s*:\s*(?:\{[^{}]{0,120}?"score"\s*:\s*)?(\d{1,3})',
            r'"tomatometer_score"\s*:\s*(\d{1,3})',
        ):
            match = re.search(pattern, raw, re.I | re.S)
            if match:
                critic = score(match.group(1))
                if critic is not None:
                    break

    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    if critic is None:
        for pattern in (
            r"(\d{1,3})%\s+Tomatometer\b",
            r"\bTomatometer\b.{0,90}?(\d{1,3})%",
        ):
            match = re.search(pattern, text, re.I)
            if match:
                critic = score(match.group(1))
                if critic is not None:
                    break
    if audience is None:
        for pattern in (
            r"(\d{1,3})%\s+Popcornmeter\b",
            r"\bPopcornmeter\b.{0,90}?(\d{1,3})%",
        ):
            match = re.search(pattern, text, re.I)
            if match:
                audience = score(match.group(1))
                if audience is not None:
                    break
    return audience, critic


def _rt_scores_for_context(
    url: str | None,
    context: MovieContext,
) -> tuple[int | None, int | None] | None:
    if not url or _rt_metadata_for_context(url, context) is None:
        return None
    html_text = _fetch_text(url, timeout=5, attempts=1)
    if not html_text:
        return None
    return _rt_scores_from_soup(BeautifulSoup(html_text, "html.parser"))


def _context_with_rt_identity(
    context: MovieContext,
    metadata: RtPageMetadata | None,
) -> MovieContext:
    """Use a validated RT release year to disambiguate downstream identity.

    AMC titles frequently omit a year even when a same-title older movie
    exists.  Once the already-resolved RT page agrees on title and runtime, its
    release year is stronger evidence than a generic title-only Wikidata hit.
    Explicit/anniversary-derived AMC years still win and are never overwritten.
    """
    if metadata is None or context.expected_year is not None or metadata.release_year is None:
        return context
    return replace(context, expected_year=metadata.release_year)


@dataclass(frozen=True)
class LetterboxdPageMetadata:
    title: str
    poster_url: str | None
    release_year: int | None
    runtime_min: int | None
    imdb_id: str | None
    url: str
    tmdb_url: str | None = None


def _letterboxd_title_from_soup(soup: BeautifulSoup) -> tuple[str, int | None]:
    """Extract a film title/year from a Letterboxd film page conservatively."""
    for tag in soup.find_all(attrs={"data-film-name": True}):
        value = clean_amc_title(str(tag.get("data-film-name", "")))
        if value:
            year = None
            raw_year = tag.get("data-film-release-year") or tag.get("data-film-year")
            if raw_year and str(raw_year).isdigit():
                year = int(raw_year)
            return value, year

    candidates: list[str] = []
    for attrs in (
        {"property": "og:title"},
        {"name": "twitter:title"},
    ):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            candidates.append(clean_amc_title(str(meta.get("content"))).lstrip("\u200e\u200f"))
    h1 = soup.find("h1")
    if h1:
        candidates.append(clean_amc_title(h1.get_text(" ", strip=True)))

    for raw in candidates:
        if not raw:
            continue
        # Letterboxd social titles commonly append "(YYYY) directed by ..." or
        # "• Reviews, film + cast".  Strip only those provider decorations.
        year = None
        match = re.match(r"^(.*?)\s*\(((?:19|20)\d{2})\)(?:\s+directed by.*|\s*[•|].*)?$", raw, re.I)
        if match:
            return clean_amc_title(match.group(1)), int(match.group(2))
        cleaned = re.sub(r"\s*[•|].*$", "", raw).strip()
        cleaned = re.sub(r"\s+directed by\s+.*$", "", cleaned, flags=re.I).strip()
        if cleaned:
            return cleaned, year
    return "", None


def _letterboxd_poster_url(soup: BeautifulSoup) -> str | None:
    def accepted(value: str) -> str | None:
        value = (value or "").strip()
        if value.startswith("//"):
            value = "https:" + value
        low = value.casefold()
        # Letterboxd itself uses empty-poster-* assets when the catalogue item
        # has no artwork.  They are visually blank and must never be treated as
        # a successful poster resolution.
        if any(token in low for token in (
            "empty-poster", "images-not-found", "image-not-found",
            "poster-default", "default-poster", "placeholder",
        )):
            return None
        if value.startswith(("https://", "http://")) and any(
            host in low for host in LETTERBOXD_POSTER_HOSTS
        ):
            return value
        return None

    # Prefer an explicitly poster-scoped image over OpenGraph, which some
    # providers use for a landscape sharing card.
    for img in soup.find_all("img"):
        ancestry = []
        node = img
        for _ in range(3):
            if node is None:
                break
            classes = node.get("class", []) if hasattr(node, "get") else []
            if isinstance(classes, list):
                ancestry.extend(str(x) for x in classes)
            node = getattr(node, "parent", None)
        context = " ".join(
            [str(img.get("alt", "")), " ".join(ancestry)]
        ).casefold()
        if "poster" not in context and "film-poster" not in context:
            continue
        srcset = str(img.get("srcset", "") or img.get("data-srcset", ""))
        if srcset:
            choices = []
            for piece in srcset.split(","):
                bits = piece.strip().split()
                if not bits:
                    continue
                value = accepted(bits[0])
                if not value:
                    continue
                width = 0
                if len(bits) > 1 and re.fullmatch(r"\d+w", bits[1]):
                    width = int(bits[1][:-1])
                choices.append((width, value))
            if choices:
                return max(choices, key=lambda item: item[0])[1]
        for attr in ("src", "data-src"):
            value = accepted(str(img.get(attr, "")))
            if value:
                return value

    for attrs in (
        {"property": "og:image"},
        {"name": "twitter:image"},
    ):
        meta = soup.find("meta", attrs=attrs)
        value = accepted(str(meta.get("content", "")) if meta else "")
        if value:
            return value
    return None


def _parse_letterboxd_metadata_html(
    html_text: str,
    *,
    canonical_url: str,
) -> LetterboxdPageMetadata | None:
    soup = BeautifulSoup(html_text, "html.parser")
    title, year = _letterboxd_title_from_soup(soup)
    if not title:
        return None
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    if year is None:
        match = re.search(r"\b((?:19|20)\d{2})\b", text[:700])
        if match:
            year = int(match.group(1))
    runtime = None
    match = re.search(r"\b(\d{2,3})\s*(?:mins?|minutes)\b", text, re.I)
    if match:
        runtime = int(match.group(1))

    imdb_id = None
    imdb_link = soup.find("a", href=re.compile(r"imdb\.com/title/tt\d+", re.I))
    if imdb_link is not None:
        match = IMDB_ID_RE.search(str(imdb_link.get("href", "")))
        if match:
            imdb_id = match.group(1).lower()

    tmdb_url = None
    tmdb_link = soup.find("a", href=re.compile(r"themoviedb\.org/movie/\d+", re.I))
    if tmdb_link is not None:
        candidate = str(tmdb_link.get("href", "")).strip()
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        elif candidate.startswith("/"):
            candidate = urljoin("https://www.themoviedb.org", candidate)
        if TMDB_MOVIE_RE.match(candidate):
            tmdb_url = candidate

    return LetterboxdPageMetadata(
        title=title,
        poster_url=_letterboxd_poster_url(soup),
        release_year=year,
        runtime_min=runtime,
        imdb_id=imdb_id,
        url=canonical_url,
        tmdb_url=tmdb_url,
    )


def _tmdb_page_poster_url(url: str | None) -> str | None:
    """Return TMDB-hosted artwork linked by an already-matched catalogue item.

    This is deliberately not a TMDB title search.  The URL must come from the
    validated Letterboxd film page, so it adds one bounded request without
    introducing another ambiguous search path.
    """
    if not url or not TMDB_MOVIE_RE.match(url):
        return None
    html_text = _fetch_text(url, timeout=4, attempts=1)
    if not html_text:
        return None
    soup = BeautifulSoup(html_text, "html.parser")
    for attrs in ({"property": "og:image"}, {"name": "twitter:image"}):
        meta = soup.find("meta", attrs=attrs)
        value = str(meta.get("content", "")).strip() if meta else ""
        if value.startswith("//"):
            value = "https:" + value
        low = value.casefold()
        if (
            value.startswith(("https://", "http://"))
            and "image.tmdb.org" in low
            and not any(token in low for token in ("placeholder", "no-poster", "empty-poster"))
        ):
            return value
    return None


def _letterboxd_page_metadata(url: str) -> LetterboxdPageMetadata | None:
    if not LETTERBOXD_FILM_RE.match(url or ""):
        return None
    # Letterboxd's main page sometimes exposes identity metadata but leaves the
    # poster to client-side hydration.  Parse it first, then consult the embed
    # representation only when the poster is still missing.  Both reads are
    # one-shot, short-timeout fallbacks so a blocked provider cannot stall the
    # whole report.
    main_html = _fetch_text(url, timeout=4, attempts=1)
    main_meta = (
        _parse_letterboxd_metadata_html(main_html, canonical_url=url)
        if main_html
        else None
    )
    if main_meta is not None and main_meta.poster_url:
        return main_meta

    slug = url.rstrip("/").rsplit("/", 1)[-1]
    embed_html = _fetch_text(
        f"https://embed.letterboxd.com/film/{slug}/", timeout=4, attempts=1
    )
    embed_meta = (
        _parse_letterboxd_metadata_html(embed_html, canonical_url=url)
        if embed_html
        else None
    )
    if main_meta is None:
        combined = embed_meta
    elif embed_meta is None:
        combined = main_meta
    else:
        combined = LetterboxdPageMetadata(
            title=main_meta.title or embed_meta.title,
            poster_url=main_meta.poster_url or embed_meta.poster_url,
            release_year=main_meta.release_year or embed_meta.release_year,
            runtime_min=main_meta.runtime_min or embed_meta.runtime_min,
            imdb_id=main_meta.imdb_id or embed_meta.imdb_id,
            url=url,
            tmdb_url=main_meta.tmdb_url or embed_meta.tmdb_url,
        )
    if combined is None:
        return None
    if combined.poster_url:
        return combined
    tmdb_poster = _tmdb_page_poster_url(combined.tmdb_url)
    if not tmdb_poster:
        return combined
    return replace(combined, poster_url=tmdb_poster)


def _letterboxd_slug(value: str) -> str:
    value = clean_amc_title(value)
    if not value:
        return ""
    # Letterboxd slugs are ASCII-ish, lowercase, punctuation-free words joined
    # by hyphens.  This is deterministic title shaping, not a movie alias.
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = value.replace("&", " and ").replace("’", "'").casefold()
    value = re.sub(r"['`]", "", value)
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return re.sub(r"-+", "-", value)


def _letterboxd_direct_urls(context: MovieContext) -> list[str]:
    """Cheap deterministic film-page candidates before any site search.

    When a release year is known, try the year-qualified slug first so remakes
    and same-title releases do not accidentally resolve to an older famous film.
    """
    out: list[str] = []
    seen: set[str] = set()
    for title in _context_title_variants(context):
        slug = _letterboxd_slug(title)
        if not slug:
            continue
        variants = []
        if context.expected_year is not None:
            variants.append(f"{slug}-{context.expected_year}")
        variants.append(slug)
        for candidate_slug in variants:
            url = f"{LETTERBOXD_BASE}/film/{candidate_slug}/"
            key = url.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(url)
            if len(out) >= LETTERBOXD_DIRECT_URL_LIMIT:
                return out
    return out


def _letterboxd_search_urls(query: str) -> list[str]:
    query = clean_amc_title(query)
    if not query:
        return []
    # The films-only endpoint is normally enough.  The generic endpoint is a
    # single fallback, not an additional broad crawl.
    search_urls = [
        f"{LETTERBOXD_BASE}/search/films/{quote(query, safe='')}/",
        f"{LETTERBOXD_BASE}/s/search/{quote(query, safe='')}/",
    ]
    out: list[str] = []
    seen: set[str] = set()
    for search_url in search_urls:
        html_text = _fetch_text(search_url, timeout=4, attempts=1)
        if not html_text:
            continue
        soup = BeautifulSoup(html_text, "html.parser")
        for link in soup.find_all("a", href=True):
            href = str(link.get("href", ""))
            candidate = urljoin(LETTERBOXD_BASE, href)
            candidate = candidate.split("?", 1)[0].split("#", 1)[0]
            if not candidate.endswith("/"):
                candidate += "/"
            if not LETTERBOXD_FILM_RE.match(candidate):
                continue
            key = candidate.casefold()
            if key not in seen:
                seen.add(key)
                out.append(candidate)
            if len(out) >= LETTERBOXD_SEARCH_RESULT_LIMIT:
                return out
        if out:
            break
    return out


def _letterboxd_context_cache_key(context: MovieContext, reference_year: int) -> tuple[object, ...]:
    return (
        context.display_title.casefold(),
        context.canonical_title.casefold(),
        context.expected_year,
        context.runtime_min,
        tuple(sorted(context.spoken_languages)),
        context.relax_runtime,
        reference_year,
    )


def _score_letterboxd_metadata(
    meta: LetterboxdPageMetadata,
    context: MovieContext,
    reference_year: int,
) -> tuple[float | None, str | None]:
    title_score = max(
        (_title_similarity(meta.title, c) for c in _context_title_variants(context)),
        default=0.0,
    )
    if title_score < 0.90:
        return None, "title"
    if context.expected_year is not None and meta.release_year is not None:
        if abs(meta.release_year - context.expected_year) > 3:
            return None, "year"
    if (
        context.runtime_min is not None
        and meta.runtime_min is not None
        and not context.relax_runtime
        and abs(meta.runtime_min - context.runtime_min) > 20
    ):
        return None, "runtime"

    if context.expected_year is not None:
        year_score = 0.0 if meta.release_year is None else max(
            -25.0, 12.0 - 4.0 * abs(meta.release_year - context.expected_year)
        )
    else:
        year_score = 0.0 if meta.release_year is None else max(
            -30.0, 10.0 - 2.5 * abs(meta.release_year - reference_year)
        )
    runtime_bonus = 0.0
    if context.runtime_min is not None and meta.runtime_min is not None and not context.relax_runtime:
        runtime_bonus = max(0.0, 10.0 - abs(meta.runtime_min - context.runtime_min) / 2.0)
    poster_bonus = 2.0 if meta.poster_url else 0.0
    return title_score * 100.0 + year_score + runtime_bonus + poster_bonus, None


def _best_letterboxd_metadata(
    context: MovieContext,
    reference_year: int,
) -> tuple[LetterboxdPageMetadata | None, list[str]]:
    """Return the best independently validated Letterboxd/TMDB identity.

    Fast path: deterministic film slugs. Search is used only when those fail.
    Exact/near-exact title is mandatory, year evidence is honored, and runtime
    is a veto for ordinary features. Concert/live-event presentations relax
    runtime because theatrical cuts can differ from catalogue runtime.
    """
    cache_key = _letterboxd_context_cache_key(context, reference_year)
    cached = _LETTERBOXD_RESULT_CACHE.get(cache_key)
    if cached is not None:
        meta, rejected = cached
        return meta if isinstance(meta, LetterboxdPageMetadata) else None, list(rejected)

    rejected: list[str] = []
    ranked: list[tuple[float, LetterboxdPageMetadata]] = []
    seen: set[str] = set()

    def consider(url: str) -> LetterboxdPageMetadata | None:
        key = url.casefold()
        if key in seen:
            return None
        seen.add(key)
        meta = _letterboxd_page_metadata(url)
        if meta is None:
            return None
        score, reason = _score_letterboxd_metadata(meta, context, reference_year)
        if score is None:
            rejected.append(f"{url}:{reason}")
            return None
        ranked.append((score, meta))
        return meta

    # Deterministic URLs are much cheaper than a search crawl and solve most
    # modern event/remake slugs directly.
    for url in _letterboxd_direct_urls(context):
        meta = consider(url)
        if meta is not None and meta.poster_url:
            # A direct exact identity with artwork is already the desired final
            # fallback.  Avoid unnecessary search-page/network work.
            _LETTERBOXD_RESULT_CACHE[cache_key] = (meta, list(rejected))
            return meta, rejected

    # Search only the first two distinct title variants, stopping after enough
    # candidates have been seen.  This keeps obscure-title recovery useful but
    # predictably bounded.
    if not ranked:
        for query in _context_title_variants(context)[:2]:
            for url in _letterboxd_search_urls(query):
                consider(url)
                if len(seen) >= LETTERBOXD_SEARCH_RESULT_LIMIT + LETTERBOXD_DIRECT_URL_LIMIT:
                    break
            if ranked:
                break

    if not ranked:
        result = (None, list(rejected))
        _LETTERBOXD_RESULT_CACHE[cache_key] = result
        return None, rejected
    ranked.sort(key=lambda item: item[0], reverse=True)
    best = ranked[0][1]
    _LETTERBOXD_RESULT_CACHE[cache_key] = (best, list(rejected))
    return best, rejected


def _image_via_letterboxd(
    context: MovieContext,
    reference_year: int,
) -> tuple[str | None, str | None, list[str]]:
    """Final poster fallback using Letterboxd's TMDB-backed catalogue."""
    meta, rejected = _best_letterboxd_metadata(context, reference_year)
    if meta is None or not meta.poster_url:
        return None, None, rejected
    return meta.poster_url, meta.url, rejected

def _normalized_match_text(value: str) -> str:
    value = clean_amc_title(value)
    # Wikipedia often disambiguates films with more context than just
    # "(2026 film)" -- for example "(2026 American film)",
    # "(British drama film)", or "(TV film)". Any trailing parenthetical
    # whose medium word is explicitly film/movie is metadata, not part of the
    # base movie title, so remove it for identity comparison. Arbitrary
    # parentheticals that do not say film/movie are preserved.
    value = re.sub(
        r"\s*\([^()]*\b(?:film|movie)\s*\)\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s*\(\d{4}\)\s*$", "", value)
    value = value.replace("&", " and ").replace("*", "").replace("’", "'")
    value = value.casefold()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _title_similarity(a: str, b: str) -> float:
    aa = _normalized_match_text(a)
    bb = _normalized_match_text(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    return SequenceMatcher(None, aa, bb).ratio()


def _candidate_identity_score(candidate_title: str, movie_title: str) -> float:
    variants = candidate_title_variants(movie_title)
    return max(
        (_title_similarity(candidate_title, variant) for variant in variants),
        default=0.0,
    )


def _pages_from_query(data: dict | None) -> list[dict]:
    if not isinstance(data, dict):
        return []
    pages = (data.get("query") or {}).get("pages") or {}
    if isinstance(pages, dict):
        return [p for p in pages.values() if isinstance(p, dict)]
    if isinstance(pages, list):
        return [p for p in pages if isinstance(p, dict)]
    return []


def _image_from_page(page: dict) -> str | None:
    for value in (
        (page.get("thumbnail") or {}).get("source"),
        (page.get("original") or {}).get("source"),
    ):
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            return value
    return None


def _wikipedia_page_wikitext(page_title: str) -> str | None:
    """Return the current main-slot wikitext for one English Wikipedia page.

    PageImages can legitimately be empty even when a film infobox visibly has a
    poster.  Wikitext is used only after the page itself has already passed the
    normal title/year/runtime/language validation.
    """
    page_title = clean_amc_title(page_title)
    if not page_title:
        return None
    data = _api_json(
        ENWIKI_API,
        action="query",
        format="json",
        formatversion=2,
        redirects=1,
        prop="revisions",
        rvprop="content",
        rvslots="main",
        titles=page_title,
    )
    for page in _pages_from_query(data):
        revisions = page.get("revisions") or []
        if not revisions or not isinstance(revisions[0], dict):
            continue
        slots = revisions[0].get("slots") or {}
        main = slots.get("main") or {}
        content = main.get("content")
        if isinstance(content, str) and content.strip():
            return content
        # Compatibility with older MediaWiki response shapes.
        legacy = revisions[0].get("*")
        if isinstance(legacy, str) and legacy.strip():
            return legacy
    return None


def _infobox_image_filename(wikitext: str | None) -> str | None:
    """Extract a film infobox image filename without selecting body photos.

    This intentionally reads only an ``image``/``poster`` parameter near the
    beginning of the article.  It does not search arbitrary File: links, which
    protects against selecting premiere/event photos instead of the poster.
    """
    if not isinstance(wikitext, str) or not wikitext.strip():
        return None
    # Film infoboxes are at the top.  Limiting the scan also prevents unrelated
    # template parameters later in a long article from being mistaken as a poster.
    head = wikitext[:20000]
    match = re.search(
        r"(?im)^\s*\|\s*(?:image|poster)\s*=\s*(.+?)\s*$",
        head,
    )
    if not match:
        return None
    value = match.group(1).strip()
    if not value or value.casefold() in {"none", "n/a", "na"}:
        return None

    # Common forms are a bare filename or [[File:Name.jpg|...]].
    wrapped = re.match(
        r"(?is)^\[\[(?:File|Image):\s*([^|\]]+)",
        value,
    )
    if wrapped:
        value = wrapped.group(1).strip()
    else:
        # Strip simple HTML comments/references and harmless surrounding markup.
        value = re.sub(r"<!--.*?-->", "", value, flags=re.DOTALL).strip()
        value = re.sub(r"<ref\b[^>]*>.*?</ref>", "", value, flags=re.DOTALL | re.IGNORECASE).strip()
        value = re.sub(r"<ref\b[^>]*/>", "", value, flags=re.IGNORECASE).strip()
        value = re.sub(r"^\[\[(?:File|Image):\s*", "", value, flags=re.IGNORECASE).strip()
        value = value.split("|", 1)[0].replace("]]", "").strip()

    value = re.sub(r"^(?:File|Image):\s*", "", value, flags=re.IGNORECASE).strip()
    if not value or any(token in value for token in ("{{", "}}", "<", ">")):
        return None
    # Reject obvious non-image garbage while allowing the extensions Wikipedia
    # commonly uses for posters.
    if not re.search(r"\.(?:jpe?g|png|webp|gif|svg|tiff?)$", value, flags=re.IGNORECASE):
        return None
    return value


def _wikipedia_file_thumbnail(filename: str, width: int = WIKIMEDIA_POSTER_WIDTH) -> str | None:
    filename = clean_amc_title(filename)
    if not filename:
        return None
    title = filename if filename.casefold().startswith(("file:", "image:")) else f"File:{filename}"
    data = _api_json(
        ENWIKI_API,
        action="query",
        format="json",
        formatversion=2,
        prop="imageinfo",
        iiprop="url",
        iiurlwidth=width,
        titles=title,
    )
    for page in _pages_from_query(data):
        info = page.get("imageinfo") or []
        if not info or not isinstance(info[0], dict):
            continue
        for key in ("thumburl", "url"):
            value = info[0].get(key)
            if isinstance(value, str) and value.startswith(("https://", "http://")):
                return value
    return None


def _image_from_validated_wikipedia_infobox(page_title: str) -> str | None:
    wikitext = _wikipedia_page_wikitext(page_title)
    filename = _infobox_image_filename(wikitext)
    if not filename:
        return None
    return _wikipedia_file_thumbnail(filename)


def _page_qid(page: dict) -> str | None:
    qid = (page.get("pageprops") or {}).get("wikibase_item")
    return qid if isinstance(qid, str) and QID_RE.fullmatch(qid) else None


def _page_is_disambiguation(page: dict) -> bool:
    return "disambiguation" in (page.get("pageprops") or {})


def _wikipedia_pages_for_titles(titles: list[str]) -> list[dict]:
    clean_titles: list[str] = []
    seen: set[str] = set()
    for title in titles:
        value = clean_amc_title(title)
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            clean_titles.append(value)

    out: list[dict] = []
    for start in range(0, len(clean_titles), 40):
        batch = clean_titles[start : start + 40]
        data = _api_json(
            ENWIKI_API,
            action="query",
            format="json",
            formatversion=2,
            redirects=1,
            prop="pageimages|pageprops",
            piprop="thumbnail|original",
            pithumbsize=WIKIMEDIA_POSTER_WIDTH,
            pilicense="any",
            titles="|".join(batch),
        )
        out.extend(_pages_from_query(data))
    return out


def _wikipedia_search_pages(query: str, limit: int = 10) -> list[dict]:
    query = clean_amc_title(query)
    if not query:
        return []
    data = _api_json(
        ENWIKI_API,
        action="query",
        format="json",
        formatversion=2,
        generator="search",
        gsrnamespace=0,
        gsrsearch=query,
        gsrlimit=limit,
        prop="pageimages|pageprops",
        piprop="thumbnail|original",
        pithumbsize=WIKIMEDIA_POSTER_WIDTH,
        pilicense="any",
    )
    pages = _pages_from_query(data)
    return sorted(
        pages,
        key=lambda p: p.get("index") if isinstance(p.get("index"), int) else 999999,
    )


def _extract_imdb_id(row) -> str | None:
    candidates: list[str] = []
    link = row.select_one('a[href*="imdb.com/title/"]')
    if link is not None:
        candidates.append(str(link.get("href", "")))
    # ``postprocess_report`` preserves exact provider URLs even when a numeric
    # rating is not published yet.  Use that hidden identity bridge too.
    for cell in row.find_all(["td", "th"], recursive=False):
        value = cell.get("data-source-url")
        if isinstance(value, str) and "imdb.com/title/" in value:
            candidates.append(value)
    for value in candidates:
        match = IMDB_ID_RE.search(value)
        if match:
            return match.group(1).lower()
    return None


def _runtime_minutes(text: str) -> int | None:
    text = clean_amc_title(text).casefold()
    h = re.search(r"\b(\d+)\s*h", text)
    m = re.search(r"\b(\d+)\s*m", text)
    if h:
        return int(h.group(1)) * 60 + (int(m.group(1)) if m else 0)
    mm = re.search(r"\b(\d{2,3})\s*(?:min|mins|minute|minutes)\b", text)
    return int(mm.group(1)) if mm else None


def _spoken_languages(text: str) -> frozenset[str]:
    values: set[str] = set()
    for match in re.finditer(
        r"\b([A-Za-z][A-Za-z '&-]{1,40}?)\s+Spoken\s+with\b",
        text or "",
        flags=re.IGNORECASE,
    ):
        language = clean_amc_title(match.group(1)).casefold()
        # The regex can start after punctuation/theater text only because the
        # showtime format is bracketed; keep just the last plausible words.
        language = re.sub(r"^.*?[\[\],•:]\s*", "", language)
        if language:
            values.add(language)
    return frozenset(values)


def _row_movie_context(row, display_title: str, reference_year: int) -> MovieContext:
    info = analyze_movie_title(display_title, reference_year=reference_year)
    cells = row.find_all(["th", "td"], recursive=False)
    row_text = row.get_text(" ", strip=True)

    runtime = None
    for cell in reversed(cells):
        runtime = _runtime_minutes(cell.get_text(" ", strip=True))
        if runtime is not None:
            break

    languages = _spoken_languages(row_text)
    relax_runtime = bool(
        re.search(
            r"\b(?:q\s*&\s*a|double\s+feature|marathon|live\s+event|cast\s+member|filmmaker)\b",
            display_title,
            flags=re.IGNORECASE,
        )
        or _matches_any_pattern(display_title.casefold(), MUSIC_EVENT_TITLE_PATTERNS)
    )

    return MovieContext(
        display_title=display_title,
        canonical_title=info.canonical_title,
        expected_year=info.explicit_year or info.inferred_release_year,
        runtime_min=runtime,
        spoken_languages=languages,
        relax_runtime=relax_runtime,
    )


def _wikidata_qids_for_imdb(imdb_id: str) -> list[str]:
    if not re.fullmatch(r"tt\d{5,12}", imdb_id or "", flags=re.IGNORECASE):
        return []
    data = _api_json(
        WIKIDATA_API,
        action="query",
        format="json",
        list="search",
        srnamespace=0,
        srlimit=8,
        srsearch=f"haswbstatement:P345={imdb_id.lower()}",
    )
    try:
        hits = data["query"]["search"] if data else []
    except (KeyError, TypeError):
        return []
    return [
        str(hit.get("title"))
        for hit in hits
        if QID_RE.fullmatch(str(hit.get("title", "")))
    ]


def _wikidata_qids_for_title(title: str, limit: int = 12) -> list[str]:
    title = clean_amc_title(title)
    if not title:
        return []
    data = _api_json(
        WIKIDATA_API,
        action="wbsearchentities",
        format="json",
        language="en",
        uselang="en",
        type="item",
        limit=limit,
        search=title,
    )
    hits = data.get("search", []) if isinstance(data, dict) else []
    out: list[str] = []
    for hit in hits:
        qid = str(hit.get("id", ""))
        if not QID_RE.fullmatch(qid):
            continue
        names = [clean_amc_title(str(hit.get("label", "")))]
        match = hit.get("match")
        if isinstance(match, dict):
            names.append(clean_amc_title(str(match.get("text", ""))))
        # wbsearchentities may find an item through an alias whose canonical
        # English label is different.  Consider the matched alias text as well
        # instead of discarding the item before its full aliases/claims can be
        # validated.
        if max((_title_similarity(name, title) for name in names if name), default=0.0) >= 0.82:
            out.append(qid)
    return out

def _wikidata_entities(qids: list[str]) -> list[dict]:
    clean_qids: list[str] = []
    seen: set[str] = set()
    for qid in qids:
        if QID_RE.fullmatch(qid) and qid not in seen:
            seen.add(qid)
            clean_qids.append(qid)
    if not clean_qids:
        return []

    out_by_id: dict[str, dict] = {}
    for start in range(0, len(clean_qids), 40):
        batch = clean_qids[start : start + 40]
        data = _api_json(
            WIKIDATA_API,
            action="wbgetentities",
            format="json",
            ids="|".join(batch),
            props="claims|labels|aliases|descriptions|sitelinks",
            languages="en",
            sitefilter="enwiki",
        )
        entities = data.get("entities", {}) if isinstance(data, dict) else {}
        if isinstance(entities, dict):
            for qid, entity in entities.items():
                if isinstance(entity, dict) and not entity.get("missing"):
                    out_by_id[qid] = entity
    return [out_by_id[qid] for qid in clean_qids if qid in out_by_id]


def _claim_values(entity: dict, property_id: str) -> list[object]:
    out: list[object] = []
    for claim in (entity.get("claims") or {}).get(property_id) or []:
        try:
            value = claim["mainsnak"]["datavalue"]["value"]
        except (KeyError, TypeError):
            continue
        out.append(value)
    return out


def _claim_string_values(entity: dict, property_id: str) -> list[str]:
    return [v for v in _claim_values(entity, property_id) if isinstance(v, str) and v]


def _entity_rt_movie_url(entity: dict) -> str | None:
    """Return an exact Rotten Tomatoes movie URL from Wikidata P1258."""
    for value in _claim_string_values(entity, "P1258"):
        value = value.strip().lstrip("/")
        if re.fullmatch(r"m/[0-9A-Za-z_][-0-9A-Za-z_'.]*", value):
            url = f"https://www.rottentomatoes.com/{value}"
            if RT_MOVIE_URL_RE.match(url):
                return url
    return None


def _entity_has_imdb_id(entity: dict, imdb_id: str | None) -> bool:
    """Verify an IMDb bridge result instead of trusting Wikidata search ranking.

    MediaWiki ``list=search`` can occasionally return textually related items
    for a haswbstatement query.  We therefore require the returned entity to
    contain the exact P345 value before using it as film identity evidence.
    """
    if not imdb_id:
        return False
    wanted = imdb_id.casefold()
    return wanted in {value.casefold() for value in _claim_string_values(entity, "P345")}


def _claim_entity_values(entity: dict, property_id: str) -> list[str]:
    out: list[str] = []
    for value in _claim_values(entity, property_id):
        if isinstance(value, dict):
            qid = value.get("id")
            if isinstance(qid, str) and QID_RE.fullmatch(qid):
                out.append(qid)
    return out


def _entity_is_filmish(entity: dict) -> bool:
    # Q11424 is the general Wikidata item for film.  Description fallback is
    # useful because some new items use a more specific film class directly.
    if "Q11424" in _claim_entity_values(entity, "P31"):
        return True
    description = (((entity.get("descriptions") or {}).get("en") or {}).get("value") or "")
    description = str(description).casefold()
    return any(
        word in description
        for word in ("film", "movie", "documentary", "motion picture", "concert film")
    )


def _entity_english_names(entity: dict) -> list[str]:
    values: list[str] = []
    label = ((entity.get("labels") or {}).get("en") or {}).get("value")
    if isinstance(label, str):
        values.append(label)
    for alias in (entity.get("aliases") or {}).get("en") or []:
        value = alias.get("value") if isinstance(alias, dict) else None
        if isinstance(value, str):
            values.append(value)
    return values


def _context_title_variants(context: MovieContext) -> list[str]:
    """Return generic metadata identity candidates for an AMC presentation.

    AMC can wrap an existing feature in generic event wording or presenter
    branding that metadata providers do not use.  Reuse the shared title
    candidate generator so poster validation and Audience Focus agree about the
    underlying film.  The canonical form stays first, and no movie/person
    aliases are encoded here.
    """
    values = [context.canonical_title]
    values.extend(candidate_title_variants(context.display_title))

    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = clean_amc_title(value)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


def _entity_title_score(entity: dict, context: MovieContext) -> float:
    variants = _context_title_variants(context)
    return max(
        (
            _title_similarity(name, variant)
            for name in _entity_english_names(entity)
            for variant in variants
        ),
        default=0.0,
    )


def _entity_release_years(entity: dict) -> list[int]:
    years: list[int] = []
    for value in _claim_values(entity, "P577"):
        if not isinstance(value, dict):
            continue
        raw = value.get("time")
        if not isinstance(raw, str):
            continue
        match = re.match(r"^[+-](\d{4,})-", raw)
        if match:
            year = int(match.group(1))
            if 1888 <= year <= 2200:
                years.append(year)
    return years


def _entity_runtimes(entity: dict) -> list[int]:
    minutes: list[int] = []
    for value in _claim_values(entity, "P2047"):
        if not isinstance(value, dict):
            continue
        try:
            amount = abs(float(value.get("amount")))
        except (TypeError, ValueError):
            continue
        unit = str(value.get("unit", ""))
        if unit.endswith("/Q25235"):  # hour
            amount *= 60
        elif unit.endswith("/Q11574"):  # second
            amount /= 60
        # Q7727 is minute; unknown units are conservatively treated as minutes
        # because film-duration claims overwhelmingly use minutes.
        if 1 <= amount <= 1000:
            minutes.append(int(round(amount)))
    return minutes


def _wikidata_labels(qids: list[str]) -> dict[str, str]:
    missing = [qid for qid in qids if QID_RE.fullmatch(qid) and qid not in _LABEL_CACHE]
    for start in range(0, len(missing), 40):
        batch = missing[start : start + 40]
        data = _api_json(
            WIKIDATA_API,
            action="wbgetentities",
            format="json",
            ids="|".join(batch),
            props="labels",
            languages="en",
        )
        entities = data.get("entities", {}) if isinstance(data, dict) else {}
        if isinstance(entities, dict):
            for qid, entity in entities.items():
                label = (((entity or {}).get("labels") or {}).get("en") or {}).get("value")
                if isinstance(label, str):
                    _LABEL_CACHE[qid] = clean_amc_title(label).casefold()
    return {qid: _LABEL_CACHE[qid] for qid in qids if qid in _LABEL_CACHE}


def _entity_languages(entity: dict) -> set[str]:
    qids = _claim_entity_values(entity, "P364")
    return set(_wikidata_labels(qids).values())


def _entity_claim_labels(entity: dict, property_ids: tuple[str, ...]) -> set[str]:
    """Return normalized English labels for entity-valued Wikidata claims."""
    qids: list[str] = []
    seen: set[str] = set()
    for property_id in property_ids:
        for qid in _claim_entity_values(entity, property_id):
            if qid not in seen:
                seen.add(qid)
                qids.append(qid)
    return set(_wikidata_labels(qids).values())


def _title_version_languages(display_title: str) -> set[str]:
    """Infer a screening's language only from an explicit *version* suffix.

    AMC sometimes appends a bracketed or dash-delimited language qualifier
    before providers publish original-language metadata. Reuse the existing
    audience-language vocabulary rather than maintaining title-specific rules.
    Ordinary film names that happen to mention a language (e.g. a story about
    a country) must not be interpreted as language evidence.
    """
    known = {language for group in LANGUAGE_FOCUS_GROUPS.values() for language in group}
    if not known:
        return set()

    # Longer multiword variants first (e.g. "mandarin chinese" before "chinese").
    languages = "|".join(re.escape(name) for name in sorted(known, key=len, reverse=True))
    qualifier = re.compile(
        rf"(?:in\s+|original\s+)?(?P<language>{languages})"
        rf"(?:[\s-]+(?:language|version|dubbed|dub|subtitled))?"
        rf"(?:\s*(?:[,/]|with|w/?)\s*english\s*(?:subtitles?|subs|cc))?",
        flags=re.IGNORECASE,
    )
    title = clean_amc_title(display_title)
    # Display titles may append a release year *after* the language version.
    # Strip only that generic four-digit suffix, never a film title or year in
    # the middle of the name. This remains valid for future calendar years.
    title = re.sub(r"\s*\(\d{4}\)\s*$", "", title)
    candidates = []
    for pattern in (
        r"\(([^()]*)\)\s*$",
        r"\[([^\[\]]*)\]\s*$",
        r"\s+[-–—]\s+([^()\[\]]{1,80})\s*$",
    ):
        match = re.search(pattern, title)
        if match:
            candidates.append(match.group(1).strip())

    found = set()
    for candidate in candidates:
        match = qualifier.fullmatch(candidate)
        if match:
            found.add(match.group("language").casefold())
    return found


def _add_language_focus(
    tags: set[str],
    reasons: list[str],
    languages: set[str],
    *,
    source_label: str,
) -> None:
    """Map explicit language evidence to neutral cultural/market tags."""
    mapped_languages: set[str] = set()
    for tag, known_languages in LANGUAGE_FOCUS_GROUPS.items():
        matched = sorted(languages & known_languages)
        if not matched:
            continue
        tags.add(tag)
        mapped_languages.update(matched)
        reasons.append(f"{tag}: {source_label} {', '.join(matched)}")

    # Preserve useful context for languages not in the common mappings.  English
    # is deliberately omitted because it is the default for many AMC listings.
    for language in sorted(languages - mapped_languages - {"english"}):
        clean = clean_amc_title(language)
        if not clean:
            continue
        label = f"{clean.title()}-language"
        tags.add(label)
        reasons.append(f"{label}: {source_label} {clean}")


def _add_entity_market_focus(
    tags: set[str],
    reasons: list[str],
    languages: set[str],
    countries: set[str],
) -> None:
    """Add Wikidata market tags while suppressing contradictory metadata.

    A same-title mismatch often presents as a country from one film and a
    language from another.  When both claim types exist, a non-Spanish market
    tag must agree across the two claim sets.  When only one claim type exists,
    it can still be used as the best available structured evidence.
    """
    for tag, known_languages in LANGUAGE_FOCUS_GROUPS.items():
        matched_languages = sorted(languages & known_languages)
        known_countries = COUNTRY_FOCUS_GROUPS.get(tag, set())
        matched_countries = sorted(countries & known_countries)

        if tag == "Spanish-language":
            if matched_languages:
                tags.add(tag)
                reasons.append(
                    f"{tag}: Wikidata original language {', '.join(matched_languages)}"
                )
            continue

        # If both structured claim types are present, require them to agree.
        # This prevents one anomalous language/country claim from dominating.
        if languages and countries:
            if not (matched_languages and matched_countries):
                continue
            tags.add(tag)
            reasons.append(
                f"{tag}: Wikidata language/country agree "
                f"({', '.join(matched_languages)}; {', '.join(matched_countries)})"
            )
        elif matched_languages:
            tags.add(tag)
            reasons.append(
                f"{tag}: Wikidata original language {', '.join(matched_languages)}"
            )
        elif matched_countries:
            tags.add(tag)
            reasons.append(f"{tag}: country of origin {', '.join(matched_countries)}")


def _add_wikipedia_market_focus(
    tags: set[str],
    reasons: list[str],
    focus_text: str,
) -> None:
    """Add market tags only from explicit film/language wording on Wikipedia."""
    for tag, patterns in WIKIPEDIA_MARKET_PATTERNS.items():
        if _matches_any_pattern(focus_text, patterns):
            tags.add(tag)
            reasons.append(f"{tag}: explicit Wikipedia film/language context")


def _explicit_focus_text(entity: dict) -> str:
    """Text that may legitimately describe a film's themes/audience focus."""
    description = (((entity.get("descriptions") or {}).get("en") or {}).get("value") or "")
    # P136 = genre, P921 = main subject, P840 = narrative location. Narrative
    # location is only supporting text; generic geography alone is deliberately
    # not a focus trigger unless another explicit cultural pattern is present.
    genre_subject_labels = _entity_claim_labels(entity, ("P136", "P921", "P840"))
    parts = [str(description)] + sorted(genre_subject_labels)
    return " | ".join(clean_amc_title(part).casefold() for part in parts if part)


def _wikipedia_focus_pages(titles: list[str]) -> dict[str, dict]:
    """Fetch intro/category context for already-identified English Wikipedia pages.

    This is intentionally a batched enrichment pass, separate from poster search,
    so we do not inflate every candidate lookup with large extracts/categories.
    """
    clean_titles: list[str] = []
    seen: set[str] = set()
    for title in titles:
        value = clean_amc_title(title)
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            clean_titles.append(value)

    out: dict[str, dict] = {}
    for start in range(0, len(clean_titles), 40):
        batch = clean_titles[start : start + 40]
        data = _api_json(
            ENWIKI_API,
            action="query",
            format="json",
            formatversion=2,
            redirects=1,
            prop="categories|extracts",
            cllimit=100,
            clshow="!hidden",
            exintro=1,
            explaintext=1,
            titles="|".join(batch),
        )
        pages = _pages_from_query(data)
        page_by_title: dict[str, dict] = {}
        for page in pages:
            title = clean_amc_title(str(page.get("title", "")))
            if title:
                page_by_title[title.casefold()] = page
                out[title.casefold()] = page

        # MediaWiki returns canonical page titles after redirects. Preserve the
        # requested alias too so a stored data-wikipedia-page can still find its
        # enrichment page even when it happens to be a redirect.
        query = data.get("query", {}) if isinstance(data, dict) else {}
        redirects = query.get("redirects", []) if isinstance(query, dict) else []
        if isinstance(redirects, list):
            for item in redirects:
                if not isinstance(item, dict):
                    continue
                src = clean_amc_title(str(item.get("from", "")))
                dst = clean_amc_title(str(item.get("to", "")))
                page = page_by_title.get(dst.casefold()) if dst else None
                if src and page is not None:
                    out[src.casefold()] = page
    return out


def _page_focus_text(page: dict | None) -> str:
    """Return explicit film-level wording from a selected Wikipedia article."""
    if not isinstance(page, dict):
        return ""
    parts: list[str] = []
    extract = clean_amc_title(str(page.get("extract", "")))
    if extract:
        parts.append(extract)
    categories = page.get("categories") or []
    if isinstance(categories, list):
        for item in categories:
            if not isinstance(item, dict):
                continue
            title = clean_amc_title(str(item.get("title", "")))
            title = re.sub(r"^Category:\s*", "", title, flags=re.IGNORECASE)
            if title:
                parts.append(title)
    return " | ".join(part.casefold() for part in parts if part)


def _matches_any_pattern(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def classify_audience_focus(
    context: MovieContext,
    entity: dict | None = None,
    *,
    supplemental_focus_text: str = "",
    supplemental_languages: frozenset[str] = frozenset(),
    supplemental_genres: frozenset[str] = frozenset(),
) -> AudienceFocusResult:
    """
    Describe strong audience/cultural signals without judging film quality.

    Important: this function does not inspect cast/crew identities.  Cultural
    identity tags require explicit film metadata, while international-market
    tags may use language/country-of-origin metadata.
    """
    tags: set[str] = set()
    reasons: list[str] = []

    amc_languages = set(context.spoken_languages)
    if amc_languages:
        _add_language_focus(tags, reasons, amc_languages, source_label="AMC lists")

    # A screening/version label is direct evidence of the audience's language
    # even when RT/Wikidata are missing or still describe a different edit.
    # No movie-title exceptions and no broad keyword matching within film names.
    version_languages = _title_version_languages(context.display_title)
    if version_languages:
        _add_language_focus(
            tags, reasons, version_languages,
            source_label="AMC title version qualifier indicates",
        )

    if supplemental_languages:
        _add_language_focus(
            tags, reasons, set(supplemental_languages),
            source_label="Rotten Tomatoes original language",
        )

    if _matches_any_pattern(context.display_title.casefold(), MUSIC_EVENT_TITLE_PATTERNS):
        tags.add("Music/Concert")
        reasons.append("Music/Concert: explicit cinema-event wording in title")
    if any(
        re.search(r"\b(?:music|concert)\b", genre, flags=re.IGNORECASE)
        for genre in supplemental_genres
    ):
        tags.add("Music/Concert")
        reasons.append("Music/Concert: Rotten Tomatoes genre metadata")

    # Title wording is used only for a small set of unambiguous faith terms.
    # This is a generic fallback for new releases whose Wikidata/Wikipedia
    # records have not yet accumulated subject or genre metadata.
    title_focus_text = context.canonical_title.casefold()
    if _matches_any_pattern(title_focus_text, FAITH_TITLE_PATTERNS):
        tags.add("Faith-oriented")
        reasons.append("Faith-oriented: explicit religious wording in title")

    if entity is not None:
        entity_languages = _entity_languages(entity)
        countries = _entity_claim_labels(entity, ("P495",))
        _add_entity_market_focus(tags, reasons, entity_languages, countries)

        focus_text = _explicit_focus_text(entity)
        if _matches_any_pattern(focus_text, MUSIC_EVENT_METADATA_PATTERNS):
            tags.add("Music/Concert")
            reasons.append("Music/Concert: explicit music/concert metadata")
        if _matches_any_pattern(focus_text, FAITH_TEXT_PATTERNS):
            tags.add("Faith-oriented")
            reasons.append("Faith-oriented: explicit faith/religion metadata")
        if _matches_any_pattern(focus_text, LGBTQ_TEXT_PATTERNS):
            tags.add("LGBTQ+-focused")
            reasons.append("LGBTQ+-focused: explicit LGBTQ+ metadata")
        if _matches_any_pattern(focus_text, BLACK_FOCUS_TEXT_PATTERNS):
            tags.add("Black-focused")
            reasons.append("Black-focused: explicit Black/African-American metadata")
        if _matches_any_pattern(focus_text, LATINO_FOCUS_TEXT_PATTERNS):
            tags.add("Latino/Hispanic-focused")
            reasons.append("Latino/Hispanic-focused: explicit Latino/Hispanic metadata")

        organizations = _entity_claim_labels(entity, ("P272", "P750"))

        faith_orgs = [
            FAITH_ORGANIZATION_NAMES[name]
            for name in sorted(organizations)
            if name in FAITH_ORGANIZATION_NAMES
        ]
        if faith_orgs:
            tags.add("Faith-oriented")
            reasons.append(
                "Faith-oriented: "
                + ", ".join(faith_orgs)
                + " production/distribution signal"
            )

        black_focus_orgs = [
            BLACK_FOCUS_ORGANIZATION_NAMES[name]
            for name in sorted(organizations)
            if name in BLACK_FOCUS_ORGANIZATION_NAMES
        ]
        if black_focus_orgs:
            tags.add("Black-focused")
            reasons.append(
                "Black-focused: "
                + ", ".join(black_focus_orgs)
                + " production/distribution signal"
            )

        latino_focus_orgs = [
            LATINO_FOCUS_ORGANIZATION_NAMES[name]
            for name in sorted(organizations)
            if name in LATINO_FOCUS_ORGANIZATION_NAMES
        ]
        if latino_focus_orgs:
            tags.add("Latino/Hispanic-focused")
            reasons.append(
                "Latino/Hispanic-focused: "
                + ", ".join(latino_focus_orgs)
                + " production/distribution signal"
            )

    if supplemental_focus_text:
        focus_text = supplemental_focus_text.casefold()
        _add_wikipedia_market_focus(tags, reasons, focus_text)
        if _matches_any_pattern(focus_text, MUSIC_EVENT_METADATA_PATTERNS):
            tags.add("Music/Concert")
            reasons.append("Music/Concert: explicit Wikipedia film context")
        if _matches_any_pattern(focus_text, FAITH_TEXT_PATTERNS):
            tags.add("Faith-oriented")
            reasons.append("Faith-oriented: explicit Wikipedia film context")
        if _matches_any_pattern(focus_text, WIKIPEDIA_LGBTQ_FOCUS_PATTERNS):
            tags.add("LGBTQ+-focused")
            reasons.append("LGBTQ+-focused: explicit Wikipedia film context")
        if _matches_any_pattern(focus_text, WIKIPEDIA_BLACK_FOCUS_PATTERNS):
            tags.add("Black-focused")
            reasons.append("Black-focused: explicit Wikipedia film context")
        if _matches_any_pattern(focus_text, WIKIPEDIA_LATINO_FOCUS_PATTERNS):
            tags.add("Latino/Hispanic-focused")
            reasons.append("Latino/Hispanic-focused: explicit Wikipedia film context")

    if not tags:
        return AudienceFocusResult(tags=("General",), reasons=("No strong niche-audience signal found",))

    order = {tag: i for i, tag in enumerate(AUDIENCE_TAG_ORDER)}
    sorted_tags = tuple(sorted(tags, key=lambda tag: (order.get(tag, 999), tag.casefold())))
    # Deduplicate reasons while preserving first-seen evidence order.
    deduped_reasons = tuple(dict.fromkeys(reasons))
    return AudienceFocusResult(tags=sorted_tags, reasons=deduped_reasons)


def _audience_entity_for_context(
    context: MovieContext,
    *,
    imdb_id: str | None,
    wikipedia_page: str | None,
) -> dict | None:
    """Find a trustworthy Wikidata entity for audience-focus enrichment."""
    # Best case: poster validation already selected a specific Wikipedia page.
    if wikipedia_page:
        pages = _wikipedia_pages_for_titles([wikipedia_page])
        qids = [_page_qid(page) for page in pages]
        entities = _wikidata_entities([qid for qid in qids if qid])
        entity, _ = _best_entity_for_context(entities, context, imdb_id)
        if entity is not None:
            return entity

    # If AMC already supplied a clear non-English language, the local language
    # flag is useful on its own. Avoid another remote search unless a page was
    # already identified. This keeps the feature from materially slowing large
    # weekend reports.
    non_english_amc = set(context.spoken_languages) - {"english"}
    if non_english_amc and "spanish" not in non_english_amc:
        return None

    # IMDb is the cheapest/strongest identity bridge when the report has a
    # title ID.  Only pay for a title-search fallback if that bridge does not
    # produce a context-compatible entity.  This keeps the new column from
    # roughly doubling Wikimedia calls for every English-language movie.
    if imdb_id:
        imdb_entities = [
            entity
            for entity in _wikidata_entities(_wikidata_qids_for_imdb(imdb_id))
            if _entity_has_imdb_id(entity, imdb_id)
        ]
        entity, _ = _best_entity_for_context(imdb_entities, context, imdb_id)
        if entity is not None:
            return entity

    # Use the same generic metadata candidates as the rest of the lookup
    # pipeline.  Audience Focus should not depend on poster resolution having
    # succeeded first; a presentation wrapper may be absent from Wikidata even
    # when the underlying film has complete language/country metadata.
    for candidate in _context_title_variants(context):
        title_entities = _wikidata_entities(_wikidata_qids_for_title(candidate))
        entity, _ = _best_entity_for_context(title_entities, context, None)
        if entity is not None:
            return entity

    return None


def _ensure_audience_focus_style(soup: BeautifulSoup) -> None:
    old = soup.find(id="audience-focus-style")
    if old:
        old.decompose()
    if soup.head is None:
        return
    style = soup.new_tag("style", id="audience-focus-style")
    style.string = AUDIENCE_STYLE
    soup.head.append(style)


# Only language/market labels backed by explicit language evidence can survive a
# temporary metadata outage. Do not perpetuate unverified country-only signals,
# broad thematic categories, or generic/unknown classifications.
_VERIFIED_LANGUAGE_EVIDENCE = (
    "Wikidata original language ",
    "Wikidata language/country agree ",
    "Rotten Tomatoes original language ",
    "AMC lists ",
    "AMC title version qualifier indicates ",
    "explicit Wikipedia film/language context",
)


def _prior_verified_language_index() -> dict[str, list[dict]]:
    """Load verified language evidence from the previous published report.

    Read the already-snapshotted report only once per finalizer invocation.
    The recorded source explanations serve as provenance, not as proof of
    identity; matching the current provider IDs happens separately below.
    """
    index: dict[str, list[dict]] = {}
    if not PREVIOUS_REPORT_PATH.is_file():
        return index
    try:
        previous = BeautifulSoup(
            PREVIOUS_REPORT_PATH.read_text(encoding="utf-8"), "html.parser"
        )
    except (OSError, UnicodeError):
        return index

    for row in previous.select("tr[data-movie-id]"):
        title_el = row.select_one(".movie-cell-title")
        focus_cell = row.select_one("td.audience-focus-cell")
        if title_el is None or focus_cell is None:
            continue
        title_key = _normalized_match_text(title_el.get_text(" ", strip=True))
        if not title_key:
            continue
        published_tags = set(str(focus_cell.get("data-audience-focus", "")).split("|"))
        verified: list[tuple[str, str]] = []
        for reason in str(focus_cell.get("data-audience-reasons", "")).split(" | "):
            tag, separator, evidence = reason.partition(": ")
            if not separator or tag not in published_tags:
                continue
            if tag not in LANGUAGE_FOCUS_GROUPS and not (
                tag.endswith("-language") and len(tag) > len("-language")
            ):
                continue
            if evidence.startswith(_VERIFIED_LANGUAGE_EVIDENCE):
                verified.append((tag, reason))
        if not verified:
            continue
        cells = row.find_all(["th", "td"], recursive=False)
        runtime = _runtime_minutes(cells[5].get_text(" ", strip=True)) if len(cells) >= 6 else None
        index.setdefault(title_key, []).append({
            "imdb_id": _extract_imdb_id(row),
            "rt_url": _extract_rt_url(row),
            "runtime_min": runtime,
            "verified": tuple(verified),
        })
    return index


def _retain_previous_verified_language_focus(
    row,
    context: MovieContext,
    fresh: AudienceFocusResult,
    previous_index: dict[str, list[dict]],
    *,
    entity: dict | None,
    rt_metadata: RtPageMetadata | None,
) -> AudienceFocusResult:
    """Recover an original-language tag only when movie identity is unchanged.

    Never override any present-language evidence (including English), even if
    it yields no market tag. A previous report alone is not a trusted source:
    require an exact IMDb or RT identifier agreement, an unchanged AMC display
    title, and no contradictory identifier or substantial runtime change.
    This protects remakes, alternate versions and changed provider matches.
    """
    if (
        context.spoken_languages
        or _title_version_languages(context.display_title)
        or (rt_metadata is not None and rt_metadata.languages)
        or (entity is not None and _entity_languages(entity))
        or any(tag in LANGUAGE_FOCUS_GROUPS or tag.endswith("-language") for tag in fresh.tags)
    ):
        return fresh

    current_imdb = _extract_imdb_id(row)
    current_rt = _extract_rt_url(row)
    if not current_imdb and not current_rt:
        return fresh
    key = _normalized_match_text(context.display_title)
    matches = []
    for entry in previous_index.get(key, []):
        prior_imdb = entry["imdb_id"]
        prior_rt = entry["rt_url"]
        if current_imdb and prior_imdb and current_imdb != prior_imdb:
            continue
        if current_rt and prior_rt and current_rt.rstrip("/").casefold() != prior_rt.rstrip("/").casefold():
            continue
        if not (
            (current_imdb and prior_imdb and current_imdb == prior_imdb)
            or (current_rt and prior_rt and current_rt.rstrip("/").casefold() == prior_rt.rstrip("/").casefold())
        ):
            continue
        previous_runtime = entry["runtime_min"]
        if (
            not context.relax_runtime
            and context.runtime_min is not None
            and previous_runtime is not None
            and abs(context.runtime_min - previous_runtime) > 3
        ):
            continue
        matches.append(entry)

    # Ambiguous prior rows with different, equally plausible language evidence
    # must not be allowed to choose an arbitrary classification.
    if not matches:
        return fresh
    evidence_sets = {tuple(sorted(set(entry["verified"]))) for entry in matches}
    if len(evidence_sets) != 1:
        return fresh
    verified = matches[0]["verified"]
    tags = set(fresh.tags) - {"General"}
    reasons = [reason for reason in fresh.reasons if reason != "No strong niche-audience signal found"]
    for tag, reason in verified:
        tags.add(tag)
        # Keep the original source wording so another run can verify that the
        # carried-forward classification began with explicit language evidence.
        suffix = " (retained from previous report; fresh language evidence unavailable)"
        reasons.append(reason if suffix in reason else reason + suffix)
    order = {tag: i for i, tag in enumerate(AUDIENCE_TAG_ORDER)}
    return AudienceFocusResult(
        tags=tuple(sorted(tags, key=lambda tag: (order.get(tag, 999), tag.casefold()))),
        reasons=tuple(dict.fromkeys(reasons)),
    )


def _add_audience_focus_column(soup: BeautifulSoup, reference_year: int) -> int:
    """Append/update the Audience Focus column and return classified row count."""
    table = soup.find("table", class_=re.compile(r"\bdataframe\b")) or soup.find("table")
    if table is None:
        return 0
    thead = table.find("thead")
    tbody = table.find("tbody")
    if thead is None or tbody is None:
        return 0
    header_row = thead.find("tr")
    if header_row is None:
        return 0

    # Idempotence: remove a prior generated Audience Focus column before adding
    # the freshly classified values.
    headers = header_row.find_all(["th", "td"], recursive=False)
    focus_index = next(
        (
            i
            for i, cell in enumerate(headers)
            if clean_amc_title(cell.get_text(" ", strip=True)).casefold()
            in {"audience focus", "audience"}
        ),
        None,
    )
    if focus_index is not None:
        headers[focus_index].decompose()
        for row in tbody.find_all("tr", recursive=False):
            cells = row.find_all(["th", "td"], recursive=False)
            if 0 <= focus_index < len(cells):
                cells[focus_index].decompose()

    th = soup.new_tag("th")
    th.string = "Audience Focus"
    th["title"] = "Descriptive cultural/market context; does not change ratings"
    header_row.append(th)

    # Snapshot of previously verified language evidence. No extra network calls.
    previous_language_index = _prior_verified_language_index()

    # First pass: resolve identity/Wikidata and collect the already-selected
    # Wikipedia pages.  We batch the page-context request afterward so this
    # feature adds at most one Wikipedia enrichment request per ~40 rows rather
    # than one request per movie.
    records: list[dict] = []
    page_titles: list[str] = []
    for row in tbody.find_all("tr", recursive=False):
        title_el = row.select_one(".movie-cell-title")
        if title_el is None:
            continue
        display_title = clean_amc_title(title_el.get_text(" ", strip=True))
        if not display_title:
            continue

        context = _row_movie_context(row, display_title, reference_year)
        rt_metadata = _rt_metadata_for_context(_extract_rt_url(row), context)
        context = _context_with_rt_identity(context, rt_metadata)
        img = row.select_one("img.movie-poster")
        wiki_page = None
        if img is not None:
            value = img.get("data-wikipedia-page")
            if isinstance(value, str) and value.strip():
                wiki_page = clean_amc_title(value)

        imdb_id = _extract_imdb_id(row)
        entity = _audience_entity_for_context(
            context,
            imdb_id=imdb_id,
            wikipedia_page=wiki_page,
        )
        page_title = wiki_page or (_entity_enwiki_title(entity) if entity is not None else None)

        # Page intro/categories are most useful when the structured metadata has
        # not already identified a thematic audience focus. International-market
        # labels (Indian/Korean/etc.) remain valid and may coexist with a theme.
        initial = classify_audience_focus(
            context,
            entity,
            supplemental_languages=(rt_metadata.languages if rt_metadata else frozenset()),
            supplemental_genres=(rt_metadata.genres if rt_metadata else frozenset()),
        )
        thematic_tags = {
            "Faith-oriented", "LGBTQ+-focused", "Black-focused",
            "Latino/Hispanic-focused",
        }
        needs_page_context = bool(page_title and not (set(initial.tags) & thematic_tags))
        if needs_page_context:
            page_titles.append(page_title)

        records.append({
            "row": row,
            "context": context,
            "entity": entity,
            "page_title": page_title if needs_page_context else None,
            "initial": initial,
            "rt_metadata": rt_metadata,
        })

    focus_pages = _wikipedia_focus_pages(page_titles) if page_titles else {}

    count = 0
    for record in records:
        page_title = record["page_title"]
        page = focus_pages.get(page_title.casefold()) if page_title else None
        supplemental = _page_focus_text(page)
        result = (
            classify_audience_focus(
                record["context"],
                record["entity"],
                supplemental_focus_text=supplemental,
                supplemental_languages=(record["rt_metadata"].languages if record["rt_metadata"] else frozenset()),
                supplemental_genres=(record["rt_metadata"].genres if record["rt_metadata"] else frozenset()),
            )
            if supplemental
            else record["initial"]
        )

        result = _retain_previous_verified_language_focus(
            record["row"], record["context"], result, previous_language_index,
            entity=record["entity"], rt_metadata=record["rt_metadata"],
        )

        td = soup.new_tag("td", **{"class": "audience-focus-cell"})
        td["data-audience-focus"] = "|".join(result.tags)
        td["data-audience-reasons"] = " | ".join(result.reasons)
        td["title"] = " | ".join(result.reasons)
        for tag in result.tags:
            badge = soup.new_tag("span", **{"class": "audience-focus-tag"})
            badge.string = tag
            td.append(badge)
        record["row"].append(td)
        count += 1

    _ensure_audience_focus_style(soup)
    return count


def _entity_contradictions(entity: dict, context: MovieContext) -> list[str]:
    reasons: list[str] = []
    if not _entity_is_filmish(entity):
        reasons.append("not-film")
        return reasons

    if context.expected_year is not None:
        years = _entity_release_years(entity)
        if years and min(abs(y - context.expected_year) for y in years) > 3:
            reasons.append("year")

    if context.runtime_min is not None and not context.relax_runtime:
        runtimes = _entity_runtimes(entity)
        if runtimes and min(abs(r - context.runtime_min) for r in runtimes) > 20:
            reasons.append("runtime")

    if context.spoken_languages:
        languages = _entity_languages(entity)
        if languages and not (set(context.spoken_languages) & languages):
            reasons.append("language")

    return reasons


def _english_tiebreak_enabled(context: MovieContext) -> bool:
    """Use English only as a soft preference for genuinely ambiguous titles."""
    if context.spoken_languages:
        return False
    words = re.findall(r"\w+", _normalized_match_text(context.canonical_title), flags=re.UNICODE)
    return 0 < len(words) <= 2


def _entity_score(entity: dict, context: MovieContext, imdb_id: str | None) -> float:
    score = _entity_title_score(entity, context) * 100.0

    imdb_values = {v.casefold() for v in _claim_string_values(entity, "P345")}
    if imdb_id and imdb_id.casefold() in imdb_values:
        score += 25.0

    if context.expected_year is not None:
        years = _entity_release_years(entity)
        if years:
            diff = min(abs(y - context.expected_year) for y in years)
            if diff <= 3:
                score += max(0.0, 15.0 - 4.0 * diff)

    if context.runtime_min is not None and not context.relax_runtime:
        runtimes = _entity_runtimes(entity)
        if runtimes:
            diff = min(abs(r - context.runtime_min) for r in runtimes)
            if diff <= 20:
                score += max(0.0, 10.0 - diff / 2.0)

    if context.spoken_languages:
        languages = _entity_languages(entity)
        if languages and set(context.spoken_languages) & languages:
            score += 10.0
    elif _english_tiebreak_enabled(context):
        # Soft preference only. Four points is enough to break an otherwise
        # ambiguous same-title tie, but weaker than strong runtime/year evidence.
        languages = _entity_languages(entity)
        if "english" in languages:
            score += 4.0

    return score


def _entity_enwiki_title(entity: dict) -> str | None:
    title = ((entity.get("sitelinks") or {}).get("enwiki") or {}).get("title")
    return clean_amc_title(title) if isinstance(title, str) and title.strip() else None


def _commons_url_from_entity(entity: dict) -> str | None:
    filenames = _claim_string_values(entity, "P3383") or _claim_string_values(entity, "P18")
    if not filenames:
        return None
    filename = filenames[0].strip()
    if not filename:
        return None
    return COMMONS_FILE_REDIRECT + quote(filename.replace(" ", "_"), safe="()_',.-") + f"?width={WIKIMEDIA_POSTER_WIDTH}"


def _image_from_validated_entity(entity: dict) -> tuple[str | None, str | None, str | None]:
    enwiki_title = _entity_enwiki_title(entity)
    if enwiki_title:
        pages = _wikipedia_pages_for_titles([enwiki_title])
        for page in pages:
            if page.get("missing") is not None or _page_is_disambiguation(page):
                continue
            page_title = clean_amc_title(str(page.get("title", enwiki_title)))
            image = _image_from_page(page)
            if image:
                return image, page_title, "wikidata-enwiki"
            image = _image_from_validated_wikipedia_infobox(page_title)
            if image:
                return image, page_title, "wikidata-enwiki-infobox"
    commons = _commons_url_from_entity(entity)
    if commons:
        return commons, None, "wikidata-commons"
    return None, None, None


def _best_entity_for_context(
    entities: list[dict],
    context: MovieContext,
    imdb_id: str | None,
) -> tuple[dict | None, list[str]]:
    ranked: list[tuple[float, int, dict]] = []
    rejected: list[str] = []
    for index, entity in enumerate(entities):
        if _entity_title_score(entity, context) < 0.82:
            continue
        reasons = _entity_contradictions(entity, context)
        if reasons:
            rejected.append(f"{entity.get('id', '?')}:{','.join(reasons)}")
            continue
        ranked.append((_entity_score(entity, context, imdb_id), -index, entity))
    if not ranked:
        return None, rejected
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return ranked[0][2], rejected


def _wikidata_entity_for_exact_imdb(
    context: MovieContext,
    imdb_id: str | None,
) -> dict | None:
    if not imdb_id:
        return None
    entities = [
        entity
        for entity in _wikidata_entities(_wikidata_qids_for_imdb(imdb_id))
        if _entity_has_imdb_id(entity, imdb_id)
    ]
    entity, _ = _best_entity_for_context(entities, context, imdb_id)
    return entity


def _set_rt_cell(
    soup: BeautifulSoup,
    row,
    url: str,
    audience: int | None,
    critic: int | None,
) -> None:
    """Write one RT identity/score cell using the report's critic/audience order."""
    cells = row.find_all(["th", "td"], recursive=False)
    if len(cells) < 3:
        return
    cell = cells[2]
    cell["data-source-url"] = url
    cell.clear()
    if audience is None and critic is None:
        return
    link = soup.new_tag("a", href=url)
    link["rel"] = "noopener noreferrer"
    link["target"] = "_blank"
    link.string = f"{critic if critic is not None else '-'}/{audience if audience is not None else '-'}"
    cell.append(link)


def _repair_rt_identity_from_wikidata(
    soup: BeautifulSoup,
    row,
    context: MovieContext,
) -> tuple[MovieContext, RtPageMetadata | None, bool]:
    """Repair a missing/contradictory RT identity through exact IMDb→Wikidata.

    This path is deliberately narrow: it runs only when the current RT URL is
    missing for a year-anchored presentation, or when the existing RT page is
    incompatible with AMC title/runtime evidence.  Wikidata's stable P1258
    identifier then supplies the exact RT movie URL.  No title-specific aliases
    or provider IDs are embedded in production code.
    """
    current_url = _extract_rt_url(row)
    current_meta = _rt_metadata_for_context(current_url, context) if current_url else None
    needs_bridge = (
        (current_url is not None and current_meta is None)
        or (current_url is None and context.expected_year is not None)
    )
    if not needs_bridge:
        anchored = _context_with_rt_identity(context, current_meta)
        return anchored, current_meta, False

    imdb_id = _extract_imdb_id(row)
    entity = _wikidata_entity_for_exact_imdb(context, imdb_id)
    if entity is None:
        anchored = _context_with_rt_identity(context, current_meta)
        return anchored, current_meta, False
    candidate_url = _entity_rt_movie_url(entity)
    if not candidate_url:
        anchored = _context_with_rt_identity(context, current_meta)
        return anchored, current_meta, False
    candidate_meta = _rt_metadata_for_context(candidate_url, context)
    if candidate_meta is None:
        anchored = _context_with_rt_identity(context, current_meta)
        return anchored, current_meta, False

    scores = _rt_scores_for_context(candidate_url, context) or (None, None)
    _set_rt_cell(soup, row, candidate_url, scores[0], scores[1])
    anchored = _context_with_rt_identity(context, candidate_meta)
    return anchored, candidate_meta, True


def _image_via_wikidata(
    context: MovieContext,
    imdb_id: str | None,
) -> tuple[str | None, str | None, str | None, list[str]]:
    """Resolve a poster through Wikidata, preferring a verified IMDb bridge.

    Title fallback is progressive rather than broad: try the canonical title
    first and consult at most a couple of generic metadata variants only if the
    earlier query cannot produce a validated image.  This improves recall for
    presentation/anniversary titles without multiplying network work for the
    normal case.
    """
    rejected: list[str] = []

    if imdb_id:
        imdb_entities = [
            entity
            for entity in _wikidata_entities(_wikidata_qids_for_imdb(imdb_id))
            if _entity_has_imdb_id(entity, imdb_id)
        ]
        entity, imdb_rejected = _best_entity_for_context(
            imdb_entities, context, imdb_id
        )
        rejected.extend(imdb_rejected)
        if entity is not None:
            image, page, source = _image_from_validated_entity(entity)
            if image:
                return image, page, source, rejected

    seen_qids: set[str] = set()
    for title in _context_title_variants(context)[:WIKIDATA_TITLE_VARIANT_LIMIT]:
        qids = [
            qid for qid in _wikidata_qids_for_title(title, limit=10)
            if qid not in seen_qids
        ]
        seen_qids.update(qids)
        if not qids:
            continue
        title_entities = _wikidata_entities(qids)
        entity, title_rejected = _best_entity_for_context(
            title_entities, context, None
        )
        rejected.extend(title_rejected)
        if entity is None:
            continue
        image, page, source = _image_from_validated_entity(entity)
        if image:
            return image, page, source, rejected

    return None, None, None, rejected

def _page_evaluation(
    page: dict,
    context: MovieContext,
) -> tuple[bool, list[str], float]:
    """Validate and rank one Wikipedia page with at most one Wikidata fetch."""
    if page.get("missing") is not None or _page_is_disambiguation(page):
        return False, ["missing-or-disambiguation"], 0.0
    page_title = clean_amc_title(str(page.get("title", "")))
    identity = _candidate_identity_score(page_title, context.canonical_title)
    if identity < 0.82:
        return False, ["title"], identity * 100.0
    qid = _page_qid(page)
    if not qid:
        # No structured metadata means no contradiction is known. This is a
        # permissive fallback, exactly as intended by the poster policy.
        return True, [], identity * 100.0
    entities = _wikidata_entities([qid])
    if not entities:
        return True, [], identity * 100.0
    entity = entities[0]
    reasons = _entity_contradictions(entity, context)
    return not reasons, reasons, _entity_score(entity, context, None)


def _page_context_ok(page: dict, context: MovieContext) -> tuple[bool, list[str]]:
    ok, reasons, _ = _page_evaluation(page, context)
    return ok, reasons


def _page_rank_score(page: dict, context: MovieContext) -> float:
    """Rank a validated Wikipedia page using the same soft metadata signals."""
    _, _, score = _page_evaluation(page, context)
    return score


def _image_via_wikipedia(
    context: MovieContext,
    reference_year: int,
) -> tuple[str | None, str | None, str | None, list[str]]:
    rejected: list[str] = []
    candidates: list[tuple[float, int, int, str, str, str]] = []
    seen_pages: set[object] = set()
    order = 0

    def consider(page: dict, source: str, source_priority: int) -> None:
        nonlocal order
        page_id = page.get("pageid") or page.get("title")
        if page_id in seen_pages:
            return
        seen_pages.add(page_id)
        ok, reasons, rank_score = _page_evaluation(page, context)
        if not ok:
            rejected.append(f"{page.get('title', '?')}:{','.join(reasons)}")
            return
        title = clean_amc_title(str(page.get("title", "")))
        image = _image_from_page(page)
        resolved_source = source
        if not image and title:
            # The validated article can visibly contain an infobox poster even
            # when PageImages has no selected page image.  Fall back to the
            # infobox's explicit image filename, never arbitrary body images.
            image = _image_from_validated_wikipedia_infobox(title)
            if image:
                resolved_source = f"{source}-infobox"
        if not image:
            return
        candidates.append((
            rank_score,
            source_priority,
            -order,
            image,
            title,
            resolved_source,
        ))
        order += 1

    # Exact/redirected page-title candidates remain the cheapest first pass.
    exact_pages = _wikipedia_pages_for_titles(
        wikipedia_title_candidates(context.display_title, reference_year=reference_year)
    )
    for page in exact_pages:
        consider(page, "wikipedia-exact", 1)

    # A short title with no AMC language tag is genuinely ambiguous. In that
    # case, do one English-oriented search before accepting an otherwise valid
    # foreign-language exact redirect. This is a ranking preference, not a
    # veto: if no good English candidate exists, the foreign candidate remains.
    if _english_tiebreak_enabled(context):
        query = f'"{context.canonical_title}" English-language film'
        for page in _wikipedia_search_pages(query):
            consider(page, "wikipedia-search", 0)

    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        _, _, _, image, title, source = candidates[0]
        return image, title, source, rejected

    # Normal fallback search. For non-ambiguous titles this preserves the old
    # behavior; for ambiguous titles it runs only when exact + English-first
    # candidates produced no usable image.
    terms = search_terms_for_title(context.canonical_title)
    queries = [
        f'"{context.canonical_title}" film',
        f'intitle:"{context.canonical_title}" film',
        f"{terms} film",
    ]
    for query in queries:
        for page in _wikipedia_search_pages(query):
            consider(page, "wikipedia-search", 0)

    if not candidates:
        return None, None, None, rejected

    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    _, _, _, image, title, source = candidates[0]
    return image, title, source, rejected


def _previous_poster_index() -> dict[str, list[dict[str, str | int | None]]]:
    """Index non-placeholder posters from the immediately previous report.

    The workflow snapshots the committed report before nbconvert overwrites it.
    Reuse is identity-gated later; this cache is only a resilience fallback for
    temporary provider failures, never a title-only override.
    """
    global _PREVIOUS_POSTER_INDEX
    if _PREVIOUS_POSTER_INDEX is not None:
        return _PREVIOUS_POSTER_INDEX
    index: dict[str, list[dict[str, str | int | None]]] = {}
    if not PREVIOUS_REPORT_PATH.exists():
        _PREVIOUS_POSTER_INDEX = index
        return index
    try:
        previous = BeautifulSoup(PREVIOUS_REPORT_PATH.read_text(encoding="utf-8"), "html.parser")
    except Exception:
        _PREVIOUS_POSTER_INDEX = index
        return index
    for row in previous.select("tr[data-movie-id]"):
        title_el = row.select_one(".movie-cell-title")
        img = row.select_one("img.movie-poster")
        if title_el is None or img is None:
            continue
        title = clean_amc_title(title_el.get_text(" ", strip=True))
        src = str(img.get("src", "")).strip()
        if not title or _is_placeholder(src) or not src.startswith(("https://", "http://")):
            continue
        source = str(img.get("data-poster-source", "")).strip()
        if source == "rejected-contradiction":
            continue
        runtime = None
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) >= 6:
            runtime = _runtime_minutes(cells[5].get_text(" ", strip=True))
        entry = {
            "title": title,
            "src": src,
            "source": source,
            "wiki_page": str(img.get("data-wikipedia-page", "")).strip() or None,
            "rt_url": _extract_rt_url(row),
            "imdb_id": _extract_imdb_id(row),
            "runtime_min": runtime,
        }
        for key_title in {title, clean_amc_title(analyze_movie_title(title).canonical_title)}:
            key = _normalized_match_text(key_title)
            if key:
                index.setdefault(key, []).append(entry)
    _PREVIOUS_POSTER_INDEX = index
    return index


def _previous_poster_for_row(row, context: MovieContext) -> dict[str, str | int | None] | None:
    """Return a prior poster only when movie identity is still the same.

    Exact RT URL or IMDb ID is the preferred anchor.  Without one, require an
    exact normalized title plus a near-identical AMC runtime and a structured
    Wikimedia/Letterboxd source.  This prevents a famous older same-title film
    from being resurrected for a remake merely because a current lookup failed.
    """
    index = _previous_poster_index()
    candidates: list[dict[str, str | int | None]] = []
    seen = set()
    for title in _context_title_variants(context):
        key = _normalized_match_text(title)
        for item in index.get(key, []):
            marker = (item.get("src"), item.get("rt_url"), item.get("imdb_id"))
            if marker not in seen:
                seen.add(marker)
                candidates.append(item)
    current_rt = _extract_rt_url(row)
    current_imdb = _extract_imdb_id(row)
    for item in candidates:
        prev_rt = str(item.get("rt_url") or "")
        prev_imdb = str(item.get("imdb_id") or "")
        if current_rt and prev_rt and current_rt.rstrip("/").casefold() == prev_rt.rstrip("/").casefold():
            return item
        if current_imdb and prev_imdb and current_imdb.casefold() == prev_imdb.casefold():
            return item
    # If the current report has a provider identity and none of the prior rows
    # agree with it, that disagreement is evidence against continuity.
    if current_rt or current_imdb:
        return None
    # No strong provider anchor: allow only a very conservative continuity case.
    for item in candidates:
        source = str(item.get("source") or "").casefold()
        if not (source.startswith("wikipedia") or source.startswith("wikidata") or source == "letterboxd-tmdb"):
            continue
        previous_runtime = item.get("runtime_min")
        if context.runtime_min is None or not isinstance(previous_runtime, int):
            continue
        if abs(previous_runtime - context.runtime_min) <= 3:
            return item
    return None


def resolve_poster(
    context: MovieContext,
    *,
    imdb_id: str | None = None,
    reference_year: int | None = None,
) -> tuple[str | None, str | None, str | None, list[str]]:
    ref_year = reference_year or datetime.now(timezone.utc).year

    image, page, source, rejected = _image_via_wikidata(context, imdb_id)
    if image:
        return image, page, source, rejected

    image, page, source, rejected2 = _image_via_wikipedia(context, ref_year)
    return image, page, source, rejected + rejected2


def _is_placeholder(src: str | None) -> bool:
    if not src:
        return True
    low = src.casefold()
    return (
        PLACEHOLDER_TOKEN in low
        or "rt-poster-default" in low
        or "poster-default" in low
        or "default-poster" in low
        or "empty-poster" in low
        or "image-not-found" in low
        or "no-poster" in low
        or "no_poster" in low
    )


def _needs_validation(img, context: MovieContext) -> bool:
    src = str(img.get("src", ""))
    if _is_placeholder(src):
        return True
    if img.get("data-wikipedia-page") or img.get("data-poster-source"):
        return True
    if context.expected_year is not None or context.spoken_languages:
        return True
    # Short/generic titles are disproportionately ambiguous (e.g. a word can
    # name a movie, person, sport, object, song, etc.).  Re-resolve those while
    # leaving long, already-working titles alone to minimize Wikimedia calls.
    words = re.findall(r"\w+", context.canonical_title, flags=re.UNICODE)
    return len(words) <= 2


def _existing_known_contradiction(img, context: MovieContext) -> list[str]:
    page_title = img.get("data-wikipedia-page")
    if not isinstance(page_title, str) or not page_title.strip():
        return []
    pages = _wikipedia_pages_for_titles([page_title])
    for page in pages:
        ok, reasons = _page_context_ok(page, context)
        if not ok:
            return reasons
        return []
    return []


def _imdb_rating_values(wanted_ids: set[str]) -> dict[str, float]:
    """Read ratings for a small set of IMDb IDs from the dataset already fetched.

    Papermill downloads the official IMDb ``title.ratings.tsv.gz`` once per run.
    Reusing it here avoids another broad network lookup and can fill ratings for
    identities discovered later via Wikidata/Letterboxd.  No score is invented:
    IDs absent from the official dataset simply remain blank.
    """
    wanted = {x.lower() for x in wanted_ids if re.fullmatch(r"tt\d{5,12}", x or "", re.I)}
    if not wanted or not IMDB_RATINGS_GZ.exists():
        return {}
    out: dict[str, float] = {}
    try:
        with gzip.open(IMDB_RATINGS_GZ, "rt", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            for row in reader:
                tconst = str(row.get("tconst", "")).lower()
                if tconst not in wanted:
                    continue
                try:
                    value = float(row.get("averageRating", ""))
                except (TypeError, ValueError):
                    continue
                if 0.0 <= value <= 10.0:
                    out[tconst] = value
                    if len(out) == len(wanted):
                        break
    except (OSError, EOFError, gzip.BadGzipFile):
        return {}
    return out


def _imdb_live_rating(imdb_id: str) -> float | None:
    """Read a current rating from an exact IMDb title ID, best-effort only."""
    if not re.fullmatch(r"tt\d{5,12}", imdb_id or "", re.I):
        return None
    html_text = _fetch_text(
        f"https://www.imdb.com/title/{imdb_id.lower()}/", timeout=5, attempts=1
    )
    if not html_text:
        return None
    soup = BeautifulSoup(html_text, "html.parser")
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(" ", strip=True)
        try:
            data = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        objects = data if isinstance(data, list) else [data]
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            aggregate = obj.get("aggregateRating")
            if not isinstance(aggregate, dict):
                continue
            try:
                value = float(aggregate.get("ratingValue"))
            except (TypeError, ValueError):
                continue
            if 0.0 <= value <= 10.0:
                return value
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    match = re.search(r"\bIMDb\s+RATING\s+(\d(?:\.\d)?)/10\b", text, re.I)
    if match:
        value = float(match.group(1))
        if 0.0 <= value <= 10.0:
            return value
    return None


def _imdb_poster_url(imdb_id: str | None) -> str | None:
    """Return exact-ID IMDb key art as a bounded identity-safe fallback."""
    if not re.fullmatch(r"tt\d{5,12}", imdb_id or "", re.I):
        return None
    url = f"https://www.imdb.com/title/{str(imdb_id).lower()}/"
    html_text = _fetch_text(url, timeout=5, attempts=1)
    if not html_text:
        return None
    soup = BeautifulSoup(html_text, "html.parser")

    candidates: list[str] = []
    for attrs in ({"property": "og:image"}, {"name": "twitter:image"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            candidates.append(str(meta.get("content")))
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(" ", strip=True)
        try:
            data = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        objects = data if isinstance(data, list) else [data]
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            image = obj.get("image")
            if isinstance(image, str):
                candidates.append(image)
            elif isinstance(image, dict) and isinstance(image.get("url"), str):
                candidates.append(str(image.get("url")))

    for value in candidates:
        value = value.strip()
        low = value.casefold()
        if not value.startswith(("https://", "http://")):
            continue
        if any(token in low for token in ("placeholder", "nopicture", "no-image")):
            continue
        if any(host in low for host in ("m.media-amazon.com", "media-amazon.com", "imdb-media")):
            return value
    return None


def _imdb_id_for_context(
    row,
    context: MovieContext,
    *,
    wikipedia_page: str | None,
    reference_year: int,
) -> str | None:
    """Resolve an IMDb ID from already-validated generic metadata identities."""
    existing = _extract_imdb_id(row)
    if existing:
        return existing.lower()

    entity = _audience_entity_for_context(
        context, imdb_id=None, wikipedia_page=wikipedia_page
    )
    if entity is not None:
        values = [
            value.lower()
            for value in _claim_string_values(entity, "P345")
            if re.fullmatch(r"tt\d{5,12}", value or "", re.I)
        ]
        if values:
            return values[0]

    # Sparse/new event titles may not yet have a useful Wikidata item.  The
    # same independently validated Letterboxd/TMDB identity used for poster
    # fallback can expose its linked IMDb ID without guessing a title alias.
    meta, _ = _best_letterboxd_metadata(context, reference_year)
    if meta is not None and meta.imdb_id:
        return meta.imdb_id.lower()
    return None


def _fill_missing_imdb_scores(soup: BeautifulSoup, reference_year: int) -> int:
    """Backfill missing IMDb URLs/ratings without replacing existing values."""
    table = soup.find("table", class_=re.compile(r"\bdataframe\b")) or soup.find("table")
    if table is None:
        return 0
    header_row = table.find("thead")
    header_row = header_row.find("tr") if header_row else None
    tbody = table.find("tbody")
    if header_row is None or tbody is None:
        return 0
    headers = header_row.find_all(["th", "td"], recursive=False)
    imdb_idx = next(
        (i for i, cell in enumerate(headers) if clean_amc_title(cell.get_text(" ", strip=True)).casefold() == "imdb"),
        None,
    )
    if imdb_idx is None:
        return 0

    pending: list[tuple[object, str]] = []
    for row in tbody.find_all("tr", recursive=False):
        cells = row.find_all(["th", "td"], recursive=False)
        if imdb_idx >= len(cells):
            continue
        cell = cells[imdb_idx]
        if clean_amc_title(cell.get_text(" ", strip=True)):
            continue
        title_el = row.select_one(".movie-cell-title")
        if title_el is None:
            continue
        display_title = clean_amc_title(title_el.get_text(" ", strip=True))
        if not display_title:
            continue
        context = _row_movie_context(row, display_title, reference_year)
        rt_metadata = _rt_metadata_for_context(_extract_rt_url(row), context)
        context = _context_with_rt_identity(context, rt_metadata)
        img = row.select_one("img.movie-poster")
        wiki_page = str(img.get("data-wikipedia-page", "")).strip() if img is not None else ""
        imdb_id = _imdb_id_for_context(
            row,
            context,
            wikipedia_page=wiki_page or None,
            reference_year=reference_year,
        )
        if imdb_id:
            pending.append((cell, imdb_id))

    ratings = _imdb_rating_values({imdb_id for _, imdb_id in pending})
    # IMDb's downloadable ratings file is official but daily, so a brand-new
    # rating can appear on the exact title page before the next dataset refresh.
    # Only exact IDs that are still absent get this short, non-fatal live check.
    for imdb_id in dict.fromkeys(imdb_id for _, imdb_id in pending):
        if imdb_id in ratings:
            continue
        value = _imdb_live_rating(imdb_id)
        if value is not None:
            ratings[imdb_id] = value

    changed = 0
    for cell, imdb_id in pending:
        url = f"https://www.imdb.com/title/{imdb_id}/"
        cell["data-source-url"] = url
        rating = ratings.get(imdb_id)
        if rating is None:
            continue
        cell.clear()
        link = soup.new_tag("a", href=url)
        link["rel"] = "noopener noreferrer"
        link["target"] = "_blank"
        link.string = f"{rating:.1f}"
        cell.append(link)
        changed += 1
    return changed


def main() -> int:
    if not HTML_PATH.exists():
        raise RuntimeError(f"{HTML_PATH} does not exist.")

    soup = BeautifulSoup(HTML_PATH.read_text(encoding="utf-8"), "html.parser")
    current_year = datetime.now(timezone.utc).year
    updated = 0
    preserved = 0
    unresolved: list[str] = []

    # Resolve missing IMDb identities/ratings before poster work so exact IMDb
    # IDs can also serve as safe Wikidata and poster identity bridges.  This is
    # the same bounded enrichment pass that previously ran at the end.
    imdb_enriched = _fill_missing_imdb_scores(soup, current_year)

    for row in soup.select("tr[data-movie-id]"):
        title_el = row.select_one(".movie-cell-title")
        if title_el is None:
            continue
        display_title = clean_amc_title(title_el.get_text(" ", strip=True))
        if not display_title:
            continue
        if is_non_movie_title(display_title):
            raise RuntimeError(
                "Non-movie inventory survived notebook filtering: "
                f"{display_title!r}"
            )

        context = _row_movie_context(row, display_title, current_year)
        context, rt_metadata, _ = _repair_rt_identity_from_wikidata(
            soup, row, context
        )
        canonical = context.canonical_title

        poster_link = row.select_one(".movie-poster-wrap a")
        if poster_link is not None:
            poster_link["href"] = (
                "https://www.youtube.com/results?search_query="
                + quote(f"{canonical} official trailer")
            )

        img = row.select_one("img.movie-poster")
        if img is None:
            continue
        img["data-lookup-title"] = canonical

        if not _needs_validation(img, context):
            preserved += 1
            continue

        old_src = str(img.get("src", ""))
        imdb_id = _extract_imdb_id(row)

        # Prefer high-quality Wikimedia identity resolution.  A validated RT
        # page contributes year/runtime/language identity above, but its CDN
        # thumbnail must not automatically replace an already-good poster.
        # This restores the pre-v32 quality policy while retaining remake-safe
        # identity constraints (for example, a current film cannot resolve to
        # an older same-title Wikipedia page).
        image = None
        wiki_page = None
        source = None
        previous_poster = None
        rejected: list[str] = []
        image, wiki_page, source, rejected = resolve_poster(
            context,
            imdb_id=imdb_id,
            reference_year=current_year,
        )

        # Very new/limited/event titles are frequently absent from Wikimedia.
        # Prefer Letterboxd/TMDB artwork next because it normally exposes the
        # theatrical poster at useful resolution.  The page must still pass
        # generic title/year/runtime identity checks.
        if not image:
            image, letterboxd_url, rejected_lb = _image_via_letterboxd(
                context, current_year
            )
            rejected.extend(rejected_lb)
            if image:
                source = "letterboxd-tmdb"
                img["data-letterboxd-page"] = letterboxd_url or ""

        # Exact IMDb IDs are another identity-safe, bounded poster fallback.
        # This is especially useful for very new event titles whose Wikimedia
        # and Letterboxd pages exist but do not yet expose usable key art.
        if not image and imdb_id:
            image = _imdb_poster_url(imdb_id)
            if image:
                source = "imdb-exact"

        # Provider availability can change from run to run.  If fresh
        # resolution failed, recover the immediately previous non-placeholder
        # poster only when the current row still has the same validated movie
        # identity.  This prevents transient outages from turning a good image
        # into a placeholder while remaining remake-safe.
        if not image:
            previous_poster = _previous_poster_for_row(row, context)
            if previous_poster is not None:
                image = str(previous_poster.get("src") or "") or None
                source = str(previous_poster.get("source") or "previous-report")
                wiki_page = str(previous_poster.get("wiki_page") or "") or None
                if image:
                    img["data-poster-reused"] = "previous-report"

        # If the generated report already contains a validated Wikimedia or
        # Letterboxd/TMDB poster, do not downgrade it to an RT thumbnail merely
        # because a later lookup was temporarily inconclusive.  A known
        # contradiction still wins and is handled below.
        existing_source = str(img.get("data-poster-source", "")).casefold()
        if (
            not image
            and not _is_placeholder(old_src)
            and (
                existing_source.startswith("wikipedia")
                or existing_source.startswith("wikidata")
                or existing_source == "letterboxd-tmdb"
            )
        ):
            contradictions = _existing_known_contradiction(img, context)
            if not contradictions:
                preserved += 1
                continue

        # RT is a last poster fallback.  Its exact movie page is valuable for
        # identity/scores/language, but poster art can be a low-resolution or
        # generic default asset.  _rt_poster_url() filters known defaults.
        if not image and rt_metadata is not None and rt_metadata.poster_url:
            image = _usable_rt_poster_url(rt_metadata.poster_url)
            if image:
                source = "rottentomatoes-exact"

        if image:
            if image != old_src:
                updated += 1
            img["src"] = image
            img["data-poster-source"] = source or "wikimedia"
            if previous_poster is None and img.has_attr("data-poster-reused"):
                del img["data-poster-reused"]
            if source != "letterboxd-tmdb" and img.has_attr("data-letterboxd-page"):
                del img["data-letterboxd-page"]
            if wiki_page:
                img["data-wikipedia-page"] = wiki_page
            elif img.has_attr("data-wikipedia-page"):
                del img["data-wikipedia-page"]
            if rejected:
                img["data-rejected-candidates"] = " | ".join(rejected[:4])
            continue

        # Default is preservation.  Only remove a known existing Wikipedia
        # poster when structured metadata gives a strong contradiction.
        contradictions = _existing_known_contradiction(img, context)
        if contradictions:
            img["src"] = PLACEHOLDER_URL
            img["data-poster-source"] = "rejected-contradiction"
            img["data-rejection-reason"] = ",".join(contradictions)
            if img.has_attr("data-wikipedia-page"):
                del img["data-wikipedia-page"]
            updated += 1
            unresolved.append(display_title)
            print(
                f"[POSTER] Rejected contradictory poster for {display_title!r}: "
                + ", ".join(contradictions)
            )
        elif _is_placeholder(old_src):
            unresolved.append(display_title)
            print(
                f"[POSTER] No usable poster from validated metadata sources: "
                f"{display_title!r}"
            )
        else:
            # No decisive evidence either way: keep the working image.
            preserved += 1

    audience_rows = _add_audience_focus_column(soup, current_year)

    HTML_PATH.write_text(str(soup), encoding="utf-8")
    print(
        "[OK] Poster validation complete: "
        f"{updated} changed, {preserved} preserved, {len(unresolved)} unresolved; "
        f"IMDb backfilled {imdb_enriched} rating(s); "
        f"Audience Focus classified {audience_rows} row(s)."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
