#!/usr/bin/env python3
"""
Patch the AMC notebook into build/ before Papermill executes it.

This patcher is intentionally conservative:
1. Include the current Pacific calendar date plus the next two Saturday/Sunday
   calendar days strictly after it, so today's listings are never omitted.
2. Replace the notebook's candidate_title_variants() with the shared generic
   implementation in scripts/movie_titles.py.
3. Remove non-film AMC inventory before ratings/numbering/planner generation.
4. Improve IMDb candidate scoring so it considers every canonical lookup
   variant while preserving leading articles for final identity ranking.
5. Keep Rotten Tomatoes on its existing direct-page method, but add the
   original release-year hint for anniversary/repertory titles and parse scores
   only when critic/audience labels are explicit.
6. Emit an explicit RT_C/A display column so a missing side renders as "-".
7. Parse AMC's current escaped React/Next showtime payload without relying on
   brittle field ordering.
8. Merge server-rendered, browser-rendered, and retry/cache discoveries for
   every theatre/date without inferring inventory from screen count.
9. Measure movie identity independently from the row parser. Treat only
   directly rendered, clickable movie/time links as hard showtime-completeness
   evidence; a title/card with no trusted rendered showtime is diagnostic only,
   because AMC can render placeholder/orphan/duplicate movie shells. Embedded
   React/Next time records are likewise diagnostic because AMC can duplicate or
   supersede them during SSR/browser hydration.
10. Persist versioned source snapshots and parsed rows across Papermill retries
    so complementary attempts are merged without carrying forward obsolete
    raw-ID or serialized-slot assumptions.
11. Let the Playwright fallback wait for AMC's dynamically rendered showtime
    DOM to stabilize instead of taking a fixed-delay snapshot.
12. If live sources remain incomplete, allow only a recent, same-weekend prior
    successful report to act as a transparent last-good showtime cache;
    preserve per-showtime A-List exclusions while doing so.
13. Reject structural aggregate failures and unresolved source/parser coverage
    gaps, while accepting genuinely small schedules when AMC itself exposes
    only a small schedule.
14. Preserve AMC's explicit A-List exclusion metadata (NOALIST) through SSR and
    rendered-DOM recovery paths.
15. Keep scraper INFO/WARN diagnostics out of the public HTML while leaving
    them available in the GitHub Actions execution log.

Important safety property
-------------------------
IMDb edits are made ONLY inside build_imdb_lookup(), identified via Python's
AST. The script validates the transformed function before writing the notebook.

This avoids brittle notebook-wide string replacement. If the notebook changes
in a way this patcher does not understand, the build fails before Papermill
rather than producing a partially patched notebook.
"""

from __future__ import annotations

import argparse
import ast
import base64
import json
from pathlib import Path
import sys



UPCOMING_WEEKEND_REPLACEMENT = r'''def current_pacific_date() -> date:
    """Return today's calendar date in America/Los_Angeles."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).date()
    except Exception:
        return date.today()


def upcoming_weekend_pacific() -> Tuple[date, date]:
    """Return the next two weekend calendar days after today (Pacific time).

    "Next" is intentionally strict: today itself is never returned. The
    caller separately includes ``current_pacific_date()`` so every report has
    today's schedule plus the two future weekend days used for planning.

    OVERRIDE_SATURDAY keeps its historical meaning: when explicitly supplied,
    it names the first day of a forced Saturday/Sunday pair.
    """
    today = current_pacific_date()

    if OVERRIDE_SATURDAY:
        sat = datetime.strptime(OVERRIDE_SATURDAY, "%Y-%m-%d").date()
        return sat, sat + timedelta(days=1)

    weekend_days = []
    cursor = today + timedelta(days=1)
    while len(weekend_days) < 2:
        if cursor.weekday() in (5, 6):  # Saturday=5, Sunday=6
            weekend_days.append(cursor)
        cursor += timedelta(days=1)

    return weekend_days[0], weekend_days[1]
'''

WEEKEND_PRINT_OLD = 'print(f"Upcoming weekend (Pacific): {sat.isoformat()} (Sat), {sun.isoformat()} (Sun)")'
WEEKEND_PRINT_NEW = 'print("Requested dates (Pacific): " + ", ".join(f"{d.isoformat()} ({d.strftime(\'%a\')})" for d in dates))'

DATE_LIST_OLD = 'dates = [sat, sun]'
DATE_LIST_NEW = 'dates = list(dict.fromkeys([current_pacific_date(), sat, sun]))'

# Probe all configured theatres for a date before advancing to the next date.
# This prevents a single problematic theatre from supplying the first three
# consecutive failures and prematurely opening the run-wide AMC circuit.
AMC_SCRAPE_LOOP_OLD = """for th in THEATRES:
    for d in dates:
        try:
            showtimes.extend(scrape_amc_showtimes_for_date(session, th["name"], th["url"], d))
            time.sleep(0.2)
        except Exception as e:
            print(f"[WARN] Failed scraping {th['name']} {d}: {e}")"""
AMC_SCRAPE_LOOP_NEW = """for d in dates:
    for th in THEATRES:
        try:
            showtimes.extend(scrape_amc_showtimes_for_date(session, th["name"], th["url"], d))
            time.sleep(0.2)
        except Exception as e:
            print(f"[WARN] Failed scraping {th['name']} {d}: {e}")"""

FUNCTION_WRAPPER = """def candidate_title_variants(title: str) -> List[str]:
    \"\"\"Use the shared generic AMC-title normalizer for metadata lookup.\"\"\"
    from scripts.movie_titles import candidate_title_variants as _shared_variants
    return _shared_variants(title)


def metadata_release_year_hint(title: str, reference_year: int | None = None) -> Optional[int]:
    \"\"\"Return the original-feature year encoded by an AMC presentation title.\"\"\"
    from scripts.movie_titles import metadata_release_year_hint as _shared_year_hint
    return _shared_year_hint(title, reference_year=reference_year)
"""


RT_PARSE_REPLACEMENT = r'''def rt_parse_scores(decoded_html: str, soup: BeautifulSoup) -> Tuple[Optional[int], Optional[int]]:
    """
    Return (audience, critic) from the visible Rotten Tomatoes scoreboard.

    Rotten Tomatoes can leave stale/hidden component attributes in the page
    DOM even when the visible Popcornmeter has no published percentage. The
    visible scoreboard is therefore authoritative whenever it is present.
    """
    full_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    # Work from the first public score-board heading, not the whole page. The
    # rest of an RT page can contain recommendation cards, historical/hidden
    # components, and many unrelated percentages.
    scoreboard = None
    for anchor_rx in (
        r"\bWatchlist\s+Tomatometer\s+Popcornmeter\b",
        r"\bTomatometer\s+Popcornmeter\b",
    ):
        m = re.search(anchor_rx, full_text, re.I)
        if m:
            scoreboard = full_text[m.start() : m.start() + 1000]
            break

    if scoreboard is None:
        # Older/alternate RT markup may omit the combined heading. Limit the
        # fallback to the first local score area around the movie heading.
        h1 = soup.find("h1")
        tail = full_text
        if h1:
            anchor = re.sub(r"\s+", " ", h1.get_text(" ", strip=True)).strip()
            if anchor:
                pos = full_text.casefold().find(anchor.casefold())
                if pos >= 0:
                    tail = full_text[pos:]
        first_label = re.search(
            r"\b(?:Tomatometer|Popcornmeter|Audience\s*Score)\b",
            tail,
            re.I,
        )
        if first_label:
            scoreboard = tail[first_label.start() : first_label.start() + 1000]

    def _labeled_score(text: str, label: str) -> Optional[int]:
        for pattern in (
            re.compile(rf"(\d{{1,3}})\s*[％%]\s*{label}\b", re.I),
            re.compile(rf"\b{label}\s*(\d{{1,3}})\s*[％%]", re.I),
        ):
            match = pattern.search(text)
            if match:
                value = _int0_100(match.group(1))
                if value is not None:
                    return value
        return None

    if scoreboard is not None:
        critic = _labeled_score(scoreboard, "Tomatometer")
        audience = _labeled_score(
            scoreboard,
            r"(?:Popcornmeter|Audience\s*Score)",
        )

        # RT withholds the Popcornmeter percentage below its publication
        # threshold. Hidden/stale attributes must never override this visible
        # no-score state.
        audience_unpublished = re.search(
            r"\bPopcornmeter\s+(?:"
            r"(?:0|No)\s+(?:Verified\s+)?Ratings?"
            r"|Fewer\s+than\s+\d+(?:\+)?\s+(?:Verified\s+)?Ratings?"
            r"|Not\s+Enough\s+(?:Verified\s+)?Ratings?"
            r")\b",
            scoreboard,
            re.I,
        )
        if audience_unpublished:
            audience = None

        # A visible scoreboard is authoritative. Do not fall through to
        # hidden DOM attributes just because one side is blank.
        return audience, critic

    # Last-resort compatibility path for an RT layout with no visible labels.
    # Even here, only semantically named score attributes are considered.
    critic = None
    audience = None

    for attr in (
        "tomatometerscore",
        "tomatometerScore",
        "tomatometerscoreallcritics",
        "tomatometerscoreall",
    ):
        tag = soup.find(attrs={attr: True})
        if tag:
            critic = _int0_100(tag.get(attr))
            if critic is not None:
                break

    for attr in (
        "audiencescore",
        "audienceScore",
        "popcornmeterscore",
        "popcornmeterScore",
    ):
        tag = soup.find(attrs={attr: True})
        if tag:
            audience = _int0_100(tag.get(attr))
            if audience is not None:
                break

    return audience, critic
'''

