#!/usr/bin/env python3
"""
Patch the AMC notebook into build/ before Papermill executes it.

This patcher is intentionally conservative:
1. Replace the notebook's candidate_title_variants() with the shared generic
   implementation in scripts/movie_titles.py.
2. Remove non-film AMC inventory before ratings/numbering/planner generation.
3. Improve IMDb candidate scoring so it considers every canonical lookup
   variant while preserving leading articles for final identity ranking.
4. Keep Rotten Tomatoes on its existing direct-page method, but add the
   original release-year hint for anniversary/repertory titles and parse scores
   only when critic/audience labels are explicit.
5. Emit an explicit RT_C/A display column so a missing side renders as "-".
6. Parse AMC's current escaped React/Next showtime payload without relying on
   brittle field ordering.
7. If the server-rendered page is suspiciously sparse relative to the theatre
   screen count, fetch the fully rendered browser DOM and merge its
   aria-labelled movie/showtime regions.
8. Let the Playwright fallback wait for AMC's dynamically rendered showtime
   DOM to stabilize instead of taking a fixed-delay snapshot.
9. If AMC still serves a sparse/blocked response, allow only a recent,
   same-weekend prior successful report to act as a transparent last-good
   showtime cache; preserve per-showtime A-List exclusions while doing so.
10. Reject suspiciously incomplete aggregate weekend reports before ratings or
   publishing, using theatre/date coverage plus a conservative prior-report
   baseline when available.
11. Preserve AMC's explicit A-List exclusion metadata (NOALIST) through SSR and
   rendered-DOM recovery paths.
12. Keep scraper INFO/WARN diagnostics out of the public HTML while leaving
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
    debug: bool = False) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """
    Fetch Rotten Tomatoes with the same direct-page strategy as the notebook.

    The only lookup expansion here is evidence already encoded in AMC's title:
    an explicit year or anniversary-derived original release year is tried
    before the current-year guesses.  This fixes repertory presentations such
    as "<film> 10th Anniversary Remastered" without adding another scraper or
    relying on a search engine that may be blocked in CI.
    """
    key = (title or "").lower().strip()
    if key in cache:
        return cache[key]

    current_year = date.today().year
    hinted_year = metadata_release_year_hint(title, reference_year=current_year)
    years_to_check = []
    for year in (hinted_year, current_year, current_year + 1, current_year - 1):
        if year is not None and year not in years_to_check:
            years_to_check.append(year)

    for q in candidate_title_variants(title):
        base_slug = rt_slugify(q)
        if not base_slug:
            continue

        # Keep the yearless RT slug last because it can be ambiguous for reused
        # titles.  A known original-feature year should get first chance.
        url_attempts = [f"{RT_BASE}/m/{base_slug}_{y}" for y in years_to_check]
        url_attempts.append(f"{RT_BASE}/m/{base_slug}")

        for rt_url in url_attempts:
            status, raw_html = fetch_html(session, rt_url, params=None, tries=2)
            if debug:
                print(f"[RT] Testing {rt_url} -> Status: {status}")
            if status != 200 or not raw_html:
                continue

            lower_html = raw_html.lower()
            if "404 - page not found" in lower_html or "couldn't find the page you're looking for" in lower_html:
                continue

            decoded = html_lib.unescape(raw_html)
            soup = BeautifulSoup(decoded, "html.parser")
            aud, crit = rt_parse_scores(decoded, soup)

            # A 200 response without either labeled score is not a successful
            # movie match. Continue to the next canonical/year candidate rather
            # than caching a blank result too early.
            if aud is None and crit is None:
                continue

            cache[key] = (aud, crit, rt_url)
            return cache[key]

    cache[key] = (None, None, None)
    return cache[key]
'''


AMC_BROWSER_CHILD_SCRIPT = r"""
import json
import re
import sys
import time
from urllib.parse import unquote, urljoin
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

    payload = {
        "status": int(status or 200),
        "html": page.content(),
        "url": page.url,
        "signature": showtime_signature(page),
    }
    context.close()
    browser.close()

