#!/usr/bin/env python3
"""
Resolve missing movie posters after postprocess_report.py.

This file deliberately uses Wikimedia infrastructure only:
  * English Wikipedia's documented MediaWiki Action API
  * Wikidata's documented APIs
  * Wikimedia Commons for a Wikidata P18 image, when one exists

It does NOT scrape IMDb, Rotten Tomatoes, Fandango, or another poster site.
IMDb IDs already present in the generated report are used only as identifiers
to find the corresponding Wikidata item.

There are intentionally NO movie-specific mappings or poster URLs in this file.

Why this exists
---------------
postprocess_report.py already resolves many posters through Wikipedia. This
second pass only revisits rows that still contain the known missing-image
placeholder.

The prior implementation made many sequential REST-summary calls. That was
fragile for:
  * Wikipedia title capitalization and redirects
  * alternate regional movie titles
  * ambiguous short titles
  * rate limiting after many exact-title attempts

This implementation instead prefers the MediaWiki Action API's `pageimages`
property, which returns page identity + poster/lead image in one response and
can resolve redirects. It also uses Wikidata as a *disambiguation layer*.

Lookup order for a missing poster
---------------------------------
1. If the row already contains an IMDb ID, ask Wikidata which item has that
   exact P345 identifier. If it has an English-Wikipedia sitelink, fetch that
   page's image. If it has a Commons P18 image, use that.
2. Query English Wikipedia for all generic exact title candidates in one
   Action-API request (including anniversary-derived release years).
3. Use Wikipedia generator-search + `pageimages`, validating each returned
   article against the canonical movie title.
4. Search Wikidata by the canonical title. Accept only strongly matching
   film/movie/documentary entities; then use an English-Wikipedia sitelink or
   a Commons P18 image if available.
5. Leave the placeholder if Wikimedia has no usable image.

A missing poster is preferable to a confidently wrong poster.
"""

from __future__ import annotations

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

# Use a descriptive UA per Wikimedia API etiquette. A repository URL makes
# automated traffic identifiable without exposing personal information.
USER_AGENT = (
    "AMC-weekend-movie-report/3.0 "
    "(https://github.com/benpark2/AMC; Wikimedia metadata lookup)"
)

# Match both the deployed placeholder URL and browser-saved variants such as
# "images-not-found_QI6q.webp".
PLACEHOLDER_TOKEN = "images-not-found"

IMDB_ID_RE = re.compile(r"/title/(tt\d{5,12})(?:/|$)", re.IGNORECASE)
QID_RE = re.compile(r"^Q\d+$")

# Wikimedia transient failures are retried conservatively. We do not retry
# permanent 4xx responses other than 429.
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
MAX_API_ATTEMPTS = 3

# Small in-process cache because the same Wikipedia page can be reached through
# IMDb/Wikidata and title search during one report build.
_JSON_CACHE: dict[str, dict | None] = {}


def _api_url(base: str, params: dict[str, object]) -> str:
    """Build a deterministic API URL, useful for caching and tests."""
    encoded = urlencode(
        [(str(k), str(v)) for k, v in params.items() if v is not None]
    )
    return f"{base}?{encoded}"


def _fetch_json(url: str, timeout: int = 20) -> dict | None:
    """
    Fetch one Wikimedia JSON response with a small retry policy.

    Network/API failure is a normal lookup miss, not a report-build failure.
    This is intentional: one unavailable poster should not block all showtimes.
    """
    if url in _JSON_CACHE:
        return _JSON_CACHE[url]

    for attempt in range(MAX_API_ATTEMPTS):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
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

            # Respect Retry-After when present, but cap it so one image lookup
            # cannot stall a daily report for a long period.
            retry_after = 0.0
            try:
                retry_after = float(exc.headers.get("Retry-After", "0") or 0)
            except (TypeError, ValueError):
                retry_after = 0.0

            if attempt + 1 < MAX_API_ATTEMPTS:
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
    """Convenience wrapper around _fetch_json for a Wikimedia Action API."""
    return _fetch_json(_api_url(base, params))