RT_GET_SCORES_REPLACEMENT = r'''def rt_get_scores(
    session: requests.Session,
    title: str,
    cache: Dict[str, Tuple[Optional[int], Optional[int], Optional[str]]],
    debug: bool = False,
    runtime_min: Optional[int] = None) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """
    Resolve one identity-compatible Rotten Tomatoes movie page, then read scores.

    Provider search is intentionally tried before a long series of guessed URLs.
    Exact title is mandatory; release-year and AMC runtime evidence reject older
    same-title films. A validated page with no published percentage remains a
    useful identity, but we keep looking briefly for another compatible page
    before returning blanks. No movie aliases or fixed years are encoded.
    """
    from urllib.parse import quote as _rt_quote, urljoin as _rt_urljoin

    key = (title or "").lower().strip()
    cache_key = f"{key}\x1f{int(runtime_min) if runtime_min is not None else ''}"
    if cache_key in cache:
        return cache[cache_key]

    def _literal_norm(value: str) -> str:
        value = html_lib.unescape(value or "").casefold()
        value = value.replace("’", "'").replace("–", "-").replace("—", "-")
        value = re.sub(r"\s*\|\s*rotten tomatoes\s*$", "", value)
        value = re.sub(r"[^a-z0-9]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    def _page_title(soup: BeautifulSoup) -> str:
        heading = soup.find("h1")
        if heading:
            value = heading.get_text(" ", strip=True)
            if value:
                return value
        meta = soup.find("meta", attrs={"property": "og:title"})
        if meta and meta.get("content"):
            return str(meta.get("content"))
        if soup.title:
            return soup.title.get_text(" ", strip=True)
        return ""

    def _page_text(soup: BeautifulSoup) -> str:
        return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    def _page_release_year(soup: BeautifulSoup) -> Optional[int]:
        text = _page_text(soup)
        for pattern in (
            r"Release Date\s*\(Theaters\)\s*.{0,100}?\b((?:19|20)\d{2})\b",
            r"Release Date\s*.{0,100}?\b((?:19|20)\d{2})\b",
        ):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text(" ", strip=True)
            match = re.search(
                r'"(?:datePublished|dateCreated)"\s*:\s*"((?:19|20)\d{2})',
                raw or "",
                flags=re.IGNORECASE,
            )
            if match:
                return int(match.group(1))
        return None

    def _page_runtime_min(soup: BeautifulSoup) -> Optional[int]:
        text = _page_text(soup)
        snippets = []
        m = re.search(r"\bRuntime\b(.{0,90})", text, flags=re.IGNORECASE)
        if m:
            snippets.append(m.group(1))
        page_title = _page_title(soup)
        if page_title:
            pos = text.casefold().find(clean_amc_title(page_title).casefold())
            if pos >= 0:
                snippets.append(text[pos : pos + 320])
        snippets.append(text[:420])
        for snippet in snippets:
            h = re.search(r"\b(\d{1,2})\s*h(?:ours?)?\s*(?:(\d{1,2})\s*m)?", snippet, re.I)
            if h:
                return int(h.group(1)) * 60 + int(h.group(2) or 0)
            m2 = re.search(r"\b(\d{2,3})\s*(?:min|mins|minutes)\b", snippet, re.I)
            if m2:
                return int(m2.group(1))
        return None

    def _identity_matches(
        soup: BeautifulSoup,
        query_title: str,
        expected_year: Optional[int],
    ) -> bool:
        page_norm = _literal_norm(_page_title(soup))
        target_norms = {
            _literal_norm(v) for v in candidate_title_variants(query_title) if v
        }
        if not page_norm or page_norm not in target_norms:
            return False
        page_year = _page_release_year(soup)
        if expected_year is not None and page_year is not None:
            if abs(int(page_year) - int(expected_year)) > 1:
                return False
        page_runtime = _page_runtime_min(soup)
        # If a candidate is supposed to represent a specific release but the
        # page itself exposes no year, do not accept it on title alone.  A
        # matching runtime can still prove continuity for sparse provider
        # pages; otherwise the candidate is too ambiguous and could be an
        # older same-title film.
        if expected_year is not None and page_year is None and runtime_min is not None:
            if page_runtime is None or abs(int(page_runtime) - int(runtime_min)) > 20:
                return False
        if runtime_min is not None and page_runtime is not None:
            if abs(int(page_runtime) - int(runtime_min)) > 20:
                return False
        return True

    def _canonical_url(soup: BeautifulSoup, fallback: str) -> str:
        link = soup.find("link", attrs={"rel": re.compile(r"\bcanonical\b", re.I)})
        if link and link.get("href"):
            value = _rt_urljoin(RT_BASE, str(link.get("href")))
            if "/m/" in value:
                return value.split("?", 1)[0].split("#", 1)[0]
        meta = soup.find("meta", attrs={"property": "og:url"})
        if meta and meta.get("content"):
            value = _rt_urljoin(RT_BASE, str(meta.get("content")))
            if "/m/" in value:
                return value.split("?", 1)[0].split("#", 1)[0]
        return fallback

    fetched_urls = set()
    best_blank = None
    best_blank_year = None

    def _fetch_validated(rt_url: str, q: str, expected_year: Optional[int]):
        nonlocal best_blank, best_blank_year
        url_key = rt_url.casefold()
        if url_key in fetched_urls:
            return None
        fetched_urls.add(url_key)
        status, raw_html = fetch_html(session, rt_url, params=None, tries=2)
        if debug:
            print(f"[RT] Testing {rt_url} -> Status: {status}")
        if status != 200 or not raw_html:
            return None
        lower_html = raw_html.lower()
        if "404 - page not found" in lower_html or "couldn't find the page you're looking for" in lower_html:
            return None
        decoded = html_lib.unescape(raw_html)
        soup = BeautifulSoup(decoded, "html.parser")
        if not _identity_matches(soup, q, expected_year):
            if debug:
                print(f"[RT] Rejected identity mismatch: {rt_url}")
            return None
        aud, crit = rt_parse_scores(decoded, soup)
        page_year = _page_release_year(soup)
        canonical = _canonical_url(soup, rt_url)
        result = (aud, crit, canonical)
        if aud is not None or crit is not None:
            return result
        if best_blank is None:
            best_blank = result
            best_blank_year = page_year or expected_year
        return None

    current_year = date.today().year
    hinted_year = metadata_release_year_hint(title, reference_year=current_year)

    def _search_item_year(item, path: str) -> Optional[int]:
        raw_year = item.get("year") or item.get("releaseYear") or item.get("release_year")
        try:
            year = int(raw_year) if raw_year not in (None, "") else None
        except (TypeError, ValueError):
            year = None
        if year is not None:
            return year
        # RT search occasionally omits a structured year even when the movie
        # URL itself carries one.  Recover only a terminal four-digit slug
        # suffix; this is generic provider parsing, not a movie-specific map.
        match = re.search(r"[_-]((?:19|20)\d{2})(?:/)?$", path or "")
        if match:
            return int(match.group(1))
        return None

    # Search first: one lightweight provider query usually identifies the exact
    # current/repertory movie and avoids probing several guessed year URLs.  If
    # AMC supplied a reliable original-year hint (for example an anniversary),
    # ask RT for the year-qualified title first while still validating results
    # against the underlying canonical movie title.
    search_specs = []
    seen_search_specs = set()
    for q in candidate_title_variants(title):
        terms = [f"{q} {hinted_year}", q] if hinted_year is not None else [q]
        for search_text in terms:
            marker = (search_text.casefold(), q.casefold())
            if marker not in seen_search_specs:
                seen_search_specs.add(marker)
                search_specs.append((search_text, q))

    for search_text, q in search_specs:
        search_url = f"{RT_BASE}/napi/search/?query={_rt_quote(search_text)}&limit=12"
        status, payload = fetch_html(session, search_url, params=None, tries=2)
        if debug:
            print(f"[RT] Search {search_url} -> Status: {status}")
        if status != 200 or not payload:
            continue
        try:
            data = json.loads(payload)
        except Exception:
            continue
        movies = data.get("movies", []) if isinstance(data, dict) else []
        if not isinstance(movies, list):
            continue

        target_norms = {_literal_norm(v) for v in candidate_title_variants(q) if v}
        ranked = []
        for index, item in enumerate(movies):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("title") or "")
            if _literal_norm(name) not in target_norms:
                continue
            path = str(item.get("url") or item.get("movieUrl") or "").strip()
            if not path:
                continue
            candidate_url = _rt_urljoin(RT_BASE, path)
            if "/m/" not in candidate_url:
                continue
            item_year = _search_item_year(item, path)
            if hinted_year is not None and item_year is not None and abs(item_year - hinted_year) > 1:
                continue
            target_year = hinted_year if hinted_year is not None else current_year
            # Unknown-year search results should not automatically lose to a
            # famous decades-old film.  Give them a modest neutral rank while
            # still preferring a known current/original-year match.
            year_rank = -4 if item_year is None else -abs(item_year - target_year)
            ranked.append((year_rank, -index, candidate_url, item_year))

        for _, _, rt_url, item_year in sorted(ranked, reverse=True)[:4]:
            expected_year = hinted_year if hinted_year is not None else item_year
            found = _fetch_validated(rt_url, q, expected_year)
            if found is not None:
                cache[cache_key] = found
                return found
        # A ranked search result can still be the wrong same-title release
        # after page-level runtime/year validation.  Only stop early once an
        # identity-compatible blank page was actually validated; otherwise a
        # year-qualified anniversary search gets one normal-title fallback.
        if best_blank is not None:
            break

    # Minimal direct fallback for provider-search misses. Explicit/anniversary
    # year hints get first chance; current-year and yearless slugs cover new
    # releases and legacy canonical pages without a five-year probe loop.
    direct_candidates = []
    seen_direct = set()
    for q in candidate_title_variants(title):
        base_slug = rt_slugify(q)
        if not base_slug:
            continue
        specs = []
        if hinted_year is not None:
            specs.append((f"{RT_BASE}/m/{base_slug}_{hinted_year}", hinted_year))
        else:
            specs.append((f"{RT_BASE}/m/{base_slug}_{current_year}", current_year))
            specs.append((f"{RT_BASE}/m/{base_slug}_{current_year - 1}", current_year - 1))
        specs.append((f"{RT_BASE}/m/{base_slug}", hinted_year))
        for url, expected_year in specs:
            key2 = url.casefold()
            if key2 in seen_direct:
                continue
            seen_direct.add(key2)
            direct_candidates.append((q, url, expected_year))

    for q, rt_url, expected_year in direct_candidates:
        # If a recent exact-title page already validated but had no score, use
        # its release year to protect a yearless canonical URL from falling
        # through to an older same-title film. This still allows the canonical
        # page for the same release to supply scores when an alias/stub did not.
        effective_year = expected_year
        if effective_year is None and best_blank_year is not None:
            effective_year = best_blank_year
        found = _fetch_validated(rt_url, q, effective_year)
        if found is not None:
            cache[cache_key] = found
            return found

    if best_blank is not None:
        cache[cache_key] = best_blank
        return best_blank
    cache[cache_key] = (None, None, None)
    return cache[cache_key]
'''

RT_LOOKUP_LOOP_OLD = 'movie_rows = []\nfor title in movie_titles:\n    rt_aud, rt_crit, rt_url = rt_get_scores(session, title, rt_cache, debug=DEBUG_RT)\n    imdb, imdb_runtime_min, imdb_url = imdb_rating_runtime_url(session, title, imdb_cache)\n\n    rank, prim_score, prim_src = pick_primary(rt_aud, imdb, rt_crit)\n\n    # Priority: AMC runtime first, IMDb runtime second\n    amc_rt = df_show[df_show["movie_title"] == title]["runtime_min"].dropna()\n    rt_min = int(amc_rt.iloc[0]) if not amc_rt.empty else imdb_runtime_min\n'
RT_LOOKUP_LOOP_NEW = 'movie_rows = []\nfor title in movie_titles:\n    # AMC\'s own runtime is a strong generic identity signal for reused movie\n    # titles/remakes. Feed it into RT resolution before scores are accepted.\n    amc_rt = df_show[df_show["movie_title"] == title]["runtime_min"].dropna()\n    amc_runtime_min = int(amc_rt.iloc[0]) if not amc_rt.empty else None\n\n    rt_aud, rt_crit, rt_url = rt_get_scores(\n        session, title, rt_cache, debug=DEBUG_RT, runtime_min=amc_runtime_min\n    )\n    imdb, imdb_runtime_min, imdb_url = imdb_rating_runtime_url(session, title, imdb_cache)\n\n    rank, prim_score, prim_src = pick_primary(rt_aud, imdb, rt_crit)\n\n    # Priority: AMC runtime first, IMDb runtime second\n    rt_min = amc_runtime_min if amc_runtime_min is not None else imdb_runtime_min\n'


AMC_BROWSER_CHILD_SCRIPT = r"""
import json
import re
import sys
import time
from datetime import datetime
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

AMC_BASE = "https://www.amctheatres.com"
QUEUE_RE = re.compile(r"document\.location\.href\s*=\s*decodeURIComponent\(\s*['\"]([^'\"]+)['\"]\s*\)", re.I)
MARKERS = (
    "the site requires javascript to be enabled",
    "queue.amctheatres.com",
    "global safety net",
    "enable-javascript.com",
    "access denied",
    "verify you are human",
    "checking your browser before accessing",
    "attention required",
    "cf-chl",
    "captcha",
    "bot protection",
)


def looks_like_interstitial(html_txt: str, final_url: str = "") -> bool:
    low = (html_txt or "").lower()
    final_low = (final_url or "").lower()
    return any(marker in low for marker in MARKERS) or "queue.amctheatres.com" in final_low


def extract_redirect(html_txt: str, current_url: str = AMC_BASE):
    if not html_txt:
        return None
    match = QUEUE_RE.search(html_txt)
    if not match:
        return None
    raw = match.group(1)
    try:
        decoded = unquote(raw)
    except Exception:
        decoded = raw
    return urljoin(current_url or AMC_BASE, decoded)


def query_date(url: str):
    try:
        values = parse_qs(urlparse(str(url or "")).query).get("date") or []
        return str(values[0]) if values else None
    except Exception:
        return None


def observed_schedule_dates(html_txt: str):
    # Return Pacific local dates explicitly represented by AMC showtime UTC fields.
    text = str(html_txt or "").replace('\\"', '"')
    found = set()
    pacific = ZoneInfo("America/Los_Angeles")
    for match in re.finditer(r'"showDateTimeUtc"\s*:\s*"([^"]+)"', text, re.I):
        raw = match.group(1).strip()
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                continue
            found.add(stamp.astimezone(pacific).date().isoformat())
        except Exception:
            continue
    return sorted(found)


def date_matches_request(requested_date: str | None, final_url: str, html_txt: str):
    if not requested_date:
        return True
    observed = observed_schedule_dates(html_txt)
    if observed:
        return requested_date in observed
    return query_date(final_url) == requested_date


def showtime_signature(page):
    # Return generic DOM/data counts that rise as AMC finishes rendering.
    try:
        movie_regions = page.locator('[aria-label^="Showtimes for "]').count()
    except Exception:
        movie_regions = 0
    try:
        showtime_links = page.locator('a[href*="/showtimes/"]').count()
    except Exception:
        showtime_links = 0
    try:
        html_txt = page.content()
    except Exception:
        html_txt = ""
    return (
        int(movie_regions),
        int(showtime_links),
        min(9999, html_txt.count('showtimeId')),
        min(9999, html_txt.count('Showtimes for')),
    )


base_url = sys.argv[1]
qs = sys.argv[2]
timeout_ms = int(sys.argv[3])
executable_path = sys.argv[4] or None
target_url = base_url + (("?" + qs) if qs else "")

with sync_playwright() as p:
    launch_kwargs = {
        "headless": True,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    }
    if executable_path:
        launch_kwargs["executable_path"] = executable_path
    browser = p.chromium.launch(**launch_kwargs)

    # Derive the user agent from installed Chromium instead of freezing a
    # browser version in source code. Strip only the headless marker.
    probe = browser.new_page()
    try:
        browser_ua = probe.evaluate("navigator.userAgent")
    finally:
        probe.close()
    browser_ua = str(browser_ua or "").replace("HeadlessChrome/", "Chrome/")

    context = browser.new_context(
        user_agent=browser_ua or None,
        locale="en-US",
        timezone_id="America/Los_Angeles",
        viewport={"width": 1440, "height": 2200},
        java_script_enabled=True,
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    context.add_init_script(
        '''
        Object.defineProperty(navigator, "webdriver", { get: () => undefined });
        window.chrome = window.chrome || { runtime: {} };
        Object.defineProperty(navigator, "languages", { get: () => ["en-US", "en"] });
        '''
    )
    # AMC's legacy cookietest is useful for the real site, but skip it for
    # local/synthetic diagnostics so the browser helper remains testable.
    if "amctheatres.com" in target_url.lower():
        context.add_cookies([{
            "name": "cookietest",
            "value": "1",
            "domain": "www.amctheatres.com",
            "path": "/",
        }])

    page = context.new_page()
    response = page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
    status = response.status if response is not None else 200
    page.wait_for_timeout(1000)
    html_txt = page.content()

    # Retain the queue-token behavior from the previous fetcher.
    for _ in range(3):
        if not looks_like_interstitial(html_txt, page.url):
            break
        redirect_url = extract_redirect(html_txt, page.url or target_url)
        try:
            if redirect_url:
                response = page.goto(redirect_url, wait_until="domcontentloaded", timeout=timeout_ms)
                status = response.status if response is not None else status
            else:
                response = page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
                status = response.status if response is not None else status
        except Exception:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=min(7000, timeout_ms))
        except Exception:
            pass
        page.wait_for_timeout(1500)
        html_txt = page.content()

    # Queue/interstitial redirects can drop AMC's future-date query parameter and
    # quietly land Chromium back on today's default schedule. Once the queue has
    # settled, restore the original requested URL exactly once when the rendered
    # page does not prove that it is showing the requested date. The outer
    # scraper still validates parsed showtime dates before accepting the snapshot.
    requested_date = query_date(target_url)
    date_restore_attempted = False
    date_before_restore = query_date(page.url)
    observed_before_restore = observed_schedule_dates(html_txt)
    if requested_date and not date_matches_request(requested_date, page.url, html_txt):
        date_restore_attempted = True
        try:
            response = page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
            status = response.status if response is not None else status
        except Exception:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=min(7000, timeout_ms))
        except Exception:
            pass
        page.wait_for_timeout(1500)
        html_txt = page.content()

        # If restoring the requested date re-enters AMC's queue, resolve that
        # queue but do not enter an unbounded date/queue navigation loop.
        for _ in range(2):
            if not looks_like_interstitial(html_txt, page.url):
                break
            redirect_url = extract_redirect(html_txt, page.url or target_url)
            try:
                if redirect_url:
                    response = page.goto(redirect_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    status = response.status if response is not None else status
                else:
                    response = page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
                    status = response.status if response is not None else status
            except Exception:
                pass
            try:
                page.wait_for_load_state("networkidle", timeout=min(7000, timeout_ms))
            except Exception:
                pass
            page.wait_for_timeout(1500)
            html_txt = page.content()

    # Wait for useful AMC content to stabilize. A page can briefly plateau at
    # one or two cards while later React requests are still resolving.
    start = time.monotonic()
    deadline = start + min(24.0, max(10.0, timeout_ms / 1000.0 * 0.60))
    first_useful_at = None
    previous = None
    stable_samples = 0

    while time.monotonic() < deadline:
        current_html = page.content()
        if looks_like_interstitial(current_html, page.url):
            break
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        page.wait_for_timeout(1200)
        signature = showtime_signature(page)
        useful = any(signature)
        now = time.monotonic()

        if useful and first_useful_at is None:
            first_useful_at = now
        if signature == previous and useful:
            stable_samples += 1
        else:
            stable_samples = 0
        previous = signature

        if (
            first_useful_at is not None
            and now - first_useful_at >= 5.0
            and stable_samples >= 3
        ):
            break

    final_html = page.content()
    observed_after_restore = observed_schedule_dates(final_html)
    payload = {
        "status": int(status or 200),
        "html": final_html,
        "url": page.url,
        "signature": showtime_signature(page),
        "requested_date": requested_date,
        "final_url_date": query_date(page.url),
        "observed_schedule_dates": observed_after_restore,
        "date_before_restore": date_before_restore,
        "observed_before_restore": observed_before_restore,
        "date_restore_attempted": bool(date_restore_attempted),
        "date_match": bool(date_matches_request(requested_date, page.url, final_html)),
    }
    context.close()
    browser.close()

print(json.dumps(payload))
"""