print(json.dumps(payload))
"""

AMC_BROWSER_FETCH_REPLACEMENT_TEMPLATE = r'''def fetch_html_with_browser(url: str, params: dict | None = None, timeout_ms: int = 30000) -> Tuple[int, str]:
    """
    Fetch an AMC page in Chromium and wait for its client-rendered showtime DOM
    to settle before taking the HTML snapshot.

    AMC can hydrate the movie/showtime regions several seconds after the first
    document arrives. A fixed short sleep is race-prone on GitHub-hosted runners,
    so the child browser polls generic showtime signals until they stabilize.
    """
    import os as _amc_os

    full_url = requests.Request("GET", url, params=params).prepare().url
    query_string = urlparse(full_url).query
    browser_fetch_script = base64.b64decode("__BROWSER_FETCH_B64__").decode("utf-8")
    executable_path = _amc_os.environ.get("AMC_CHROMIUM_EXECUTABLE", "")

    proc = subprocess.run(
        [
            sys.executable,
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

    _screen_match = re.search(r"\b(\d{1,2})\s*$", theatre_name or "")
    _screen_count = int(_screen_match.group(1)) if _screen_match else None
    _sparse_threshold = (
        max(3, min(10, int((_screen_count * 0.45) + 0.999)))
        if _screen_count
        else 5
    )

    _cache_dir = _AMCPath("build/amc_combo_cache")
    _cache_dir.mkdir(parents=True, exist_ok=True)
    _cache_slug = re.sub(r"[^a-z0-9]+", "-", theatre_name.lower()).strip("-")
    _cache_path = _cache_dir / f"{_cache_slug}-{wanted}.json"

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
                if _unique_title_count() >= _sparse_threshold:
                    print(
                        f"[INFO] Reusing fresh in-run AMC cache for {theatre_name} {wanted}: "
                        f"{_unique_title_count()} movie(s)"
                    )
                    return _clean_rows()
                elif out:
                    print(
                        f"[INFO] Loaded partial in-run AMC cache for {theatre_name} {wanted}: "
                        f"{_unique_title_count()} movie(s); continuing enrichment"
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

    def merge_html(page_html: str, *, rendered: bool = False) -> None:
        if not page_html:
            return

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
        if len(recovered_unique) < _sparse_threshold:
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

    browser_html = ""
    html_txt = ""
    target = requests.Request("GET", showtimes_url, params={"date": wanted}).prepare().url

    # Two targeted rounds are much more useful than throwing away every good
    # theatre/date and restarting the entire notebook. Round 1 always renders
    # the browser DOM because AMC's static React payload can contain enough
    # titles while still omitting individual showtime links. Round 2 is used
    # only when the combination remains sparse.
    _max_rounds = 2
    for _round in range(1, _max_rounds + 1):
        try:
            status, round_html = fetch_amc_html(session, showtimes_url, params={"date": wanted})
        except Exception as e:
            status, round_html = 0, ""
            print(f"[WARN] AMC static fetch failed for {theatre_name} {wanted} round {_round}: {e}")

        if status == 200 and round_html:
            html_txt = round_html
            merge_html(round_html, rendered=False)
        else:
            print(
                f"[WARN] AMC static fetch returned status {status} for "
                f"{theatre_name} {wanted} round {_round}; trying browser DOM"
            )

        _need_browser = (_round == 1) or (_unique_title_count() < _sparse_threshold)
        if _need_browser:
            if _round == 1:
                print(
                    f"[INFO] Merging rendered AMC DOM for {theatre_name} {wanted} "
                    f"(static/cached titles so far: {_unique_title_count()})"
                )
            else:
                print(
                    f"[INFO] Targeted AMC retry {_round}/{_max_rounds} for {theatre_name} {wanted}: "
                    f"only {_unique_title_count()} movie(s), threshold {_sparse_threshold}"
                )
            try:
                status2, round_browser_html = fetch_html_with_browser(
                    showtimes_url,
                    params={"date": wanted},
                    timeout_ms=45000,
                )
                if status2 == 200 and round_browser_html:
                    browser_html = round_browser_html
                    merge_html(round_browser_html, rendered=True)
                else:
                    print(
                        f"[WARN] AMC browser fetch returned status {status2} for "
                        f"{theatre_name} {wanted} round {_round}"
                    )
            except Exception as e:
                print(f"[WARN] AMC browser enrichment failed for {theatre_name} {wanted} round {_round}: {e}")

        _save_cache()
        if _unique_title_count() >= _sparse_threshold:
            break

    final_rows = _clean_rows()
    final_unique = {r["movie_title"].casefold() for r in final_rows if r.get("movie_title")}
    if not final_rows or len(final_unique) < _sparse_threshold:
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
            print(
                f"[WARN] AMC live scrape remained sparse for {theatre_name} {wanted}; "
                f"using recent same-weekend previous report ({len(previous_unique)} movie(s))."
            )
            return previous_rows

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

# Completeness guard. AMC occasionally serves a partial React/queue response
# that contains many valid rows for only one theatre or one day. A healthy
# aggregate count therefore is not enough evidence that the weekend is complete.
_unique_movie_count = int(df_show["movie_title"].nunique())
_theatre_count = int(df_show["theatre"].nunique()) if "theatre" in df_show.columns else 0
_date_count = int(df_show["show_date"].nunique()) if "show_date" in df_show.columns else 0

# This report intentionally combines these three configured AMC locations
# across both weekend days. Match by stable theatre prefix so harmless wording
# changes such as "@ The District" vs "at The District" do not break coverage.
_project_theatre_specs = [
    ("AMC Tustin 14", 14),
    ("AMC Woodbridge 5", 5),
    ("AMC Orange 30", 30),
]

# Ten unique titles is a conservative overall floor.
_minimum_unique_movies = 10

# When the checked-in prior report was healthy, use it as an additional soft
# baseline. This prevents a sudden collapse from silently replacing a normal
# report, while still allowing substantial week-to-week changes.
try:
    from pathlib import Path as _AMCPath
    _previous_html = _AMCPath("docs/index.html")
    _previous_movie_count = 0
    if _previous_html.exists():
        _previous_text = _previous_html.read_text(encoding="utf-8", errors="ignore")
        _previous_movie_count = len(
            set(re.findall(r'data-movie-id="([0-9]+)"', _previous_text))
        )
    if _previous_movie_count >= 12:
        _minimum_unique_movies = max(
            _minimum_unique_movies,
            min(20, int((_previous_movie_count * 0.40) + 0.999)),
        )
except Exception:
    _previous_movie_count = 0

_incomplete_reasons = []
if _unique_movie_count < _minimum_unique_movies:
    _incomplete_reasons.append(
        f"only {_unique_movie_count} unique movie(s), expected at least {_minimum_unique_movies}"
    )
if _date_count < 2:
    _incomplete_reasons.append(f"only {_date_count} weekend date represented")

# Validate the full theatre x date matrix instead of merely requiring "2
# theatres somewhere" and "2 dates somewhere". That old aggregate rule allowed
# Sunday Orange + a few Woodbridge rows to publish even when Tustin and most of
# Saturday were absent.
_theatre_text = df_show["theatre"].fillna("").astype(str)
_observed_dates = sorted(
    str(x) for x in df_show["show_date"].dropna().astype(str).unique()
)

for _theatre_prefix, _screen_count in _project_theatre_specs:
    _theatre_mask = _theatre_text.str.contains(
        re.escape(_theatre_prefix), case=False, regex=True, na=False
    )
    _theatre_rows = df_show.loc[_theatre_mask]
    if _theatre_rows.empty:
        _incomplete_reasons.append(f"missing {_theatre_prefix} entirely")
        continue

    _theatre_dates = set(_theatre_rows["show_date"].dropna().astype(str))
    if len(_theatre_dates) < 2:
        _incomplete_reasons.append(
            f"{_theatre_prefix} has only {len(_theatre_dates)} weekend date(s)"
        )

    # Use the same screen-count-aware floor that triggers browser enrichment:
    # Orange 30 -> 10 titles/date, Tustin 14 -> 7, Woodbridge 5 -> 3.
    _combo_min_movies = max(3, min(10, int((_screen_count * 0.45) + 0.999)))
    for _wanted_date in _observed_dates:
        _combo_rows = _theatre_rows.loc[
            _theatre_rows["show_date"].astype(str) == _wanted_date
        ]
        _combo_movies = int(_combo_rows["movie_title"].nunique())
        if _combo_movies < _combo_min_movies:
            _incomplete_reasons.append(
                f"{_theatre_prefix} {_wanted_date} has only {_combo_movies} "
                f"movie(s), expected at least {_combo_min_movies}"
            )

if _incomplete_reasons:
    raise RuntimeError(
        "No fresh AMC showtimes were parsed for the requested dates. "
        "The AMC scrape was suspiciously incomplete: " + "; ".join(_incomplete_reasons) + "."
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
    candidate_function_replacements = 0
    df_show_insertions = 0
    imdb_function_patches = 0
    imdb_year_priority_patches = 0
    rt_parser_replacements = 0
    rt_get_scores_replacements = 0
    rt_display_insertions = 0
    rt_desired_replacements = 0
    amc_browser_fetch_replacements = 0
    amc_showtime_parser_replacements = 0
    amc_scrape_function_replacements = 0

    for cell in notebook.get("cells", []):
        if cell.get("cell_type") != "code":
            continue

        source = _source_text(cell)

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
        "candidate_title_variants replacement": candidate_function_replacements,
        "df_show non-movie filter": df_show_insertions,
        "build_imdb_lookup patch": imdb_function_patches,
        "IMDb original-year priority patch": imdb_year_priority_patches,
        "rt_parse_scores replacement": rt_parser_replacements,
        "rt_get_scores replacement": rt_get_scores_replacements,
        "RT display column insertion": rt_display_insertions,
        "RT desired-column replacement": rt_desired_replacements,
        "AMC stabilized browser fetch replacement": amc_browser_fetch_replacements,
        "AMC escaped showtime parser replacement": amc_showtime_parser_replacements,
        "AMC sparse-page scrape replacement": amc_scrape_function_replacements,
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
        "AMC A-List metadata + clean public logs v22 -> "
        f"{args.output_notebook}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