def _normalized_match_text(value: str) -> str:
    """
    Normalize a page/movie title for cautious identity comparison.

    We remove ordinary Wikipedia film disambiguators and punctuation but keep
    the words themselves. This handles capitalization, curly apostrophes, "&"
    versus "and", and AMC censor asterisks without knowing any movie name.
    """
    value = clean_amc_title(value)

    # Common Wikipedia film disambiguators. Do not remove arbitrary
    # parenthetical text from genuine movie titles.
    value = re.sub(
        r"\s*\((?:\d{4}\s+)?(?:film|movie)\)\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )

    value = value.replace("&", " and ").replace("*", "")
    value = value.replace("’", "'")
    value = value.casefold()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _title_similarity(a: str, b: str) -> float:
    """Return a conservative normalized string-similarity score."""
    aa = _normalized_match_text(a)
    bb = _normalized_match_text(b)

    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0

    return SequenceMatcher(None, aa, bb).ratio()


def _candidate_identity_score(page_title: str, movie_title: str) -> float:
    """Best similarity between a Wikimedia page and generic title variants."""
    variants = candidate_title_variants(canonical_movie_title(movie_title))
    return max(
        (_title_similarity(page_title, variant) for variant in variants),
        default=0.0,
    )


def _image_from_page(page: dict) -> str | None:
    """
    Extract the image selected by MediaWiki's PageImages extension.

    `thumbnail` is preferred to avoid unnecessarily large files. `original`
    remains a fallback because some pages expose one but not the requested
    thumbnail size.
    """
    if not isinstance(page, dict):
        return None

    thumbnail = (page.get("thumbnail") or {}).get("source")
    original = (page.get("original") or {}).get("source")

    for value in (thumbnail, original):
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            return value

    return None


def _page_is_disambiguation(page: dict) -> bool:
    """MediaWiki marks disambiguation pages in pageprops."""
    pageprops = page.get("pageprops") or {}
    return "disambiguation" in pageprops


def _pages_from_query(data: dict | None) -> list[dict]:
    """Normalize Action-API query.pages (dict or list) into a list."""
    if not isinstance(data, dict):
        return []

    pages = (data.get("query") or {}).get("pages") or {}

    if isinstance(pages, dict):
        return [page for page in pages.values() if isinstance(page, dict)]

    if isinstance(pages, list):
        return [page for page in pages if isinstance(page, dict)]

    return []


def _wikipedia_pages_for_titles(titles: list[str]) -> list[dict]:
    """
    Resolve many exact candidate page titles in one API request.

    `redirects=1` lets Wikipedia canonicalize redirecting titles. The response
    includes PageImages data, so no second REST request is needed.
    """
    clean_titles: list[str] = []
    seen: set[str] = set()

    for title in titles:
        value = clean_amc_title(title)
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            clean_titles.append(value)

    if not clean_titles:
        return []

    # MediaWiki allows multiple titles per request. The candidate list here is
    # deliberately small, but chunking avoids depending on a particular API
    # account's max-title limit.
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
            # Movie posters are frequently non-free fair-use images on
            # Wikipedia. PageImages defaults to free-only unless this is set.
            pilicense="any",
            titles="|".join(batch),
        )
        out.extend(_pages_from_query(data))

    return out


def _wikipedia_search_pages(query: str, limit: int = 8) -> list[dict]:
    """
    Search Wikipedia and return page + image data in one request.

    `generator=search` is preferable to a list=search call followed by one REST
    request per result: fewer requests, fewer rate-limit opportunities.
    """
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
        # Same reason as exact-title lookup: allow Wikipedia's selected
        # non-free movie poster when the article uses one.
        pilicense="any",
    )

    pages = _pages_from_query(data)

    # generator=search normally supplies an `index` field. Preserve relevance
    # order when it does, but remain compatible if Wikimedia omits it.
    return sorted(
        pages,
        key=lambda page: (
            page.get("index") if isinstance(page.get("index"), int) else 999999
        ),
    )


def _best_wikipedia_page(
    pages: list[dict],
    movie_title: str,
    *,
    minimum_similarity: float,
) -> tuple[str | None, str | None]:
    """
    Select the safest poster-bearing Wikipedia result.

    Disambiguation pages and low-similarity titles are rejected. Among valid
    candidates, title identity outranks search order.
    """
    candidates: list[tuple[float, str, str]] = []

    for page in pages:
        if page.get("missing") is not None:
            continue
        if _page_is_disambiguation(page):
            continue

        page_title = clean_amc_title(str(page.get("title", "")))
        image = _image_from_page(page)
        if not page_title or not image:
            continue

        score = _candidate_identity_score(page_title, movie_title)
        if score < minimum_similarity:
            continue

        candidates.append((score, page_title, image))

    if not candidates:
        return None, None

    candidates.sort(key=lambda item: item[0], reverse=True)
    _, page_title, image = candidates[0]
    return image, page_title