AMC_STATIC_FETCH_REPLACEMENT = r'''def fetch_amc_html(session: requests.Session, url: str, params: dict | None = None) -> Tuple[int, str]:
    """Return one static AMC response; the scraper owns browser recovery.

    The notebook used to call a browser fallback here and then call the browser
    again inside ``scrape_amc_showtimes_for_date``. During an AMC-wide block
    that doubled expensive Playwright work for every theatre/date. Keep this
    helper deliberately static-only so there is exactly one rendered-browser
    decision point, with a single HTTP attempt before it.
    """
    return fetch_html(session, url, params=params, tries=1)
'''


AMC_BROWSER_FETCH_REPLACEMENT_TEMPLATE = r'''def fetch_html_with_browser(url: str, params: dict | None = None, timeout_ms: int = 30000) -> Tuple[int, str]:
    """
    Fetch an AMC page in Chromium and wait for its client-rendered showtime DOM
    to settle before taking the HTML snapshot.

    AMC can hydrate the movie/showtime regions several seconds after the first
    document arrives. A fixed short sleep is race-prone on GitHub-hosted runners,
    so the child browser polls generic showtime signals until they stabilize.
    """
    import os as _amc_os
    import base64 as _amc_base64
    import subprocess as _amc_subprocess
    import sys as _amc_sys
    from urllib.parse import urlparse as _amc_urlparse

    full_url = requests.Request("GET", url, params=params).prepare().url
    query_string = _amc_urlparse(full_url).query
    browser_fetch_script = _amc_base64.b64decode("__BROWSER_FETCH_B64__").decode("utf-8")
    executable_path = _amc_os.environ.get("AMC_CHROMIUM_EXECUTABLE", "")

    proc = _amc_subprocess.run(
        [
            _amc_sys.executable,
            "-c",
            browser_fetch_script,
            url,
            query_string,
            str(timeout_ms),
            executable_path,
        ],
        capture_output=True,
        text=True,
        timeout=max(120, int(timeout_ms / 1000) * 5),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "browser subprocess failed").strip())

    try:
        payload = json.loads(proc.stdout)
    except Exception as e:
        raise RuntimeError(f"browser subprocess returned invalid JSON: {proc.stdout[:500]}") from e

    # Keep queue/date diagnostics in memory for the scraper to persist to the
    # Actions-only metrics file. Do not print them here: Papermill stdout becomes
    # notebook output and would otherwise leak diagnostics into the public HTML.
    final_url = str(payload.get("url") or "")
    parsed_final = _amc_urlparse(final_url)
    globals()["_AMC_LAST_BROWSER_META"] = {
        "requested_date": payload.get("requested_date"),
        "final_url_date": payload.get("final_url_date"),
        "final_host": parsed_final.netloc,
        "final_path": parsed_final.path,
        "observed_schedule_dates": list(payload.get("observed_schedule_dates") or []),
        "date_before_restore": payload.get("date_before_restore"),
        "observed_before_restore": list(payload.get("observed_before_restore") or []),
        "date_restore_attempted": bool(payload.get("date_restore_attempted")),
        "date_match": bool(payload.get("date_match")),
    }

    return int(payload.get("status") or 200), str(payload.get("html") or "")
'''

AMC_BROWSER_FETCH_REPLACEMENT = AMC_BROWSER_FETCH_REPLACEMENT_TEMPLATE.replace(
    "__BROWSER_FETCH_B64__",
    base64.b64encode(AMC_BROWSER_CHILD_SCRIPT.encode("utf-8")).decode("ascii"),
)


RT_DISPLAY_MARKER = "df_display = df_summary.copy()\n"
RT_DISPLAY_INSERT = """df_display = df_summary.copy()

def _rt_pair_display(row):
    def _one(value):
        if pd.isna(value):
            return ""
        try:
            return str(int(round(float(value))))
        except Exception:
            text = str(value).strip()
            return "" if text.lower() in {"none", "nan", "null", "n/a"} else text

    critic = _one(row.get("rt_critic"))
    audience = _one(row.get("rt_audience"))
    if not critic and not audience:
        return ""
    return f"{critic or '-'}/{audience or '-'}"

df_display["rt_c/a"] = df_display.apply(_rt_pair_display, axis=1)
"""

RT_DESIRED_OLD = """desired = [
    "movie_title",
    "runtime",
    "rt_critic",
"""
RT_DESIRED_NEW = """desired = [
    "movie_title",
    "runtime",
    "rt_c/a",
    "rt_critic",
"""

AMC_SHOWTIME_PARSE_REPLACEMENT = r'''def extract_showtimes_from_json_scripts(html_txt: str, theatre_name: str, d: date) -> List[dict]:
    """
    Extract AMC showtimes from structured JSON and the current escaped
    React/Next flight payload.

    Correctness rule: a showtime is only associated with a movie when the
    serialized record has the current AMC flat-object shape. We intentionally
    do NOT carry one title forward across an arbitrary run of showtimeId
    values; if local pairing is uncertain, the rendered DOM fallback is safer.
    """
    soup = BeautifulSoup(html_txt or "", "html.parser")
    out: List[dict] = []
    seen_ids = set()
    seen_fallback = set()
    wanted = d.isoformat()

    def add_row(row: dict) -> None:
        if not row or row.get("show_date") != wanted:
            return
        title = _normalize_space(str(row.get("movie_title") or ""))
        show_time = _normalize_space(str(row.get("show_time") or "")).lower()
        if not title or not looks_like_title_text(title) or not TIME_RE.search(show_time):
            return
        row = dict(row)
        row["movie_title"] = title
        row["show_time"] = show_time
        sid = _normalize_space(str(row.get("showtime_id") or ""))
        if sid:
            if sid in seen_ids:
                return
            seen_ids.add(sid)
        else:
            key = (
                title.casefold(),
                str(row.get("theatre") or "").casefold(),
                wanted,
                show_time,
                str(row.get("format_label") or "").strip().casefold(),
            )
            if key in seen_fallback:
                return
            seen_fallback.add(key)
        out.append(row)

    # Preserve the notebook's clean-JSON parser first.
    for script in soup.find_all("script"):
        txt = script.string or script.get_text(" ", strip=False) or ""
        if not txt:
            continue
        low = txt.lower()
        if "showtime" not in low and "datetime" not in low and "startdate" not in low:
            continue

        candidates: List[object] = []
        stype = (script.get("type") or "").lower()
        if stype == "application/ld+json":
            try:
                candidates.append(json.loads(txt))
            except Exception:
                pass
        if "__NEXT_DATA__" in txt or '"props"' in txt or '"pageProps"' in txt:
            try:
                candidates.append(json.loads(txt))
            except Exception:
                pass
        if not candidates and (txt.lstrip().startswith("{") or txt.lstrip().startswith("[")):
            try:
                candidates.append(json.loads(txt))
            except Exception:
                pass

        for cand in candidates:
            for row in _iter_json_showtime_rows(cand, d, theatre_name):
                add_row(row)

    # Decode the escaped React/Next quote/unicode layer.
    text = html_txt or ""
    for _ in range(2):
        newer = text.replace(r'\\"', '"').replace(r'\"', '"')
        if newer == text:
            break
        text = newer

    def _decode_text(value: str) -> str:
        def repl(match):
            try:
                return chr(int(match.group(1), 16))
            except Exception:
                return match.group(0)
        value = re.sub(r"\\u([0-9a-fA-F]{4})", repl, value or "")
        try:
            return html_lib.unescape(value)
        except Exception:
            return value

    anchors = []
    for m in re.finditer(r'aria-label"\s*:\s*"Showtimes for ([^"]+)"', text, re.I):
        title = _normalize_space(_decode_text(m.group(1)))
        if title and looks_like_title_text(title):
            anchors.append((m.start(), title))

    try:
        from zoneinfo import ZoneInfo
        pacific = ZoneInfo("America/Los_Angeles")
    except Exception:
        pacific = None

    # Keep every matched record bounded to one flat showtime object. The
    # [^{}]* sections are intentional: fields may not leak into the next
    # showtime record.
    flight_rx = re.compile(
        r'"showtimeId"\s*:\s*(?:"(\d+)"|(\d+))'
        r'([^{}]{0,5000}?)'
        r'"status"\s*:\s*"([^"]+)"'
        r'([^{}]{0,5000}?)'
        r'"showDateTimeUtc"\s*:\s*"([^"]+)"'
        r'([^{}]{0,5000}?)'
        r'"display"\s*:\s*\{'
        r'([^{}]{0,1200}?)'
        r'\}',
        re.I | re.S,
    )

    for m in flight_rx.finditer(text):
        sid = m.group(1) or m.group(2) or ""
        status = _decode_text(m.group(4))
        if re.search(r"cancel", status, re.I):
            continue

        movie = None
        anchor_pos = None
        for pos, name in anchors:
            if pos < m.start():
                movie = name
                anchor_pos = pos
            else:
                break

        # A very distant preceding title is not evidence that this screening
        # belongs to that movie. Skip ambiguous SSR data and let Playwright's
        # rendered aria-labelled DOM supply the authoritative pairing.
        if not movie or anchor_pos is None or (m.start() - anchor_pos) > 12000:
            continue

        utc_txt = _decode_text(m.group(6)).strip()
        local_dt = None
        try:
            local_dt = datetime.fromisoformat(utc_txt.replace("Z", "+00:00"))
            if local_dt.tzinfo is not None and pacific is not None:
                local_dt = local_dt.astimezone(pacific)
            if local_dt.date().isoformat() != wanted:
                continue
        except Exception:
            local_dt = None

        display_text = m.group(8)
        time_m = re.search(r'"time"\s*:\s*"([^"]+)"', display_text, re.I)
        ampm_m = re.search(r'"amPm"\s*:\s*"([^"]+)"', display_text, re.I)
        time_txt = ""
        if time_m:
            time_txt = _decode_text(time_m.group(1))
            if ampm_m:
                time_txt = f"{time_txt} {_decode_text(ampm_m.group(1))}"
        elif local_dt is not None:
            try:
                time_txt = local_dt.strftime("%-I:%M %p")
            except Exception:
                time_txt = local_dt.strftime("%I:%M %p").lstrip("0")

        time_txt = _normalize_space(time_txt).lower()
        if not TIME_RE.search(time_txt):
            continue

        # AMC's canonical showtime attribute code for an A-List exclusion is
        # NOALIST. The attributes array can be serialized after display, so
        # inspect only this showtime's bounded tail (never the next showtime or
        # movie region) rather than hard-coding False as the old fallback did.
        record_end = min(len(text), m.end() + 8000)
        next_sid = re.search(r'"showtimeId"\s*:', text[m.end():], re.I)
        if next_sid:
            record_end = min(record_end, m.end() + next_sid.start())
        next_anchor = next((pos for pos, _ in anchors if pos > m.start()), None)
        if next_anchor is not None:
            record_end = min(record_end, next_anchor)
        record_text = text[m.start():record_end]
        a_list_excluded = bool(re.search(
            r'\bNOALIST\b|Excluded\s+from\s+A-List',
            _decode_text(record_text),
            re.I,
        ))

        add_row({
            "movie_title": movie,
            "theatre": theatre_name,
            "show_date": wanted,
            "show_time": time_txt,
            "format_label": None,
            "runtime_min": None,
            "a_list_excluded": a_list_excluded,
            "showtime_id": sid,
        })

    return out
'''

