#!/usr/bin/env python3
"""
Resolve missing posters after postprocess_report.py.

Wikipedia remains the ONLY automated poster source used by this script.
There are intentionally NO movie-specific poster URLs or movie-name mappings.

Why a second pass?
------------------
The existing postprocessor already succeeds for many films. This script leaves
those images alone and only revisits placeholder/missing posters using the
shared canonical-title logic.

Lookup order
------------
1. Exact English-Wikipedia page-title candidates based on the canonical title.
2. Anniversary-derived original-year candidates for repertory presentations.
3. A tightly validated MediaWiki search fallback.

If Wikipedia has no usable image, the placeholder remains. That is preferable
to maintaining title-by-title exceptions or adding a scraper that is likely to
break under anti-bot protections.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from difflib import SequenceMatcher
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
USER_AGENT = (
    "AMC-weekend-movie-report/2.0 "
    "(Wikipedia metadata lookup for a GitHub Pages movie report)"
)
KNOWN_PLACEHOLDER_FRAGMENT = "images-not-found.webp"


def _fetch_json(url: str, timeout: int = 15) -> dict | None:
    """Fetch JSON from a public Wikimedia API; failures simply trigger fallback."""
    try:
        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None


def _summary_image(page_title: str) -> str | None:
    """
    Return a Wikipedia summary image for an exact page title.

    Prefer the thumbnail but accept originalimage when Wikipedia exposes only
    that field. Both come from the same official REST summary response.
    """
    encoded = quote(page_title.replace(" ", "_"), safe="()_'&")
    data = _fetch_json(
        f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}"
    )
    if not data:
        return None

    thumbnail = (data.get("thumbnail") or {}).get("source")
    original = (data.get("originalimage") or {}).get("source")
    return thumbnail or original


def _normalized_match_text(value: str) -> str:
    """
    Normalize titles for safe comparison of MediaWiki search hits.

    Asterisks/punctuation are ignored so AMC censorship does not force a
    movie-specific alias. Year/"film" disambiguators are removed separately.
    """
    value = clean_amc_title(value)
    value = re.sub(
        r"\s*\((?:\d{4}\s+)?film\)\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = value.replace("&", " and ").replace("*", "")
    value = value.casefold()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _title_similarity(a: str, b: str) -> float:
    """
    Similarity score for validating Wikipedia search results.

    Exact normalized matches score 1.0. SequenceMatcher handles small
    punctuation/censorship differences without knowing any particular title.
    """
    aa = _normalized_match_text(a)
    bb = _normalized_match_text(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    return SequenceMatcher(None, aa, bb).ratio()


def _is_safe_search_hit(
    page_title: str,
    title: str,
) -> bool:
    """
    Reject loose Wikipedia search results that might show the wrong movie.

    The result must closely resemble at least one generic candidate variant.
    A deliberately high threshold favors a missing poster over a wrong poster.
    """
    variants = candidate_title_variants(canonical_movie_title(title))
    best = max(
        (_title_similarity(page_title, variant) for variant in variants),
        default=0.0,
    )
    return best >= 0.88


def _mediawiki_search(query: str, limit: int = 8) -> list[str]:
    """Return page titles from MediaWiki's documented search API."""
    params = urlencode(
        {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "srlimit": limit,
        }
    )
    data = _fetch_json(f"https://en.wikipedia.org/w/api.php?{params}") or {}

    try:
        hits = data["query"]["search"]
    except (KeyError, TypeError):
        return []

    return [
        clean_amc_title(str(hit.get("title", "")))
        for hit in hits
        if hit.get("title")
    ]


def wikipedia_image_url(
    title: str,
    *,
    reference_year: int | None = None,
) -> tuple[str | None, str | None]:
    """
    Return (image_url, Wikipedia_page_title) for an AMC display title.

    No title-specific exceptions exist here. Exact candidates are followed by
    two generic search queries and a strict similarity validation step.
    """
    ref_year = reference_year or datetime.now(timezone.utc).year

    for page_title in wikipedia_title_candidates(title, reference_year=ref_year):
        image = _summary_image(page_title)
        if image:
            return image, page_title

    canonical = canonical_movie_title(title)
    terms = search_terms_for_title(canonical)

    # Quoted canonical search is precise; simplified terms help when AMC
    # punctuation/censorship differs from Wikipedia's article title.
    queries = [
        f'"{canonical}" film',
        f"{terms} film",
        terms,
    ]

    seen_pages: set[str] = set()
    for query in queries:
        if not query.strip():
            continue

        for page_title in _mediawiki_search(query):
            key = page_title.casefold()
            if key in seen_pages:
                continue
            seen_pages.add(key)

            if not _is_safe_search_hit(page_title, title):
                continue

            image = _summary_image(page_title)
            if image:
                return image, page_title

    return None, None


def _is_placeholder(src: str | None) -> bool:
    return not src or KNOWN_PLACEHOLDER_FRAGMENT in src


def main() -> int:
    if not HTML_PATH.exists():
        raise RuntimeError(f"{HTML_PATH} does not exist.")

    soup = BeautifulSoup(HTML_PATH.read_text(encoding="utf-8"), "html.parser")
    current_year = datetime.now(timezone.utc).year

    rows = soup.select("tr[data-movie-id]")
    updated = 0
    unresolved: list[str] = []

    for row in rows:
        title_el = row.select_one(".movie-cell-title")
        if not title_el:
            continue

        display_title = clean_amc_title(title_el.get_text(" ", strip=True))
        if not display_title:
            continue

        # The notebook patch removes rentals before planner/movie IDs are built.
        # A surviving rental means the upstream patch failed, so fail loudly.
        if is_non_movie_title(display_title):
            raise RuntimeError(
                "Non-movie inventory survived notebook filtering: "
                f"{display_title!r}"
            )

        canonical = canonical_movie_title(display_title)

        # Preserve AMC's visible presentation title; use the canonical movie
        # title only for the trailer search.
        poster_link = row.select_one(".movie-poster-wrap a")
        if poster_link is not None:
            trailer_query = quote(f"{canonical} official trailer")
            poster_link["href"] = (
                "https://www.youtube.com/results?search_query=" + trailer_query
            )

        img = row.select_one("img.movie-poster")
        if img is None:
            continue

        old_src = str(img.get("src", ""))

        # The existing postprocessor already handled this row. Avoid duplicate
        # Wikimedia requests and preserve a known-good result.
        if not _is_placeholder(old_src):
            img["data-lookup-title"] = canonical
            continue

        image_url, wiki_page = wikipedia_image_url(
            display_title,
            reference_year=current_year,
        )

        if image_url:
            img["src"] = image_url
            img["data-poster-source"] = "wikipedia"
            img["data-lookup-title"] = canonical
            if wiki_page:
                img["data-wikipedia-page"] = wiki_page
            updated += 1
            print(
                f"[POSTER] Wikipedia: {display_title!r} -> {wiki_page!r}"
            )
        else:
            unresolved.append(display_title)
            print(f"[POSTER] Wikipedia has no usable image: {display_title!r}")

    HTML_PATH.write_text(str(soup), encoding="utf-8")

    print(
        "[OK] Generic poster finalization complete: "
        f"{updated} replacements, {len(unresolved)} unresolved."
    )
    if unresolved:
        print(
            "[INFO] Unresolved titles are intentionally left as placeholders; "
            "there are no per-movie overrides to maintain."
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