def _extract_imdb_id(row) -> str | None:
    """
    Read an IMDb ID already present in the generated report.

    No request is made to imdb.com. The ID is merely an external identifier
    that Wikidata can match through property P345.
    """
    link = row.select_one('a[href*="imdb.com/title/"]')
    if link is None:
        return None

    href = str(link.get("href", ""))
    match = IMDB_ID_RE.search(href)
    return match.group(1).lower() if match else None


def _wikidata_qids_for_imdb(imdb_id: str) -> list[str]:
    """
    Find Wikidata items carrying an exact IMDb P345 identifier.

    Wikidata search supports the `haswbstatement:` keyword. The returned entity
    is still revalidated against its P345 claim before it is trusted.
    """
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

    qids: list[str] = []
    for hit in hits:
        qid = str(hit.get("title", ""))
        if QID_RE.fullmatch(qid):
            qids.append(qid)

    return qids


def _wikidata_qids_for_title(title: str, limit: int = 8) -> list[str]:
    """
    Search Wikidata by canonical movie title.

    This is a final fallback for rows without an IMDb ID or without an English
    Wikipedia page. Identity is validated again after entity details are loaded.
    """
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
    qids: list[str] = []

    for hit in hits:
        qid = str(hit.get("id", ""))
        label = clean_amc_title(str(hit.get("label", "")))
        description = clean_amc_title(str(hit.get("description", ""))).casefold()

        if not QID_RE.fullmatch(qid):
            continue

        # Require near-exact title identity and a film-ish description. This is
        # intentionally conservative for generic names shared by books/songs.
        if _title_similarity(label, title) < 0.92:
            continue

        filmish_words = (
            "film",
            "movie",
            "documentary",
            "motion picture",
            "concert film",
        )
        if description and not any(word in description for word in filmish_words):
            continue

        qids.append(qid)

    return qids


def _claim_string_values(entity: dict, property_id: str) -> list[str]:
    """Return string-valued Wikidata claim values for one property."""
    claims = (entity.get("claims") or {}).get(property_id) or []
    out: list[str] = []

    for claim in claims:
        try:
            value = claim["mainsnak"]["datavalue"]["value"]
        except (KeyError, TypeError):
            continue

        if isinstance(value, str) and value:
            out.append(value)

    return out


def _claim_entity_values(entity: dict, property_id: str) -> list[str]:
    """Return item QIDs from entity-valued Wikidata claims."""
    claims = (entity.get("claims") or {}).get(property_id) or []
    out: list[str] = []

    for claim in claims:
        try:
            value = claim["mainsnak"]["datavalue"]["value"]
        except (KeyError, TypeError):
            continue

        if not isinstance(value, dict):
            continue

        qid = value.get("id")
        if isinstance(qid, str) and QID_RE.fullmatch(qid):
            out.append(qid)

    return out


def _entity_is_filmish(entity: dict) -> bool:
    """
    Confirm a title-searched Wikidata entity is actually film-related.

    Q11424 is Wikidata's general "film" item and is commonly used directly as
    P31. The English description is a secondary signal for newly created items
    whose modeling may still be incomplete.
    """
    if "Q11424" in _claim_entity_values(entity, "P31"):
        return True

    description = (
        ((entity.get("descriptions") or {}).get("en") or {}).get("value")
    )
    if not isinstance(description, str):
        return False

    description = description.casefold()
    return any(
        word in description
        for word in (
            "film",
            "movie",
            "documentary",
            "motion picture",
            "concert film",
        )
    )


def _wikidata_entities(qids: list[str]) -> list[dict]:
    """Fetch claims, labels, aliases, descriptions and sitelinks for QIDs."""
    clean_qids: list[str] = []
    seen: set[str] = set()

    for qid in qids:
        if QID_RE.fullmatch(qid) and qid not in seen:
            seen.add(qid)
            clean_qids.append(qid)

    if not clean_qids:
        return []

    out: list[dict] = []

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
            out.extend(
                entity
                for entity in entities.values()
                if isinstance(entity, dict) and not entity.get("missing")
            )

    return out


def _entity_enwiki_title(entity: dict) -> str | None:
    """Return an entity's English-Wikipedia sitelink title."""
    sitelink = (entity.get("sitelinks") or {}).get("enwiki") or {}
    title = sitelink.get("title")

    if isinstance(title, str) and title.strip():
        return clean_amc_title(title)

    return None