AMC_SCRAPE_REPLACEMENT = r'''def scrape_amc_showtimes_for_date(session: requests.Session, theatre_name: str, showtimes_url: str, d: date) -> List[dict]:
    """
    Merge AMC sources while deduplicating on AMC's numeric showtime ID.

    v18 also keeps a small fresh cache in build/ for the lifetime of one
    workflow run. GitHub retries launch Papermill again in the same workspace,
    so a theatre/date combination that succeeded on attempt 1 is retained
    rather than thrown away when another combination fails.
    """
    from pathlib import Path as _AMCPath

    wanted = d.isoformat()
    out: List[dict] = []
    id_to_index = {}
    fallback_to_index = {}
    # A second Papermill attempt can load rows saved by an earlier attempt.
    # Track semantic showtime slots parsed from THIS attempt's live browser DOM
    # separately, so preserved rows are never mislabeled as newly retrieved.
    _fresh_row_slots = set()

    # Never infer expected inventory from screen count. Instead, retain two
    # independent things across this workflow run: rows we successfully parsed
    # and evidence that AMC's own response exposed an actionable movie/showtime.
    # This lets a genuine one-movie schedule pass while a page exposing 12
    # movies but yielding only 2 parsed movies is retried and ultimately rejected.
    _cache_dir = _AMCPath("build/amc_combo_cache")
    _cache_dir.mkdir(parents=True, exist_ok=True)
    _cache_slug = re.sub(r"[^a-z0-9]+", "-", theatre_name.lower()).strip("-")
    _cache_path = _cache_dir / f"{_cache_slug}-{wanted}.json"
    _evidence_path = _cache_dir / f"{_cache_slug}-{wanted}.evidence.json"
    _status_dir = _AMCPath("build/amc_combo_status")
    _status_dir.mkdir(parents=True, exist_ok=True)
    _status_path = _status_dir / f"{_cache_slug}-{wanted}.json"

    # Completeness evidence is snapshot-based. Do not union AMC's raw
    # showtime IDs across requests: AMC can replace/duplicate internal IDs for
    # the same visible screening between SSR, browser hydration, and retries.
    # Instead keep each response as an independent snapshot and compare the
    # parser with the richest *actionable* snapshot using stable semantic keys
    # (movie identity + local visible clock time). Movie headings/cards that are
    # not tied to a trusted rendered time are diagnostic only: AMC sometimes
    # emits placeholder, duplicate, or not-yet-bookable title shells.
    _source_snapshots = []
    _source_observations = 0

    if _evidence_path.exists():
        try:
            _saved_evidence = json.loads(_evidence_path.read_text(encoding="utf-8"))
            if isinstance(_saved_evidence, dict):
                _source_observations = int(_saved_evidence.get("observations") or 0)
                _evidence_version = int(_saved_evidence.get("evidence_version") or 0)
                for _snap in (_saved_evidence.get("snapshots") or []):
                    if not isinstance(_snap, dict):
                        continue
                    # v27 deliberately does not trust the old `slots` field.
                    # v25/v26 mixed directly rendered DOM links with serialized
                    # React payload records, and AMC can duplicate/reshape those
                    # serialized records. Only v27+ `trusted_slots` are allowed
                    # to participate in a fatal completeness decision.
                    trusted_slots = (
                        {str(x) for x in (_snap.get("trusted_slots") or []) if x}
                        if _evidence_version >= 27 else set()
                    )
                    serialized_slots = (
                        {str(x) for x in (_snap.get("serialized_slots") or []) if x}
                        if _evidence_version >= 27 else set()
                    )
                    _source_snapshots.append({
                        "source": str(_snap.get("source") or "saved"),
                        "titles": {str(x) for x in (_snap.get("titles") or []) if x},
                        "trusted_slots": trusted_slots,
                        "serialized_slots": serialized_slots,
                        "showtime_ids": {str(x) for x in (_snap.get("showtime_ids") or []) if x},
                    })
                # Older evidence is useful only for movie identity. Discard all
                # old time/ID completeness data rather than carrying a false gap
                # into a newer Papermill retry.
                if not _source_snapshots and (_saved_evidence.get("titles") or []):
                    _source_snapshots.append({
                        "source": "legacy-pre-v27",
                        "titles": {str(x) for x in (_saved_evidence.get("titles") or []) if x},
                        "trusted_slots": set(),
                        "serialized_slots": set(),
                        "showtime_ids": set(),
                    })
        except Exception as e:
            print(f"[WARN] Ignoring unreadable AMC source-evidence cache for {theatre_name} {wanted}: {e}")

    def _merge_format(old_value, new_value):
        parts = []
        seen_parts = set()
        for value in (old_value, new_value):
            for part in re.split(r"\s*;\s*", str(value or "").strip()):
                part = _normalize_space(part)
                key = part.casefold()
                if part and key not in seen_parts:
                    seen_parts.add(key)
                    parts.append(part)
        return "; ".join(parts) if parts else None

    def add_row(row: dict, source_rank: int = 0) -> None:
        if not row or row.get("show_date") != wanted:
            return
        title = _normalize_space(str(row.get("movie_title") or ""))
        show_time = _normalize_space(str(row.get("show_time") or "")).lower()
        if not title or not TIME_RE.search(show_time):
            return

        row = dict(row)
        row["movie_title"] = title
        row["show_time"] = show_time
        row["_source_rank"] = int(source_rank)
        # Ranks 2/3 originate from the live AMC page; rank 1 is runner-local
        # cache. Use identity + visible time, not AMC's unstable numeric IDs,
        # to account for a live slot even when it merges with a cached row.
        if source_rank >= 2:
            _fresh_row_slots.add((title.casefold(), show_time.casefold()))
        sid = _normalize_space(str(row.get("showtime_id") or ""))

        if sid:
            idx = id_to_index.get(sid)
            if idx is not None:
                old = out[idx]
                old_rank = int(old.get("_source_rank") or 0)
                if source_rank >= old_rank:
                    old["movie_title"] = title
                    old["show_time"] = show_time
                    old["theatre"] = row.get("theatre") or old.get("theatre") or theatre_name
                    old["show_date"] = wanted
                    old["_source_rank"] = source_rank
                old["format_label"] = _merge_format(old.get("format_label"), row.get("format_label"))
                if old.get("runtime_min") is None and row.get("runtime_min") is not None:
                    old["runtime_min"] = row.get("runtime_min")
                old["a_list_excluded"] = bool(old.get("a_list_excluded")) or bool(row.get("a_list_excluded"))
                return

        fallback_key = (
            title.casefold(),
            str(row.get("theatre") or theatre_name).casefold(),
            wanted,
            show_time,
            str(row.get("format_label") or "").strip().casefold(),
        )

        if not sid:
            same_time_idx = next(
                (
                    i for i, existing in enumerate(out)
                    if existing.get("showtime_id")
                    and str(existing.get("movie_title") or "").casefold() == title.casefold()
                    and str(existing.get("theatre") or theatre_name).casefold()
                       == str(row.get("theatre") or theatre_name).casefold()
                    and existing.get("show_date") == wanted
                    and str(existing.get("show_time") or "").casefold() == show_time.casefold()
                ),
                None,
            )
            if same_time_idx is not None:
                out[same_time_idx]["format_label"] = _merge_format(
                    out[same_time_idx].get("format_label"), row.get("format_label")
                )
                return

        idx = fallback_to_index.get(fallback_key)
        if idx is not None:
            old = out[idx]
            old_sid = _normalize_space(str(old.get("showtime_id") or ""))
            if sid and old_sid and old_sid != sid:
                idx = None
            else:
                old["format_label"] = _merge_format(old.get("format_label"), row.get("format_label"))
                if old.get("runtime_min") is None and row.get("runtime_min") is not None:
                    old["runtime_min"] = row.get("runtime_min")
                old["a_list_excluded"] = bool(old.get("a_list_excluded")) or bool(row.get("a_list_excluded"))
                if sid and not old_sid:
                    old["showtime_id"] = sid
                    id_to_index[sid] = idx
                return

        idx = len(out)
        out.append(row)
        if fallback_key not in fallback_to_index or not sid:
            fallback_to_index[fallback_key] = idx
        if sid:
            id_to_index[sid] = idx

    def _clean_rows() -> List[dict]:
        cleaned = []
        for row in out:
            row = dict(row)
            row.pop("_source_rank", None)
            if row.get("show_date") == wanted:
                cleaned.append(row)
        return cleaned

    def _unique_title_count() -> int:
        return len({str(r.get("movie_title") or "").casefold() for r in out if r.get("movie_title")})

    def _title_evidence_key(value: str) -> str:
        # Compare identity rather than punctuation/HTML-entity spelling.
        return re.sub(r"[^a-z0-9]+", " ", _normalize_space(str(value or "")).casefold()).strip()

    def _clock_evidence_key(value: str) -> str:
        """Normalize a visible 12-hour clock time to a stable semantic key."""
        text = _normalize_space(str(value or "")).lower()
        m = re.search(r"\b(1[0-2]|0?[1-9]):([0-5]\d)\s*([ap])\.?m\.?\b", text, re.I)
        if not m:
            return ""
        return f"{int(m.group(1))}:{m.group(2)} {m.group(3).lower()}m"

    def _slot_evidence_key(title_key: str, clock_key: str) -> str:
        if not title_key or not clock_key:
            return ""
        return f"{title_key}\t{clock_key}"

    def _decode_amc_source_text(value: str) -> str:
        def _unicode_repl(match):
            try:
                return chr(int(match.group(1), 16))
            except Exception:
                return match.group(0)
        value = re.sub(r"\\u([0-9a-fA-F]{4})", _unicode_repl, value or "")
        try:
            return html_lib.unescape(value)
        except Exception:
            return value

    def _inspect_source_evidence(page_html: str) -> Tuple[set, set, set, set]:
        """
        Inspect one AMC response independently of the production row parser.

        Hard completeness evidence:
          * distinct movie identities exposed by AMC; and
          * movie/time links that are directly rendered as actionable DOM
            anchors under an aria-labelled movie region.

        Serialized React/Next showtime records are still inspected, but their
        movie/time slots are diagnostic only. AMC can include duplicated,
        superseded, hidden-format, or hydration records in that payload, so a
        serialized-slot surplus must never kill an otherwise complete run.
        """
        titles = set()
        trusted_slots = set()
        serialized_slots = set()
        showtime_ids = set()
        if not page_html:
            return titles, trusted_slots, serialized_slots, showtime_ids

        # Highest-confidence time evidence: an actual rendered link that the
        # user could click, scoped beneath one explicit "Showtimes for <movie>"
        # region. The production DOM parser walks this exact same structure.
        try:
            soup = BeautifulSoup(page_html, "html.parser")
            for region in soup.find_all(attrs={"aria-label": re.compile(r"^Showtimes for\s+", re.I)}):
                aria = str(region.get("aria-label") or "")
                title = _normalize_space(re.sub(r"^Showtimes for\s+", "", aria, flags=re.I))
                title_key = _title_evidence_key(title)
                local_slots = set()
                local_ids = set()
                if not title_key or not looks_like_title_text(title):
                    continue
                # The region itself is strong movie-card evidence even if AMC
                # has not yet hydrated its time anchors.
                titles.add(title_key)
                for a in region.find_all("a", href=True):
                    sid_m = re.search(r"/showtimes/(\d+)", str(a.get("href") or ""))
                    if not sid_m:
                        continue
                    visible = _normalize_space(a.get_text(" ", strip=True))
                    labelled = _normalize_space(str(a.get("aria-label") or ""))
                    clock = _clock_evidence_key(visible) or _clock_evidence_key(labelled)
                    if not clock:
                        continue
                    local_ids.add(sid_m.group(1))
                    local_slots.add(_slot_evidence_key(title_key, clock))
                trusted_slots.update(local_slots)
                showtime_ids.update(local_ids)
        except Exception:
            pass

        # Lower-confidence serialized evidence. It remains valuable for finding
        # movie cards that the browser did not render, but time-slot counts are
        # diagnostic only because one visible screening can be represented by
        # multiple internal payload records during SSR/hydration.
        text = page_html or ""
        for _ in range(2):
            newer = text.replace(r'\\"', '"').replace(r'\"', '"')
            if newer == text:
                break
            text = newer

        anchors = []
        for match in re.finditer(r'aria-label"\s*:\s*"Showtimes for ([^"]+)"', text, re.I):
            title = _normalize_space(_decode_amc_source_text(match.group(1)))
            key = _title_evidence_key(title)
            if key and looks_like_title_text(title):
                anchors.append((match.start(), title, key))
                titles.add(key)

        if anchors:
            try:
                from zoneinfo import ZoneInfo as _AMCZoneInfo
                _pacific = _AMCZoneInfo("America/Los_Angeles")
            except Exception:
                _pacific = None

            for idx, (start, _title, title_key) in enumerate(anchors):
                end = anchors[idx + 1][0] if idx + 1 < len(anchors) else min(len(text), start + 60000)
                chunk = text[start:end]
                sid_matches = list(re.finditer(
                    r'"showtimeId"\s*:\s*(?:"(\d+)"|(\d+))', chunk, re.I
                ))
                utc_matches = list(re.finditer(
                    r'"showDateTimeUtc"\s*:\s*"([^"]+)"', chunk, re.I
                ))
                if not sid_matches or not utc_matches:
                    continue

                local_slots = set()
                local_ids = set()
                for sid_match in sid_matches:
                    sid = sid_match.group(1) or sid_match.group(2) or ""
                    if not sid:
                        continue
                    nearest_utc = min(
                        utc_matches,
                        key=lambda utc_match: abs(utc_match.start() - sid_match.start()),
                    )
                    if abs(nearest_utc.start() - sid_match.start()) > 5000:
                        continue
                    utc_txt = nearest_utc.group(1)
                    try:
                        local_dt = datetime.fromisoformat(
                            _decode_amc_source_text(utc_txt).replace("Z", "+00:00")
                        )
                        if local_dt.tzinfo is not None and _pacific is not None:
                            local_dt = local_dt.astimezone(_pacific)
                        if local_dt.date().isoformat() != wanted:
                            continue
                        try:
                            clock_txt = local_dt.strftime("%-I:%M %p")
                        except Exception:
                            clock_txt = local_dt.strftime("%I:%M %p").lstrip("0")
                        clock = _clock_evidence_key(clock_txt)
                        if not clock:
                            continue
                        local_ids.add(sid)
                        local_slots.add(_slot_evidence_key(title_key, clock))
                    except Exception:
                        continue
                serialized_slots.update(local_slots)
                showtime_ids.update(local_ids)

        return titles, trusted_slots, serialized_slots, showtime_ids

    def _snapshot_to_json(snapshot: dict) -> dict:
        return {
            "source": str(snapshot.get("source") or "source"),
            "titles": sorted(snapshot.get("titles") or []),
            "trusted_slots": sorted(snapshot.get("trusted_slots") or []),
            "serialized_slots": sorted(snapshot.get("serialized_slots") or []),
            "showtime_ids": sorted(snapshot.get("showtime_ids") or []),
        }

    def _save_source_evidence() -> None:
        try:
            # Bound runner-local diagnostics while preserving the richest
            # observations. Sorting by richness keeps useful evidence if AMC is
            # noisy over many retries.
            ranked = sorted(
                _source_snapshots,
                key=lambda snap: (
                    len(snap.get("titles") or []),
                    len(snap.get("trusted_slots") or []),
                    len(snap.get("serialized_slots") or []),
                ),
                reverse=True,
            )[:12]
            _evidence_path.write_text(json.dumps({
                "evidence_version": 30,
                "observations": int(_source_observations),
                "snapshots": [_snapshot_to_json(snap) for snap in ranked],
            }, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[WARN] Could not save AMC source evidence for {theatre_name} {wanted}: {e}")

    def _record_source_evidence(page_html: str, source_label: str) -> None:
        nonlocal _source_observations
        titles, trusted_slots, serialized_slots, showtime_ids = _inspect_source_evidence(page_html)
        if not titles and not trusted_slots and not serialized_slots and not showtime_ids:
            return
        _source_observations += 1
        snapshot = {
            "source": source_label,
            "titles": set(titles),
            "trusted_slots": set(trusted_slots),
            "serialized_slots": set(serialized_slots),
            "showtime_ids": set(showtime_ids),
        }
        _source_snapshots.append(snapshot)
        _save_source_evidence()
        print(
            f"[INFO] AMC source evidence {theatre_name} {wanted} {source_label}: "
            f"{len(titles)} movie(s), {len(trusted_slots)} trusted rendered slot(s), "
            f"{len(serialized_slots)} serialized slot(s) diagnostic, "
            f"{len(showtime_ids)} raw showtime id(s)"
        )

    def _trusted_title_keys(snapshot: dict) -> set:
        """Return movie identities backed by a trusted rendered showtime.

        AMC sometimes emits aria-labelled movie shells for placeholders,
        responsive duplicates, or not-yet-bookable cards. Those headings are
        useful diagnostics, but they are not proof that the row parser missed
        a real screening. A title becomes hard completeness evidence only when
        the same source snapshot exposes a directly rendered clickable time.
        """
        keys = set()
        for slot in (snapshot.get("trusted_slots") or []):
            text = str(slot or "")
            if "\t" not in text:
                continue
            title_key, _clock = text.split("\t", 1)
            if title_key:
                keys.add(title_key)
        return keys

    def _best_source_snapshot() -> dict:
        if not _source_snapshots:
            return {
                "source": "none", "titles": set(), "trusted_slots": set(),
                "serialized_slots": set(), "showtime_ids": set()
            }
        # Rank by actionable rendered coverage first. Raw title shells and
        # serialized payload records are secondary diagnostics and must not
        # outvote a snapshot whose clickable schedule is fully represented.
        # Crucially, do not union unrelated snapshots together.
        return max(
            _source_snapshots,
            key=lambda snap: (
                len(_trusted_title_keys(snap)),
                len(snap.get("trusted_slots") or []),
                len(snap.get("titles") or []),
                len(snap.get("serialized_slots") or []),
            ),
        )

    def _coverage_state() -> dict:
        parsed_title_keys = {
            _title_evidence_key(r.get("movie_title")) for r in out if _title_evidence_key(r.get("movie_title"))
        }
        parsed_slots = set()
        parsed_ids = set()
        for row in out:
            title_key = _title_evidence_key(row.get("movie_title"))
            clock_key = _clock_evidence_key(row.get("show_time"))
            slot = _slot_evidence_key(title_key, clock_key)
            if slot:
                parsed_slots.add(slot)
            sid = _normalize_space(str(row.get("showtime_id") or ""))
            if sid:
                parsed_ids.add(sid)

        best = _best_source_snapshot()
        detected_titles = set(best.get("titles") or [])
        # Only directly rendered DOM slots are trusted enough to be fatal. A
        # hard source-title set is derived from those slots rather than from
        # every aria-labelled/serialized title shell AMC happens to emit. This
        # prevents a placeholder card with no bookable time from creating a
        # permanent false "missing movie" failure.
        source_slots = set(best.get("trusted_slots") or [])
        source_titles = _trusted_title_keys(best)
        serialized_slots = set(best.get("serialized_slots") or [])
        source_ids = set(best.get("showtime_ids") or [])
        diagnostic_only_titles = sorted(detected_titles - source_titles)
        missing_titles = sorted(source_titles - parsed_title_keys)
        missing_slots = sorted(source_slots - parsed_slots)
        unmatched_ids = sorted(source_ids - parsed_ids)
        return {
            "source_snapshot": str(best.get("source") or "source"),
            "source_titles": len(source_titles),
            "source_detected_titles": len(detected_titles),
            "source_diagnostic_only_titles": diagnostic_only_titles,
            "source_showtime_slots": len(source_slots),
            "source_serialized_showtime_slots": len(serialized_slots),
            "source_showtime_ids": len(source_ids),
            "parsed_titles": len(parsed_title_keys),
            "parsed_showtime_slots": len(parsed_slots),
            "parsed_showtime_ids": len(parsed_ids),
            "missing_titles": missing_titles,
            "missing_showtime_slots": missing_slots,
            # Diagnostic only. Raw ID mismatch never decides completeness.
            "unmatched_source_showtime_ids": unmatched_ids,
            "source_samples": int(_source_observations),
            "complete": not missing_titles and not missing_slots,
        }

    def _write_combo_status(complete: bool, reason: str = "", fallback: bool = False) -> None:
        state = _coverage_state()
        state.update({
            "complete": bool(complete),
            "reason": str(reason or ""),
            "fallback": bool(fallback),
            "theatre": theatre_name,
            "date": wanted,
        })
        try:
            _status_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[WARN] Could not save AMC combo status for {theatre_name} {wanted}: {e}")

    def _save_cache() -> None:
        rows = _clean_rows()
        if not rows:
            return
        try:
            _cache_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[WARN] Could not save AMC in-run cache for {theatre_name} {wanted}: {e}")

    # Retain successes and partial discoveries across the workflow's Papermill
    # retries. The build directory is runner-local and is not committed, so this
    # never falls back to an older day's report.
    if _cache_path.exists():
        try:
            cached = json.loads(_cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                for row in cached:
                    if isinstance(row, dict):
                        add_row(row, source_rank=1)
                if out:
                    print(
                        f"[INFO] Loaded in-run AMC cache for {theatre_name} {wanted}: "
                        f"{_unique_title_count()} movie(s); rechecking live/browser DOM"
                    )
        except Exception as e:
            print(f"[WARN] Ignoring unreadable AMC in-run cache for {theatre_name} {wanted}: {e}")

    def _a_list_excluded_for_showtime(tag, movie_region=None) -> bool:
        # Retain the notebook's legacy local detector first. Then walk outward
        # only to the nearest containing block that explicitly carries AMC's
        # canonical exclusion marker. Do not use the whole movie region as a
        # fallback because one format can be excluded while another format for
        # the same title remains A-List eligible.
        try:
            if is_a_list_excluded_near_tag(tag):
                return True
        except Exception:
            pass

        marker_rx = re.compile(r'\bNOALIST\b|Excluded\s+from\s+A-List', re.I)
        node = tag
        hops = 0
        while node is not None and node is not movie_region and hops < 8:
            hops += 1
            try:
                raw = str(node)
                txt = _normalize_space(node.get_text(" ", strip=True))
                if marker_rx.search(raw) or marker_rx.search(txt):
                    return True
            except Exception:
                pass
            node = getattr(node, "parent", None)
        return False

    def merge_html(page_html: str, *, rendered: bool = False, source_label: str = "source") -> None:
        if not page_html:
            return

        _record_source_evidence(page_html, source_label)

        for row in extract_showtimes_from_json_scripts(page_html, theatre_name, d):
            add_row(row, source_rank=1)

        soup = BeautifulSoup(page_html, "html.parser")
        aria_count = 0
        for region in soup.find_all(attrs={"aria-label": re.compile(r"^Showtimes for\s+", re.I)}):
            aria = str(region.get("aria-label") or "")
            title = _normalize_space(re.sub(r"^Showtimes for\s+", "", aria, flags=re.I))
            if not title or not looks_like_title_text(title):
                continue

            region_text = _normalize_space(region.get_text(" ", strip=True))
            runtime_min = None
            rt_m = AMC_RUNTIME_RE.search(region_text)
            if rt_m:
                runtime_min = (
                    int(rt_m.group(1)) * 60 + int(rt_m.group(2))
                    if rt_m.group(1)
                    else int(rt_m.group(3))
                )

            for a in region.find_all("a", href=True):
                href = str(a.get("href") or "")
                sid_m = re.search(r"/showtimes/(\d+)", href)
                if not sid_m:
                    continue
                txt = _normalize_space(a.get_text(" ", strip=True))
                tm = TIME_RE.search(txt)
                if not tm:
                    tm = TIME_RE.search(_normalize_space(str(a.get("aria-label") or "")))
                if not tm:
                    continue
                aria_count += 1
                add_row({
                    "movie_title": title,
                    "theatre": theatre_name,
                    "show_date": wanted,
                    "show_time": tm.group(1).lower(),
                    "format_label": _extract_local_format_near_tag(a),
                    "runtime_min": runtime_min,
                    "a_list_excluded": _a_list_excluded_for_showtime(a, region),
                    "showtime_id": sid_m.group(1),
                }, source_rank=3 if rendered else 2)

        if aria_count == 0:
            movie_blocks = _collect_movie_blocks(soup)
            for idx, (block, title) in enumerate(movie_blocks):
                stop_tag = movie_blocks[idx + 1][0] if idx + 1 < len(movie_blocks) else None
                current_runtime = None
                for el in _iter_block_tags(block, stop_tag):
                    txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
                    rt_m = AMC_RUNTIME_RE.search(txt)
                    if rt_m and current_runtime is None:
                        current_runtime = (
                            int(rt_m.group(1)) * 60 + int(rt_m.group(2))
                            if rt_m.group(1)
                            else int(rt_m.group(3))
                        )
                    if _likely_showtime_tag(el, txt):
                        time_m = TIME_RE.search(txt)
                        if time_m:
                            add_row({
                                "movie_title": title,
                                "theatre": theatre_name,
                                "show_date": wanted,
                                "show_time": time_m.group(1).lower(),
                                "format_label": _extract_local_format_near_tag(el),
                                "runtime_min": current_runtime,
                                "a_list_excluded": is_a_list_excluded_near_tag(el),
                            }, source_rank=0)

    def _previous_report_rows() -> List[dict]:
        """
        Recover one theatre/date only from a recent successful checked-in
        report, and only when that report contains the exact requested date.

        This is a last-good fallback for AMC/GitHub-runner blocking, not a
        historical-data substitute. The report must be recent (<= 36 hours),
        same-weekend, and healthy enough to satisfy this theatre's normal
        sparse threshold. Existing per-showtime A-List exclusion markers are
        preserved from either structured JSON or the visible table.
        """
        report_path = _AMCPath("docs/index.html")
        stamp_path = _AMCPath("docs/last_run_utc.txt")
        if not report_path.exists() or not stamp_path.exists():
            return []

        try:
            stamp_txt = stamp_path.read_text(encoding="utf-8").strip()
            stamp_dt = datetime.strptime(stamp_txt, "%Y-%m-%dT%H:%M:%SZ")
            age_hours = (datetime.utcnow() - stamp_dt).total_seconds() / 3600.0
            if age_hours < -1 or age_hours > 36:
                return []
        except Exception:
            return []

        try:
            previous_html = report_path.read_text(encoding="utf-8", errors="ignore")
            previous_soup = BeautifulSoup(previous_html, "html.parser")
            data_script = previous_soup.find("script", id="showtimes-data")
            if data_script is None:
                return []
            payload = json.loads(data_script.string or data_script.get_text() or "{}")
            payload_rows = payload.get("showtimes") or []
            if not isinstance(payload_rows, list):
                return []
        except Exception:
            return []

        def _norm_theatre(value: str) -> str:
            value = str(value or "").replace("@", " at ").casefold()
            return re.sub(r"[^a-z0-9]+", " ", value).strip()

        wanted_theatre = _norm_theatre(theatre_name)

        # Older reports did not serialize the exclusion flag into
        # showtimes-data. Reconstruct those flags from the visible table's
        # exact time markers so this fallback never loses the ⛔ behavior.
        excluded_keys = set()
        for tr in previous_soup.select("tr[data-movie-id]"):
            title_tag = tr.select_one(".movie-cell-title")
            title = _normalize_space(title_tag.get_text(" ", strip=True)) if title_tag else ""
            if not title:
                continue
            candidate_cells = tr.find_all(["td", "th"], recursive=False)
            show_cell = None
            for cell in candidate_cells:
                cell_text = cell.get_text("\n", strip=True)
                if "AMC " in cell_text and wanted in cell_text:
                    show_cell = cell
                    break
            if show_cell is None:
                continue

            current_theatre = None
            raw_blob = show_cell.get_text("\n", strip=True).replace("•", "\n•")
            for line in [x.strip() for x in raw_blob.splitlines() if x.strip()]:
                if line.casefold().startswith("amc "):
                    current_theatre = line
                    continue
                dm = re.match(r"^[•\-\*]?\s*(\d{4}-\d{2}-\d{2})\s*:\s*(.+)$", line)
                if not dm or not current_theatre or dm.group(1) != wanted:
                    continue
                if _norm_theatre(current_theatre) != wanted_theatre:
                    continue
                for part in [p.strip() for p in dm.group(2).split(",") if p.strip()]:
                    tm = TIME_RE.search(part)
                    if tm and "⛔" in part:
                        excluded_keys.add((
                            title.casefold(),
                            wanted_theatre,
                            wanted,
                            _normalize_space(tm.group(1)).casefold(),
                        ))

        recovered = []
        for item in payload_rows:
            if not isinstance(item, dict):
                continue
            if str(item.get("date") or "") != wanted:
                continue
            item_theatre = _norm_theatre(item.get("theater") or item.get("theatre") or "")
            if item_theatre != wanted_theatre:
                continue
            title = _normalize_space(str(item.get("movie") or item.get("movie_title") or ""))
            start = _normalize_space(str(item.get("start") or item.get("show_time") or "")).casefold()
            if not title or not TIME_RE.search(start):
                continue
            exclusion_key = (title.casefold(), wanted_theatre, wanted, start)
            recovered.append({
                "movie_title": title,
                "theatre": theatre_name,
                "show_date": wanted,
                "show_time": start,
                "format_label": _normalize_space(str(item.get("format") or "")) or None,
                "runtime_min": item.get("runtime_min"),
                "a_list_excluded": bool(item.get("a_list_excluded")) or exclusion_key in excluded_keys,
            })

        recovered_unique = {
            str(row.get("movie_title") or "").casefold()
            for row in recovered
            if row.get("movie_title")
        }
        if not recovered_unique:
            return []
        return recovered

    def _mark_previous_report_fallback() -> None:
        try:
            marker = _AMCPath("build/amc_previous_report_fallback.txt")
            marker.parent.mkdir(parents=True, exist_ok=True)
            existing = set()
            if marker.exists():
                existing = {line.strip() for line in marker.read_text(encoding="utf-8").splitlines() if line.strip()}
            existing.add(f"{theatre_name} | {wanted}")
            marker.write_text("\n".join(sorted(existing)) + "\n", encoding="utf-8")
        except Exception as e:
            print(f"[WARN] Could not record AMC previous-report fallback: {e}")

    # Per-Papermill-run live-health circuit breaker. It is intentionally kept
    # in memory (not persisted between workflow retries) so a later retry still
    # gets a fresh chance at AMC. The circuit opens quickly only for explicit
    # anti-bot/interstitial responses; otherwise it requires a broader pattern
    # of repeated failures across multiple theatres and dates. This prevents an
    # AMC-wide block from spending many minutes launching identical browsers for
    # every combination while preserving live retries for isolated failures.
    _live_health = globals().setdefault("_AMC_LIVE_HEALTH", {
        "failures": [],
        "circuit_open": False,
        "reason": "",
        "last_load_started": None,
        "last_explicit_block_at": None,
    })
    _AMCTime = __import__("time")
    try:
        _spacing_seconds = max(0.0, float(__import__("os").environ.get("AMC_LIVE_SPACING_SECONDS", "10")))
    except Exception:
        _spacing_seconds = 10.0
    try:
        _block_cooldown_seconds = max(
            0.0,
            float(__import__("os").environ.get("AMC_EXPLICIT_BLOCK_COOLDOWN_SECONDS", "60")),
        )
    except Exception:
        _block_cooldown_seconds = 60.0
    _live_metrics = globals().setdefault("_AMC_LIVE_METRICS", {
        "started_at": _AMCTime.monotonic(),
        "spacing_seconds": _spacing_seconds,
        "spacing_sleep_seconds": 0.0,
        "block_cooldown_seconds": _block_cooldown_seconds,
        "block_cooldown_sleep_seconds": 0.0,
        "live_page_loads": 0,
        "static_requests": 0,
        "browser_launches": 0,
        "combos_live": 0,
        "combos_reused": 0,
        "combos_mixed": 0,
        "combos_cached": 0,
        "combos_failed": 0,
        "circuit_cache_skips": 0,
        "explicit_block_combos": 0,
        "date_checks": [],
    })

    def _write_live_metrics() -> None:
        try:
            payload = {k: v for k, v in _live_metrics.items() if k != "started_at"}
            payload["elapsed_seconds"] = round(
                max(0.0, _AMCTime.monotonic() - float(_live_metrics.get("started_at") or _AMCTime.monotonic())),
                2,
            )
            payload["circuit_open"] = bool(_live_health.get("circuit_open"))
            payload["circuit_reason"] = str(_live_health.get("reason") or "")
            metrics_path = _AMCPath("build/amc_live_metrics.json")
            metrics_path.parent.mkdir(parents=True, exist_ok=True)
            metrics_path.write_text(__import__("json").dumps(payload, sort_keys=True), encoding="utf-8")
        except Exception:
            pass

    def _pace_live_load(kind: str) -> None:
        # Pace browser starts rather than adding a blind post-request sleep. The
        # normal interval smooths successful navigation, while an explicit AMC
        # interstitial arms a longer cooldown before the next *actual* browser
        # attempt. Cache-only circuit skips never call this helper and therefore
        # never pay a pointless cooldown.
        now = _AMCTime.monotonic()
        last_started = _live_health.get("last_load_started")
        normal_wait = 0.0
        if last_started is not None and _spacing_seconds > 0:
            normal_wait = max(0.0, _spacing_seconds - (now - float(last_started)))

        cooldown_wait = 0.0
        blocked_at = _live_health.get("last_explicit_block_at")
        if blocked_at is not None and _block_cooldown_seconds > 0:
            cooldown_wait = max(0.0, _block_cooldown_seconds - (now - float(blocked_at)))

        wait_for = max(normal_wait, cooldown_wait)
        if wait_for > 0:
            _AMCTime.sleep(wait_for)
            _live_metrics["spacing_sleep_seconds"] = float(
                _live_metrics.get("spacing_sleep_seconds") or 0.0
            ) + wait_for
            if cooldown_wait > normal_wait:
                _live_metrics["block_cooldown_sleep_seconds"] = float(
                    _live_metrics.get("block_cooldown_sleep_seconds") or 0.0
                ) + wait_for

        _live_health["last_load_started"] = _AMCTime.monotonic()
        _live_metrics["live_page_loads"] = int(_live_metrics.get("live_page_loads") or 0) + 1
        metric_key = "browser_launches" if kind == "browser" else "static_requests"
        _live_metrics[metric_key] = int(_live_metrics.get(metric_key) or 0) + 1
        _write_live_metrics()

    def _arm_explicit_block_cooldown() -> None:
        # Start the cooldown when the interstitial is actually observed. Work
        # done after detection naturally consumes part of the configured cooldown window.
        _live_health["last_explicit_block_at"] = _AMCTime.monotonic()
        _write_live_metrics()

    def _finish_combo(outcome: str, *, circuit_skip: bool = False, explicit_block: bool = False) -> None:
        # live: every final slot arrived in this attempt; reused: all slots
        # came from earlier Papermill attempts in this runner; mixed: both;
        # cached: a previous published report was used instead.
        key = {
            "live": "combos_live", "reused": "combos_reused",
            "mixed": "combos_mixed", "cached": "combos_cached",
            "failed": "combos_failed",
        }.get(outcome)
        if key:
            _live_metrics[key] = int(_live_metrics.get(key) or 0) + 1
        if circuit_skip:
            _live_metrics["circuit_cache_skips"] = int(_live_metrics.get("circuit_cache_skips") or 0) + 1
        if explicit_block:
            _live_metrics["explicit_block_combos"] = int(_live_metrics.get("explicit_block_combos") or 0) + 1
        _write_live_metrics()

    def _serialized_local_dates(page_html: str) -> list[str]:
        """Extract Pacific dates from AMC's serialized UTC showtime fields."""
        text = str(page_html or "").replace('\\"', '"')
        dates_found = set()
        try:
            from zoneinfo import ZoneInfo as _AMCMetricZoneInfo
            pacific = _AMCMetricZoneInfo("America/Los_Angeles")
        except Exception:
            pacific = None
        if pacific is None:
            return []
        for match in re.finditer(r'"showDateTimeUtc"\s*:\s*"([^"]+)"', text, re.I):
            try:
                stamp = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
                if stamp.tzinfo is not None:
                    dates_found.add(stamp.astimezone(pacific).date().isoformat())
            except Exception:
                continue
        return sorted(dates_found)

    def _record_date_check(kind: str, round_no: int, page_html: str, *, browser_meta: dict | None = None) -> bool:
        observed = _serialized_local_dates(page_html)
        meta = dict(browser_meta or {})
        requested = str(meta.get("requested_date") or wanted)
        final_date = meta.get("final_url_date")
        # Serialized showtime timestamps are the strongest evidence. If present,
        # the requested local date must be among them. Otherwise a browser page
        # must at least retain the requested date in its final URL. Static HTML
        # without serialized timestamps remains eligible for normal parser/coverage
        # validation because requests does not expose its final redirect URL here.
        if observed:
            accepted = requested in observed
        elif kind == "browser" and meta:
            accepted = bool(meta.get("date_match"))
        else:
            accepted = True

        checks = list(_live_metrics.get("date_checks") or [])
        checks.append({
            "kind": str(kind),
            "theatre": str(theatre_name),
            "requested_date": str(wanted),
            "round": int(round_no),
            "observed_dates": observed or list(meta.get("observed_schedule_dates") or []),
            "final_url_date": final_date,
            "final_host": str(meta.get("final_host") or ""),
            "final_path": str(meta.get("final_path") or ""),
            "restore_attempted": bool(meta.get("date_restore_attempted")),
            "date_valid": bool(accepted),
        })
        # Bound diagnostic growth even if AMC repeatedly fails during retries.
        _live_metrics["date_checks"] = checks[-48:]
        _write_live_metrics()
        return bool(accepted)

    def _is_explicit_amc_block(page_html: str) -> bool:
        low = str(page_html or "").casefold()
        markers = (
            "the site requires javascript to be enabled",
            "queue.amctheatres.com",
            "global safety net",
            "enable-javascript.com",
            "access denied",
            "verify you are human",
            "checking your browser before accessing",
            "attention required",
            "cf-chl",
            "captcha",
            "bot protection",
        )
        return any(marker in low for marker in markers)

    def _record_live_success() -> None:
        _live_health["failures"] = []
        _live_health["circuit_open"] = False
        _live_health["reason"] = ""
        _live_health["last_explicit_block_at"] = None

    def _record_live_failure(reason: str, *, explicit_block: bool = False) -> None:
        failures = list(_live_health.get("failures") or [])
        failures.append({
            "theatre": str(theatre_name),
            "date": str(wanted),
            "reason": str(reason or "live failure"),
            "explicit_block": bool(explicit_block),
        })
        # Keep only the current consecutive-failure streak. A successful combo
        # clears this list via ``_record_live_success``.
        failures = failures[-8:]
        _live_health["failures"] = failures

        theatres = {f.get("theatre") for f in failures if f.get("theatre")}
        dates_seen = {f.get("date") for f in failures if f.get("date")}

        # Do not let one problematic theatre shut down the entire live run. AMC
        # can transiently block several navigations for one location while other
        # theatres still work. Require three *consecutive* explicit anti-bot/
        # interstitial failures whose most-recent three combinations span at
        # least two theatres and at least two requested dates. Any live success
        # clears ``failures`` above, so the explicit streak starts over.
        recent_three = failures[-3:]
        recent_three_combos = {
            (f.get("theatre"), f.get("date"))
            for f in recent_three
            if f.get("theatre") and f.get("date")
        }
        recent_three_theatres = {f.get("theatre") for f in recent_three if f.get("theatre")}
        recent_three_dates = {f.get("date") for f in recent_three if f.get("date")}
        if (
            len(recent_three) == 3
            and all(f.get("explicit_block") for f in recent_three)
            and len(recent_three_combos) == 3
            and len(recent_three_theatres) >= 2
            and len(recent_three_dates) >= 2
        ):
            _live_health["circuit_open"] = True
            _live_health["reason"] = (
                "three consecutive explicit AMC anti-bot/interstitial responses "
                "across multiple theatres and dates"
            )
        elif len(failures) >= 4 and len(theatres) >= 2 and len(dates_seen) >= 2:
            _live_health["circuit_open"] = True
            _live_health["reason"] = "repeated empty/incomplete AMC responses across theatres and dates"

    # If earlier combinations established a site-wide live failure, skip costly
    # network/browser work only when this exact combination has a valid recent
    # previous-report fallback. Otherwise continue live so the circuit breaker
    # can never turn a missing cache into a hard failure by itself.
    if _live_health.get("circuit_open"):
        previous_rows = _previous_report_rows()
        if previous_rows:
            _mark_previous_report_fallback()
            reason = str(_live_health.get("reason") or "AMC live circuit open")
            _write_combo_status(True, reason=reason, fallback=True)
            print(
                f"[WARN] AMC live circuit open for this run ({reason}); "
                f"using recent same-weekend previous report for {theatre_name} {wanted} "
                "without another browser launch."
            )
            _finish_combo("cached", circuit_skip=True)
            return previous_rows

    browser_html = ""
    html_txt = ""
    target = requests.Request("GET", showtimes_url, params={"date": wanted}).prepare().url

    # Browser-only AMC retrieval: one rendered-browser helper call per
    # theatre/date combination. The former static preflight doubled the number
    # of AMC hits and was no longer providing useful schedule data in blocked
    # GitHub runs. The browser helper itself may perform one in-session date
    # restoration if AMC redirects away from the requested day; that safeguard
    # is not a second scraper retry.
    _max_rounds = 1
    _saw_explicit_browser_block = False
    for _round in range(1, _max_rounds + 1):
        print(
            f"[INFO] Loading rendered AMC DOM for {theatre_name} {wanted} "
            f"(cached titles so far: {_unique_title_count()})"
        )
        _browser_blocked = False
        try:
            _pace_live_load("browser")
            status2, round_browser_html = fetch_html_with_browser(
                showtimes_url,
                params={"date": wanted},
                timeout_ms=45000,
            )
            _browser_blocked = _is_explicit_amc_block(round_browser_html)
            _saw_explicit_browser_block = _saw_explicit_browser_block or _browser_blocked
            if _browser_blocked:
                _arm_explicit_block_cooldown()
            _browser_meta = dict(globals().get("_AMC_LAST_BROWSER_META") or {})
            _browser_date_ok = _record_date_check(
                "browser", _round, round_browser_html, browser_meta=_browser_meta
            ) if round_browser_html else True
            if status2 == 200 and round_browser_html and not _browser_blocked and _browser_date_ok:
                browser_html = round_browser_html
                merge_html(round_browser_html, rendered=True, source_label=f"browser-r{_round}")
            else:
                print(
                    f"[WARN] AMC browser fetch returned status {status2} for "
                    f"{theatre_name} {wanted} round {_round}"
                )
        except Exception as e:
            print(f"[WARN] AMC browser enrichment failed for {theatre_name} {wanted} round {_round}: {e}")

        if _browser_blocked and not _clean_rows():
            print(
                f"[WARN] AMC explicit block detected for {theatre_name} {wanted}; "
                "using cache fallback if available."
            )
            break

        _save_cache()
        _save_source_evidence()
        _state = _coverage_state()
        if _state["complete"] and _state["parsed_titles"] > 0:
            break

    final_rows = _clean_rows()
    _state = _coverage_state()
    _coverage_problem = bool(_state["missing_titles"] or _state["missing_showtime_slots"])
    _no_rows = not final_rows

    if _coverage_problem or _no_rows:
        debug_dir = _AMCPath("build/amc_debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_path = debug_dir / f"{_cache_slug}-{wanted}.html"
        debug_path.write_text(browser_html or html_txt or "", encoding="utf-8")
        print(f"[INFO] Saved AMC debug HTML to {debug_path}")

        previous_rows = _previous_report_rows()
        if previous_rows:
            _mark_previous_report_fallback()
            previous_unique = {
                str(row.get("movie_title") or "").casefold()
                for row in previous_rows
                if row.get("movie_title")
            }
            reason = (
                "source/parser coverage gap" if _coverage_problem
                else "no usable live rows"
            )
            _write_combo_status(True, reason=reason, fallback=True)
            _record_live_failure(reason, explicit_block=_saw_explicit_browser_block)
            print(
                f"[WARN] AMC live scrape had {reason} for {theatre_name} {wanted}; "
                f"using recent same-weekend previous report ({len(previous_unique)} movie(s))."
            )
            _finish_combo("cached", explicit_block=_saw_explicit_browser_block)
            return previous_rows

        if _coverage_problem:
            reason = (
                f"source snapshot {_state['source_snapshot']} exposed "
                f"{_state['source_titles']} actionable movie(s)/{_state['source_showtime_slots']} trusted rendered slot(s), "
                f"but parser covered {_state['parsed_titles']} movie(s)/{_state['parsed_showtime_slots']} slot(s); "
                f"missing {len(_state['missing_titles'])} movie(s) and "
                f"{len(_state['missing_showtime_slots'])} trusted rendered slot(s). "
                f"Serialized payload exposed {_state.get('source_serialized_showtime_slots', 0)} slot(s) diagnostically."
            )
            if _state.get("source_diagnostic_only_titles"):
                print(
                    f"[INFO] AMC title-shell diagnostic for {theatre_name} {wanted}: "
                    f"{len(_state['source_diagnostic_only_titles'])} detected title(s) had no trusted rendered "
                    "showtime and were intentionally non-fatal"
                )
            if _state.get("unmatched_source_showtime_ids"):
                print(
                    f"[INFO] AMC raw-ID diagnostic for {theatre_name} {wanted}: "
                    f"{len(_state['unmatched_source_showtime_ids'])} source ID(s) were not present in parsed rows; "
                    "raw IDs are intentionally non-fatal because AMC may replace/duplicate them across snapshots"
                )
            _write_combo_status(False, reason=reason)
            _record_live_failure(reason, explicit_block=_saw_explicit_browser_block)
            print(f"[WARN] AMC source/parser completeness failure for {theatre_name} {wanted}: {reason}")
        else:
            _write_combo_status(False, reason="no usable live rows")
            _record_live_failure("no usable live rows", explicit_block=_saw_explicit_browser_block)
    else:
        # If the source scanner found no independent evidence but normal parsing
        # did recover rows, do not invent a failure. The browser was given its
        # normal opportunity; surface the uncertainty without changing coverage.
        if _state["source_samples"] == 0:
            print(
                f"[WARN] AMC parsed {_state['parsed_titles']} movie(s) for {theatre_name} {wanted}, "
                "but source-evidence scanner found no independently countable regions"
            )
        final_slots = {
            (str(row.get("movie_title") or "").casefold(),
             str(row.get("show_time") or "").casefold())
            for row in final_rows
        }
        current_slots = final_slots & _fresh_row_slots
        if not current_slots:
            # Retaining a successful earlier-attempt schedule is useful, but
            # it does not mean this attempt reached AMC. In particular, an
            # explicit block must still contribute to the live circuit breaker.
            outcome = "reused"
            _write_combo_status(True, reason="runner-local cache from earlier attempt")
            _record_live_failure("no fresh rows in current attempt", explicit_block=_saw_explicit_browser_block)
        else:
            outcome = "live" if current_slots == final_slots else "mixed"
            _write_combo_status(True)
            _record_live_success()

    _finish_combo(
        (outcome if final_rows and not (_coverage_problem or _no_rows) else "failed"),
        explicit_block=_saw_explicit_browser_block,
    )
    return final_rows
'''

