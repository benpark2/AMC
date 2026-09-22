#!/usr/bin/env python3
"""
Validate and improve movie posters after postprocess_report.py.

The resolver uses Wikimedia APIs only.  It does not scrape IMDb, Rotten
Tomatoes, Fandango, or another poster site.  IMDb IDs already present in the
report are used only as optional identifiers when querying Wikidata.

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
``General`` rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
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
    "AMC-weekend-movie-report/6.0 "
    "(https://github.com/benpark2/AMC; Wikimedia metadata lookup)"
)
PLACEHOLDER_TOKEN = "images-not-found"
PLACEHOLDER_URL = (
    "https://4ddig.tenorshare.com/images/photo-recovery/images-not-found.webp"
)
IMDB_ID_RE = re.compile(r"/title/(tt\d{5,12})(?:/|$)", re.IGNORECASE)
QID_RE = re.compile(r"^Q\d+$")
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
MAX_API_ATTEMPTS = 3
_JSON_CACHE: dict[str, dict | None] = {}
_LABEL_CACHE: dict[str, str] = {}

# Audience-focus labels are intentionally descriptive and evidence based.  The
# first four describe an explicit thematic/marketing focus.  The remaining
# labels describe language/market context for international films.
AUDIENCE_TAG_ORDER = (
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
)
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


def _fetch_json(url: str, timeout: int = 20) -> dict | None:
    if url in _JSON_CACHE:
        return _JSON_CACHE[url]

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

    _JSON_CACHE[url] = None
    return None


def _api_json(base: str, **params: object) -> dict | None:
    return _fetch_json(_api_url(base, params))


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
            pithumbsize=500,
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
        pithumbsize=500,
        pilicense="any",
    )
    pages = _pages_from_query(data)
    return sorted(
        pages,
        key=lambda p: p.get("index") if isinstance(p.get("index"), int) else 999999,
    )


def _extract_imdb_id(row) -> str | None:
    link = row.select_one('a[href*="imdb.com/title/"]')
    if link is None:
        return None
    match = IMDB_ID_RE.search(str(link.get("href", "")))
    return match.group(1).lower() if match else None


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
        label = clean_amc_title(str(hit.get("label", "")))
        if QID_RE.fullmatch(qid) and _title_similarity(label, title) >= 0.82:
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


def _entity_title_score(entity: dict, context: MovieContext) -> float:
    return max(
        (_title_similarity(name, context.canonical_title) for name in _entity_english_names(entity)),
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

    if entity is not None:
        entity_languages = _entity_languages(entity)
        if entity_languages:
            _add_language_focus(
                tags,
                reasons,
                entity_languages,
                source_label="Wikidata original language",
            )

        countries = _entity_claim_labels(entity, ("P495",))
        for tag, known_countries in COUNTRY_FOCUS_GROUPS.items():
            matched = sorted(countries & known_countries)
            if matched:
                tags.add(tag)
                reasons.append(f"{tag}: country of origin {', '.join(matched)}")

        focus_text = _explicit_focus_text(entity)
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
        imdb_entities = _wikidata_entities(_wikidata_qids_for_imdb(imdb_id))
        entity, _ = _best_entity_for_context(imdb_entities, context, imdb_id)
        if entity is not None:
            return entity

    title_entities = _wikidata_entities(
        _wikidata_qids_for_title(context.canonical_title)
    )
    entity, _ = _best_entity_for_context(title_entities, context, None)
    return entity


def _ensure_audience_focus_style(soup: BeautifulSoup) -> None:
    old = soup.find(id="audience-focus-style")
    if old:
        old.decompose()
    if soup.head is None:
        return
    style = soup.new_tag("style", id="audience-focus-style")
    style.string = AUDIENCE_STYLE
    soup.head.append(style)


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
        initial = classify_audience_focus(context, entity)
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
            )
            if supplemental
            else record["initial"]
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
    return COMMONS_FILE_REDIRECT + quote(filename.replace(" ", "_"), safe="()_',.-") + "?width=500"


def _image_from_validated_entity(entity: dict) -> tuple[str | None, str | None, str | None]:
    enwiki_title = _entity_enwiki_title(entity)
    if enwiki_title:
        pages = _wikipedia_pages_for_titles([enwiki_title])
        for page in pages:
            if page.get("missing") is not None or _page_is_disambiguation(page):
                continue
            image = _image_from_page(page)
            if image:
                return image, clean_amc_title(str(page.get("title", enwiki_title))), "wikidata-enwiki"
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


def _image_via_wikidata(
    context: MovieContext,
    imdb_id: str | None,
) -> tuple[str | None, str | None, str | None, list[str]]:
    qids: list[str] = []
    seen: set[str] = set()
    for qid in (
        _wikidata_qids_for_imdb(imdb_id) if imdb_id else []
    ) + _wikidata_qids_for_title(context.canonical_title):
        if qid not in seen:
            seen.add(qid)
            qids.append(qid)

    entities = _wikidata_entities(qids)
    entity, rejected = _best_entity_for_context(entities, context, imdb_id)
    if entity is None:
        return None, None, None, rejected
    image, page, source = _image_from_validated_entity(entity)
    return image, page, source, rejected


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
        image = _image_from_page(page)
        if not image:
            return
        title = clean_amc_title(str(page.get("title", "")))
        candidates.append((
            rank_score,
            source_priority,
            -order,
            image,
            title,
            source,
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
    return not src or PLACEHOLDER_TOKEN in src.casefold()


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


def main() -> int:
    if not HTML_PATH.exists():
        raise RuntimeError(f"{HTML_PATH} does not exist.")

    soup = BeautifulSoup(HTML_PATH.read_text(encoding="utf-8"), "html.parser")
    current_year = datetime.now(timezone.utc).year
    updated = 0
    preserved = 0
    unresolved: list[str] = []

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
        image, wiki_page, source, rejected = resolve_poster(
            context,
            imdb_id=imdb_id,
            reference_year=current_year,
        )

        if image:
            if image != old_src:
                updated += 1
            img["src"] = image
            img["data-poster-source"] = source or "wikimedia"
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
            print(f"[POSTER] No usable Wikimedia image: {display_title!r}")
        else:
            # No decisive evidence either way: keep the working image.
            preserved += 1

    audience_rows = _add_audience_focus_column(soup, current_year)

    HTML_PATH.write_text(str(soup), encoding="utf-8")
    print(
        "[OK] Poster validation complete: "
        f"{updated} changed, {preserved} preserved, {len(unresolved)} unresolved; "
        f"Audience Focus classified {audience_rows} row(s)."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