def _entity_english_names(entity: dict) -> list[str]:
    """Collect an entity's English label and aliases for identity validation."""
    values: list[str] = []

    label = ((entity.get("labels") or {}).get("en") or {}).get("value")
    if isinstance(label, str):
        values.append(label)

    aliases = (entity.get("aliases") or {}).get("en") or []
    for alias in aliases:
        value = alias.get("value") if isinstance(alias, dict) else None
        if isinstance(value, str):
            values.append(value)

    return values


def _entity_matches_movie_title(entity: dict, movie_title: str) -> bool:
    """Require a strong label/alias match for title-based Wikidata fallback."""
    canonical = canonical_movie_title(movie_title)
    names = _entity_english_names(entity)

    return max(
        (_title_similarity(name, canonical) for name in names),
        default=0.0,
    ) >= 0.92


def _commons_url_from_entity(entity: dict) -> str | None:
    """
    Return a Commons-hosted image from a Wikidata film item.

    P3383 is Wikidata's dedicated "film poster" property, so it is preferred.
    P18 is a general image and is only a secondary fallback.

    Special:Redirect/file is a Wikimedia endpoint and supports a width
    parameter. No Commons HTML scraping is involved.
    """
    filenames = (
        _claim_string_values(entity, "P3383")
        or _claim_string_values(entity, "P18")
    )
    if not filenames:
        return None

    filename = filenames[0].strip()
    if not filename:
        return None

    return (
        COMMONS_FILE_REDIRECT
        + quote(filename.replace(" ", "_"), safe="()_',.-")
        + "?width=500"
    )