DF_SHOW_MARKER = "df_show = pd.DataFrame(showtimes)\n"
DF_SHOW_INSERT = """df_show = pd.DataFrame(showtimes)

# Remove AMC inventory that is not a film before ratings, movie IDs,
# aggregation, numbering, or planner data is constructed.
from scripts.movie_titles import clean_amc_title, is_non_movie_title
df_show["movie_title"] = df_show["movie_title"].map(clean_amc_title)
_non_movie_mask = df_show["movie_title"].map(is_non_movie_title)
if _non_movie_mask.any():
    removed = sorted(df_show.loc[_non_movie_mask, "movie_title"].unique())
    print(f"[INFO] Dropping non-movie AMC inventory: {removed}")
df_show = df_show.loc[~_non_movie_mask].copy()
if df_show.empty:
    raise RuntimeError("All AMC rows were filtered as non-movie inventory.")

# Collapse indistinguishable duplicate clock times before report/planner aggregation.
_display_duplicate_cols = ["movie_title", "theatre", "show_date", "show_time"]
if all(_c in df_show.columns for _c in _display_duplicate_cols):
    _clock_duplicate_mask = df_show.duplicated(subset=_display_duplicate_cols, keep="first")
    if _clock_duplicate_mask.any():
        print(f"[INFO] Collapsing {int(_clock_duplicate_mask.sum())} duplicate AMC clock-time row(s)")
        df_show = df_show.loc[~_clock_duplicate_mask].copy()

# Completeness guard. Browser enrichment already attempts to recover late
# React/Next rows. At this point, reject only structural coverage failures.
# A theatre's screen count is NOT a reliable lower bound on the number of
# movies for an advance schedule, so low-but-valid counts are warnings only.
_unique_movie_count = int(df_show["movie_title"].nunique())

_configured_theatre_names = [
    str(th.get("name") or "").strip()
    for th in THEATRES
    if isinstance(th, dict) and str(th.get("name") or "").strip()
]
_requested_dates = {d.isoformat() for d in dates}
_observed_dates = set(df_show["show_date"].dropna().astype(str))

_incomplete_reasons = []
if _unique_movie_count == 0:
    _incomplete_reasons.append("no unique movies were parsed")
_missing_dates = sorted(_requested_dates - _observed_dates)
if _missing_dates:
    _incomplete_reasons.append(
        "missing requested weekend date(s): " + ", ".join(_missing_dates)
    )

_theatre_text = df_show["theatre"].fillna("").astype(str).map(_normalize_space)

# The scraper writes one runner-local status record per theatre/date. A combo
# can therefore be rejected for a proven source/parser coverage gap even when
# it returned some rows. This is the critical distinction between "AMC really
# lists one movie" and "AMC exposed many movies but we parsed only one."
from pathlib import Path as _AMCStatusPath
_combo_status_dir = _AMCStatusPath("build/amc_combo_status")
for _theatre_name in _configured_theatre_names:
    _status_slug = re.sub(r"[^a-z0-9]+", "-", _theatre_name.lower()).strip("-")
    for _wanted_date in sorted(_requested_dates):
        _status_path = _combo_status_dir / f"{_status_slug}-{_wanted_date}.json"
        if not _status_path.exists():
            continue
        try:
            _combo_status = json.loads(_status_path.read_text(encoding="utf-8"))
        except Exception:
            _combo_status = None
        if isinstance(_combo_status, dict) and not bool(_combo_status.get("complete")):
            _reason = _normalize_space(str(_combo_status.get("reason") or "source/parser coverage incomplete"))
            _incomplete_reasons.append(f"{_theatre_name} {_wanted_date}: {_reason}")

for _theatre_name in _configured_theatre_names:
    _theatre_mask = _theatre_text.str.casefold() == _normalize_space(_theatre_name).casefold()
    _theatre_rows = df_show.loc[_theatre_mask]
    if _theatre_rows.empty:
        _incomplete_reasons.append(f"missing {_theatre_name} entirely")
        continue

    # An asymmetric theatre/date matrix can be legitimate while AMC is still
    # publishing an advance schedule. Surface it in Actions without converting
    # "not posted yet" into a build failure.
    _theatre_dates = set(_theatre_rows["show_date"].dropna().astype(str))
    for _wanted_date in sorted(_requested_dates):
        if _wanted_date not in _theatre_dates:
            print(f"[WARN] {_theatre_name} has no parsed showtimes for {_wanted_date}")

if _incomplete_reasons:
    raise RuntimeError(
        "No fresh AMC showtimes were parsed for the requested dates. "
        "The AMC scrape was structurally incomplete: " + "; ".join(_incomplete_reasons) + "."
    )

"""


PUBLIC_REPORT_SCRUBBER_REPLACEMENT = r'''def remove_noisy_output(soup: BeautifulSoup) -> None:
    """Remove captured scraper INFO/WARN lines from public HTML only."""
    diagnostic_rx = re.compile(r"^\s*\[(?:INFO|WARN)\](?:\s|$)", re.I)

    for tag in list(soup.find_all("pre")):
        raw = tag.get_text("\n", strip=False)
        lines = raw.splitlines(keepends=True)
        if not lines:
            continue

        kept = [line for line in lines if not diagnostic_rx.search(line)]
        if len(kept) == len(lines):
            continue

        cleaned = "".join(kept)
        if cleaned.strip():
            tag.clear()
            tag.append(cleaned)
            continue

        container = tag
        for parent in tag.parents:
            classes = parent.get("class", []) if getattr(parent, "attrs", None) else []
            if any(cls in {
                "jp-OutputArea-child", "jp-OutputArea-output", "jp-RenderedText",
                "output_area", "output_subarea",
            } for cls in classes):
                container = parent
                break
        container.decompose()
'''


def patch_postprocess_source(source: str) -> str:
    """AST-safely replace only remove_noisy_output() in postprocess_report.py."""
    patched, did_replace = _replace_function(
        source,
        "remove_noisy_output",
        PUBLIC_REPORT_SCRUBBER_REPLACEMENT,
    )
    if not did_replace:
        raise RuntimeError(
            "scripts/postprocess_report.py has no top-level remove_noisy_output(); "
            "refusing to silently skip public diagnostic cleanup."
        )
    ast.parse(patched)
    return patched