def _image_from_wikidata_entity(
    entity: dict,
    movie_title: str,
    *,
    imdb_id: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """
    Resolve one validated Wikidata entity to an image.

    Returns (image_url, wikipedia_page_title, source_label).
    """
    if imdb_id:
        imdb_values = {value.lower() for value in _claim_string_values(entity, "P345")}
        if imdb_id.lower() not in imdb_values:
            return None, None, None
    elif not _entity_matches_movie_title(entity, movie_title):
        return None, None, None

    enwiki_title = _entity_enwiki_title(entity)
    if enwiki_title:
        pages = _wikipedia_pages_for_titles([enwiki_title])
        image, page_title = _best_wikipedia_page(
            pages,
            movie_title,
            # IMDb identity is already exact, so an alternate regional title
            # on Wikipedia is safe even if its text similarity is lower.
            minimum_similarity=0.0 if imdb_id else 0.88,
        )
        if image:
            return image, page_title, "wikidata-enwiki"

    commons_image = _commons_url_from_entity(entity)
    if commons_image:
        return commons_image, None, "wikidata-commons"

    return None, None, None


def _image_via_imdb_wikidata(
    movie_title: str,
    imdb_id: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Use an existing IMDb identifier to resolve a Wikidata entity."""
    if not imdb_id:
        return None, None, None

    entities = _wikidata_entities(_wikidata_qids_for_imdb(imdb_id))
    for entity in entities:
        image, page_title, source = _image_from_wikidata_entity(
            entity,
            movie_title,
            imdb_id=imdb_id,
        )
        if image:
            return image, page_title, source

    return None, None, None


def _image_via_wikipedia_title(
    movie_title: str,
    *,
    reference_year: int,
) -> tuple[str | None, str | None, str | None]:
    """Resolve exact candidates, then validated Wikipedia search results."""
    exact_pages = _wikipedia_pages_for_titles(
        wikipedia_title_candidates(movie_title, reference_year=reference_year)
    )
    image, page_title = _best_wikipedia_page(
        exact_pages,
        movie_title,
        minimum_similarity=0.88,
    )
    if image:
        return image, page_title, "wikipedia-exact"

    canonical = canonical_movie_title(movie_title)
    terms = search_terms_for_title(canonical)

    queries = [
        f'"{canonical}" film',
        f'intitle:"{canonical}" film',
        f"{terms} film",
    ]

    seen_page_ids: set[object] = set()

    for query in queries:
        pages = []
        for page in _wikipedia_search_pages(query):
            page_id = page.get("pageid") or page.get("title")
            if page_id in seen_page_ids:
                continue
            seen_page_ids.add(page_id)
            pages.append(page)

        image, page_title = _best_wikipedia_page(
            pages,
            movie_title,
            minimum_similarity=0.88,
        )
        if image:
            return image, page_title, "wikipedia-search"

    return None, None, None


def _image_via_wikidata_title(
    movie_title: str,
) -> tuple[str | None, str | None, str | None]:
    """
    Final generic Wikimedia fallback for films without a usable enwiki search.

    This can help newly released films that have a Wikidata item before they
    have an English-Wikipedia article, provided the item has a P18 image.
    """
    canonical = canonical_movie_title(movie_title)
    qids = _wikidata_qids_for_title(canonical)
    entities = _wikidata_entities(qids)

    for entity in entities:
        if not _entity_is_filmish(entity):
            continue

        image, page_title, source = _image_from_wikidata_entity(
            entity,
            movie_title,
        )
        if image:
            return image, page_title, source

    return None, None, None


def resolve_poster(
    movie_title: str,
    *,
    imdb_id: str | None = None,
    reference_year: int | None = None,
) -> tuple[str | None, str | None, str | None]:
    """
    Resolve a poster using only generic Wikimedia-backed strategies.

    Returns (image_url, wikipedia_page_title, source_label).
    """
    ref_year = reference_year or datetime.now(timezone.utc).year

    image, page_title, source = _image_via_imdb_wikidata(
        movie_title,
        imdb_id,
    )
    if image:
        return image, page_title, source

    image, page_title, source = _image_via_wikipedia_title(
        movie_title,
        reference_year=ref_year,
    )
    if image:
        return image, page_title, source

    return _image_via_wikidata_title(movie_title)


def _is_placeholder(src: str | None) -> bool:
    """Recognize deployed and browser-saved forms of the missing-image asset."""
    return not src or PLACEHOLDER_TOKEN in src.casefold()


def main() -> int:
    if not HTML_PATH.exists():
        raise RuntimeError(f"{HTML_PATH} does not exist.")

    soup = BeautifulSoup(
        HTML_PATH.read_text(encoding="utf-8"),
        "html.parser",
    )
    current_year = datetime.now(timezone.utc).year

    rows = soup.select("tr[data-movie-id]")
    updated = 0
    unresolved: list[str] = []

    for row in rows:
        title_el = row.select_one(".movie-cell-title")
        if title_el is None:
            continue

        display_title = clean_amc_title(title_el.get_text(" ", strip=True))
        if not display_title:
            continue

        # Rental inventory should have been removed upstream before movie IDs
        # and planner JSON were generated. A surviving row means that upstream
        # invariant broke, so fail loudly instead of corrupting IDs downstream.
        if is_non_movie_title(display_title):
            raise RuntimeError(
                "Non-movie inventory survived notebook filtering: "
                f"{display_title!r}"
            )

        canonical = canonical_movie_title(display_title)

        # Keep AMC's display title visible, but use the actual film title for
        # the trailer search.
        poster_link = row.select_one(".movie-poster-wrap a")
        if poster_link is not None:
            trailer_query = quote(f"{canonical} official trailer")
            poster_link["href"] = (
                "https://www.youtube.com/results?search_query="
                + trailer_query
            )

        img = row.select_one("img.movie-poster")
        if img is None:
            continue

        old_src = str(img.get("src", ""))

        # Preserve any poster that postprocess_report.py already resolved.
        if not _is_placeholder(old_src):
            img["data-lookup-title"] = canonical
            continue

        imdb_id = _extract_imdb_id(row)
        image_url, wiki_page, source = resolve_poster(
            display_title,
            imdb_id=imdb_id,
            reference_year=current_year,
        )

        if image_url:
            img["src"] = image_url
            img["data-poster-source"] = source or "wikimedia"
            img["data-lookup-title"] = canonical
            if wiki_page:
                img["data-wikipedia-page"] = wiki_page
            updated += 1

            detail = f" -> {wiki_page!r}" if wiki_page else ""
            print(
                f"[POSTER] {source or 'wikimedia'}: "
                f"{display_title!r}{detail}"
            )
        else:
            unresolved.append(display_title)
            print(
                "[POSTER] No usable Wikimedia image: "
                f"{display_title!r}"
            )

    HTML_PATH.write_text(str(soup), encoding="utf-8")

    print(
        "[OK] Wikimedia poster finalization complete: "
        f"{updated} replacements, {len(unresolved)} unresolved."
    )

    if unresolved:
        print(
            "[INFO] Unresolved titles are intentionally left as placeholders. "
            "No per-movie overrides or non-Wikimedia scrapers are used."
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