def patch_postprocess_file(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    patched = patch_postprocess_source(source)
    path.write_text(patched, encoding="utf-8")


def _source_text(cell: dict) -> str:
    source = cell.get("source", [])
    if isinstance(source, list):
        return "".join(source)
    return str(source)


def _set_source(cell: dict, source: str) -> None:
    cell["source"] = source.splitlines(keepends=True)


def _top_level_functions(source: str, function_name: str) -> list[ast.FunctionDef]:
    """
    Return top-level functions with the requested name.

    Notebook code cells used by this project are ordinary Python. A SyntaxError
    means this is not the cell containing the function we are looking for.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    return [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    ]


def _replace_function(
    source: str,
    function_name: str,
    replacement: str,
) -> tuple[str, bool]:
    """Replace exactly one top-level Python function using AST line boundaries."""
    nodes = _top_level_functions(source, function_name)
    if not nodes:
        return source, False
    if len(nodes) != 1:
        raise RuntimeError(
            f"Expected one {function_name}() in a code cell; found {len(nodes)}."
        )

    node = nodes[0]
    lines = source.splitlines(keepends=True)
    start = node.lineno - 1
    end = node.end_lineno

    replacement_text = replacement.rstrip() + "\n"
    return "".join(lines[:start]) + replacement_text + "".join(lines[end:]), True


def _assignment_name(node: ast.AST) -> str | None:
    """Return the simple variable name assigned by an ast.Assign, if any."""
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return None
    target = node.targets[0]
    return target.id if isinstance(target, ast.Name) else None


def _is_original_target_assignment(node: ast.AST) -> bool:
    """
    Identify: target = normalize_title_for_match(title)

    We preserve this original assignment. The previous hotfix accidentally
    replaced a similarly shaped line elsewhere in the notebook; this version
    only uses it as a validated dependency inside build_imdb_lookup().
    """
    if _assignment_name(node) != "target":
        return False

    value = node.value
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "normalize_title_for_match"
        and len(value.args) == 1
        and isinstance(value.args[0], ast.Name)
        and value.args[0].id == "title"
    )


def _patch_imdb_scoring(source: str) -> tuple[str, bool]:
    """
    Patch IMDb scoring inside build_imdb_lookup() only.

    The original function keeps:
        target = normalize_title_for_match(title)

    We replace only the exact/fuzzy scoring assignments. Candidate discovery
    may stay article-insensitive (useful for broad matching), but final ranking
    uses a second normalization that PRESERVES leading articles. This prevents
    ambiguous pairs such as "Title" / "The Title" from being treated as exact
    identities while still allowing canonical AMC suffix variants.
    """
    functions = _top_level_functions(source, "build_imdb_lookup")
    if not functions:
        return source, False
    if len(functions) != 1:
        raise RuntimeError(
            "Expected one build_imdb_lookup() in its code cell; "
            f"found {len(functions)}."
        )

    fn = functions[0]

    target_assignments = [
        node for node in ast.walk(fn) if _is_original_target_assignment(node)
    ]
    if len(target_assignments) != 1:
        raise RuntimeError(
            "Inside build_imdb_lookup(), expected exactly one "
            "'target = normalize_title_for_match(title)' assignment; "
            f"found {len(target_assignments)}."
        )

    exact_nodes = [
        node
        for node in ast.walk(fn)
        if _assignment_name(node) == "exact"
    ]
    fuzz_nodes = [
        node
        for node in ast.walk(fn)
        if _assignment_name(node) == "fuzz_score"
    ]

    if len(exact_nodes) != 1 or len(fuzz_nodes) != 1:
        raise RuntimeError(
            "Inside build_imdb_lookup(), expected exactly one 'exact' and one "
            f"'fuzz_score' assignment; found exact={len(exact_nodes)}, "
            f"fuzz_score={len(fuzz_nodes)}."
        )

    exact_node = exact_nodes[0]
    fuzz_node = fuzz_nodes[0]

    if exact_node.lineno >= fuzz_node.lineno:
        raise RuntimeError(
            "Unexpected build_imdb_lookup() layout: 'exact' must precede "
            "'fuzz_score'."
        )

    # Confirm title_variants exists in this function before referring to it.
    has_title_variants = any(
        isinstance(node, ast.Name) and node.id == "title_variants"
        for node in ast.walk(fn)
    )
    if not has_title_variants:
        raise RuntimeError(
            "build_imdb_lookup() no longer contains title_variants; refusing "
            "to guess how IMDb candidates should be scored."
        )

    lines = source.splitlines(keepends=True)
    start = exact_node.lineno - 1
    end = fuzz_node.end_lineno

    # Match the indentation of the original exact assignment. This keeps the
    # replacement inside the same candidate-selection loop.
    original_line = lines[start]
    indent = original_line[: len(original_line) - len(original_line.lstrip())]

    block_lines = [
        f"{indent}candidate_literal_norms = [\n",
        f"{indent}    re.sub(r'\\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', str(name or '').casefold())).strip()\n",
        f"{indent}    for name in (cand.get('primaryTitle', ''), cand.get('originalTitle', ''))\n",
        f"{indent}    if name\n",
        f"{indent}]\n",
        f"{indent}lookup_literal_targets = [\n",
        f"{indent}    re.sub(r'\\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', str(variant or '').casefold())).strip()\n",
        f"{indent}    for variant in candidate_title_variants(title)\n",
        f"{indent}    if variant\n",
        f"{indent}]\n",
        f"{indent}exact = int(\n",
        f"{indent}    any(\n",
        f"{indent}        lookup_target == candidate_norm\n",
        f"{indent}        for lookup_target in lookup_literal_targets\n",
        f"{indent}        for candidate_norm in candidate_literal_norms\n",
        f"{indent}        if lookup_target and candidate_norm\n",
        f"{indent}    )\n",
        f"{indent})\n",
        f"{indent}fuzz_score = max(\n",
        f"{indent}    (\n",
        f"{indent}        fuzz.ratio(candidate_norm, lookup_target)\n",
        f"{indent}        for candidate_norm in candidate_literal_norms\n",
        f"{indent}        for lookup_target in lookup_literal_targets\n",
        f"{indent}        if lookup_target and candidate_norm\n",
        f"{indent}    ),\n",
        f"{indent}    default=0,\n",
        f"{indent})\n",
        f"{indent}lookup_year_hint = metadata_release_year_hint(title, reference_year=current_year)\n",
        f"{indent}year_match = int(\n",
        f"{indent}    lookup_year_hint is not None and cand.get('startYear') == lookup_year_hint\n",
        f"{indent})\n",
    ]

    updated = "".join(lines[:start]) + "".join(block_lines) + "".join(lines[end:])

    _validate_imdb_patch(updated)
    return updated, True


def _patch_imdb_year_priority(source: str) -> tuple[str, bool]:
    """Put an anniversary/explicit release-year match ahead of generic recency.

    This is AST-scoped to ``build_imdb_lookup`` for the same safety reason as
    the title-scoring patch: if the notebook layout changes, fail rather than
    editing an unrelated tuple elsewhere in the notebook.
    """
    functions = _top_level_functions(source, "build_imdb_lookup")
    if not functions:
        return source, False
    if len(functions) != 1:
        raise RuntimeError("Expected one build_imdb_lookup() for year-priority patch.")
    fn = functions[0]
    score_assignments = [
        node for node in ast.walk(fn)
        if _assignment_name(node) == "score_key" and isinstance(node.value, ast.Tuple)
    ]
    if len(score_assignments) != 1:
        raise RuntimeError(
            "Inside build_imdb_lookup(), expected one score_key tuple; "
            f"found {len(score_assignments)}."
        )
    node = score_assignments[0]
    segment = ast.get_source_segment(source, node)
    if not segment or "exact" not in segment or "fuzz_score" not in segment:
        raise RuntimeError("IMDb score_key layout changed; refusing year-priority patch.")
    lines = source.splitlines(keepends=True)
    start = node.lineno - 1
    end = node.end_lineno
    original = "".join(lines[start:end])
    if "year_match," in original:
        return source, True
    marker = "score_key = (\n"
    if marker not in original:
        raise RuntimeError(
            "IMDb score_key tuple formatting changed; refusing a partial year-priority patch."
        )
    updated_tuple = original.replace(
        marker,
        marker + " " * (node.col_offset + 4) + "year_match,\n",
        1,
    )
    if "year_match," not in updated_tuple:
        raise RuntimeError("IMDb year-priority patch did not modify score_key.")
    return "".join(lines[:start]) + updated_tuple + "".join(lines[end:]), True


def _validate_imdb_patch(source: str) -> None:
    """
    Statically validate the transformed build_imdb_lookup().

    This specifically catches the class of bug that caused the prior
    deployment failure: a scoring block referring to a variable that was never
    assigned inside the function.
    """
    functions = _top_level_functions(source, "build_imdb_lookup")
    if len(functions) != 1:
        raise RuntimeError(
            "Patched code does not contain exactly one build_imdb_lookup()."
        )

    fn = functions[0]

    assignments: dict[str, list[int]] = {}
    loads: dict[str, list[int]] = {}

    for node in ast.walk(fn):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                assignments.setdefault(node.id, []).append(node.lineno)
            elif isinstance(node.ctx, ast.Load):
                loads.setdefault(node.id, []).append(node.lineno)

    # These are introduced by this patch and therefore must be locally assigned.
    for name in ("candidate_literal_norms", "lookup_literal_targets"):
        if name not in assignments:
            raise RuntimeError(
                f"Patched build_imdb_lookup() uses no local assignment for {name!r}."
            )
        if name in loads and min(assignments[name]) > min(loads[name]):
            raise RuntimeError(
                f"Patched build_imdb_lookup() reads {name!r} before assignment."
            )

    # The broken prior version introduced "targets" without reliably defining
    # it in this function. It must not exist at all in the new patch.
    if "targets" in loads or "targets" in assignments:
        raise RuntimeError(
            "Obsolete variable 'targets' remains inside build_imdb_lookup()."
        )

    if not any(_is_original_target_assignment(node) for node in ast.walk(fn)):
        raise RuntimeError(
            "Original IMDb target normalization assignment disappeared."
        )


def patch_notebook(notebook: dict) -> dict:
    upcoming_weekend_replacements = 0
    weekend_print_replacements = 0
    date_list_replacements = 0
    amc_scrape_loop_replacements = 0
    candidate_function_replacements = 0
    df_show_insertions = 0
    imdb_function_patches = 0
    imdb_year_priority_patches = 0
    rt_parser_replacements = 0
    rt_get_scores_replacements = 0
    rt_lookup_loop_replacements = 0
    rt_display_insertions = 0
    rt_desired_replacements = 0
    amc_static_fetch_replacements = 0
    amc_browser_fetch_replacements = 0
    amc_showtime_parser_replacements = 0
    amc_scrape_function_replacements = 0

    for cell in notebook.get("cells", []):
        if cell.get("cell_type") != "code":
            continue

        source = _source_text(cell)

        source, did_patch_weekend = _replace_function(
            source,
            "upcoming_weekend_pacific",
            UPCOMING_WEEKEND_REPLACEMENT,
        )
        upcoming_weekend_replacements += int(did_patch_weekend)

        print_count = source.count(WEEKEND_PRINT_OLD)
        if print_count > 1:
            raise RuntimeError(
                f"Expected at most one weekend summary print in a cell; found {print_count}."
            )
        if print_count == 1:
            source = source.replace(WEEKEND_PRINT_OLD, WEEKEND_PRINT_NEW, 1)
            weekend_print_replacements += 1

        date_list_count = source.count(DATE_LIST_OLD)
        if date_list_count > 1:
            raise RuntimeError(
                f"Expected at most one requested-date list in a cell; found {date_list_count}."
            )
        if date_list_count == 1:
            source = source.replace(DATE_LIST_OLD, DATE_LIST_NEW, 1)
            date_list_replacements += 1

        scrape_loop_count = source.count(AMC_SCRAPE_LOOP_OLD)
        if scrape_loop_count > 1:
            raise RuntimeError(
                f"Expected at most one AMC scrape-order loop in a cell; found {scrape_loop_count}."
            )
        if scrape_loop_count == 1:
            source = source.replace(AMC_SCRAPE_LOOP_OLD, AMC_SCRAPE_LOOP_NEW, 1)
            amc_scrape_loop_replacements += 1

        source, did_replace = _replace_function(
            source,
            "candidate_title_variants",
            FUNCTION_WRAPPER,
        )
        candidate_function_replacements += int(did_replace)

        source, did_patch_imdb = _patch_imdb_scoring(source)
        imdb_function_patches += int(did_patch_imdb)
        source, did_patch_imdb_year = _patch_imdb_year_priority(source)
        imdb_year_priority_patches += int(did_patch_imdb_year)

        source, did_patch_rt = _replace_function(
            source,
            "rt_parse_scores",
            RT_PARSE_REPLACEMENT,
        )
        rt_parser_replacements += int(did_patch_rt)

        source, did_patch_rt_get_scores = _replace_function(
            source,
            "rt_get_scores",
            RT_GET_SCORES_REPLACEMENT,
        )
        rt_get_scores_replacements += int(did_patch_rt_get_scores)

        rt_loop_count = source.count(RT_LOOKUP_LOOP_OLD)
        if rt_loop_count > 1:
            raise RuntimeError(
                f"Expected at most one RT movie lookup loop in a cell; found {rt_loop_count}."
            )
        if rt_loop_count == 1:
            source = source.replace(RT_LOOKUP_LOOP_OLD, RT_LOOKUP_LOOP_NEW, 1)
            rt_lookup_loop_replacements += 1

        source, did_patch_amc_static = _replace_function(
            source,
            "fetch_amc_html",
            AMC_STATIC_FETCH_REPLACEMENT,
        )
        amc_static_fetch_replacements += int(did_patch_amc_static)

        source, did_patch_amc_browser = _replace_function(
            source,
            "fetch_html_with_browser",
            AMC_BROWSER_FETCH_REPLACEMENT,
        )
        amc_browser_fetch_replacements += int(did_patch_amc_browser)

        source, did_patch_amc_showtimes = _replace_function(
            source,
            "extract_showtimes_from_json_scripts",
            AMC_SHOWTIME_PARSE_REPLACEMENT,
        )
        amc_showtime_parser_replacements += int(did_patch_amc_showtimes)

        source, did_patch_amc_scrape = _replace_function(
            source,
            "scrape_amc_showtimes_for_date",
            AMC_SCRAPE_REPLACEMENT,
        )
        amc_scrape_function_replacements += int(did_patch_amc_scrape)

        rt_display_count = source.count(RT_DISPLAY_MARKER)
        if rt_display_count:
            if rt_display_count != 1:
                raise RuntimeError(
                    f"Expected one df_display marker in a cell; found {rt_display_count}."
                )
            source = source.replace(RT_DISPLAY_MARKER, RT_DISPLAY_INSERT, 1)
            rt_display_insertions += 1

        rt_desired_count = source.count(RT_DESIRED_OLD)
        if rt_desired_count:
            if rt_desired_count != 1:
                raise RuntimeError(
                    f"Expected one RT desired-column block; found {rt_desired_count}."
                )
            source = source.replace(RT_DESIRED_OLD, RT_DESIRED_NEW, 1)
            rt_desired_replacements += 1

        marker_count = source.count(DF_SHOW_MARKER)
        if marker_count:
            if marker_count != 1:
                raise RuntimeError(
                    f"Expected one df_show marker in a cell; found {marker_count}."
                )
            source = source.replace(DF_SHOW_MARKER, DF_SHOW_INSERT, 1)
            df_show_insertions += 1

        _set_source(cell, source)

    expected = {
        "upcoming_weekend_pacific replacement": upcoming_weekend_replacements,
        "current-date request-list replacement": date_list_replacements,
        "AMC date-major scrape ordering": amc_scrape_loop_replacements,
        "candidate_title_variants replacement": candidate_function_replacements,
        "df_show non-movie filter": df_show_insertions,
        "build_imdb_lookup patch": imdb_function_patches,
        "IMDb original-year priority patch": imdb_year_priority_patches,
        "rt_parse_scores replacement": rt_parser_replacements,
        "rt_get_scores replacement": rt_get_scores_replacements,
        "RT runtime-aware lookup loop": rt_lookup_loop_replacements,
        "RT display column insertion": rt_display_insertions,
        "RT desired-column replacement": rt_desired_replacements,
        "AMC single-pass static fetch replacement": amc_static_fetch_replacements,
        "AMC stabilized browser fetch replacement": amc_browser_fetch_replacements,
        "AMC escaped showtime parser replacement": amc_showtime_parser_replacements,
        "AMC browser-enriched scrape replacement": amc_scrape_function_replacements,
    }

    failures = {name: count for name, count in expected.items() if count != 1}
    if failures:
        detail = ", ".join(f"{name}={count}" for name, count in failures.items())
        raise RuntimeError(
            "Notebook structure did not match the expected AMC_Scraper_5.ipynb; "
            f"refusing a partial patch ({detail})."
        )

    return notebook


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_notebook", type=Path)
    parser.add_argument("output_notebook", type=Path)
    args = parser.parse_args()

    with args.input_notebook.open("r", encoding="utf-8") as fh:
        notebook = json.load(fh)

    patched = patch_notebook(notebook)

    # Patch only the runner's working copy. The workflow commits docs/, not
    # scripts/, so the repository source remains unchanged and this is applied
    # afresh on every run.
    postprocess_path = Path("scripts/postprocess_report.py")
    if postprocess_path.exists():
        patch_postprocess_file(postprocess_path)
    else:
        raise RuntimeError(f"Missing required postprocessor: {postprocess_path}")

    args.output_notebook.parent.mkdir(parents=True, exist_ok=True)
    with args.output_notebook.open("w", encoding="utf-8") as fh:
        json.dump(patched, fh, ensure_ascii=False, indent=1)
        fh.write("\n")

    print(
        "[OK] AST-scoped patch applied and validated: title normalization, "
        "non-movie filtering, original-film metadata lookup, stabilized AMC browser fetch, "
        "AMC A-List metadata + clean public logs v44 -> "
        f"{args.output_notebook}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
