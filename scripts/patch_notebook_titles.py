#!/usr/bin/env python3
"""
Patch the AMC notebook into build/ before Papermill executes it.

This patcher is intentionally conservative:
1. Replace the notebook's candidate_title_variants() with the shared generic
   implementation in scripts/movie_titles.py.
2. Remove non-film AMC inventory before ratings/numbering/planner generation.
3. Improve IMDb candidate scoring so it considers every canonical lookup
   variant while preserving leading articles for final identity ranking.
4. Parse Rotten Tomatoes scores only when their critic/audience labels are
   explicit, preventing one score from being copied into the other slot.
5. Emit an explicit RT_C/A display column so a missing side renders as "-".
6. Parse AMC's current escaped React/Next showtime payload without relying on
   brittle field ordering.
7. If the server-rendered page is suspiciously sparse, fetch the fully rendered
   browser DOM and merge its aria-labelled movie/showtime regions.

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
import json
from pathlib import Path
import sys


FUNCTION_WRAPPER = """def candidate_title_variants(title: str) -> List[str]:
    \"""Use the shared generic AMC-title normalizer for metadata lookup.\"""
    from scripts.movie_titles import candidate_title_variants as _shared_variants
    return _shared_variants(title)
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
    # Extract AMC showtimes from structured scripts plus escaped React/Next
    # flight data without assuming a fixed field order.
    soup = BeautifulSoup(html_txt or "", "html.parser")
    out: List[dict] = []
    seen = set()
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
        key = (
            title.casefold(),
            str(row.get("theatre") or "").casefold(),
            wanted,
            show_time,
            str(row.get("format_label") or "").strip().casefold(),
        )
        if key not in seen:
            out.append(row)
            seen.add(key)

    # Keep the notebook's generic structured-JSON walker first because it can
    # preserve runtime/format metadata when AMC exposes clean JSON.
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

    # Flight data can be serialized one or more escaping layers deep.
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

    # Collect movie-title markers separately from showtimes. aria-label is
    # strongest, while explicit movie-title keys cover alternate flight shapes.
    title_markers = []
    marker_patterns = (
        r'aria-label"\s*:\s*"Showtimes for ([^"]+)"',
        r"aria-label\s*=\s*[\"']Showtimes for ([^\"']+)[\"']",
        r'"movieTitle"\s*:\s*"([^"]+)"',
        r'"movieName"\s*:\s*"([^"]+)"',
        r'"filmTitle"\s*:\s*"([^"]+)"',
        r'"titleName"\s*:\s*"([^"]+)"',
    )
    for priority, pattern in enumerate(marker_patterns):
        for m in re.finditer(pattern, text, re.I):
            title = _normalize_space(_decode_text(m.group(1)))
            if title and looks_like_title_text(title):
                title_markers.append((m.start(), priority, title))
    title_markers.sort(key=lambda item: (item[0], item[1]))

    showtime_matches = list(re.finditer(
        r'"showtimeId"\s*:\s*(?:"(\d+)"|(\d+))',
        text,
        re.I,
    ))

    try:
        from zoneinfo import ZoneInfo
        pacific = ZoneInfo("America/Los_Angeles")
    except Exception:
        pacific = None

    marker_i = 0
    current_title = None
    for idx, sm in enumerate(showtime_matches):
        while marker_i < len(title_markers) and title_markers[marker_i][0] < sm.start():
            current_title = title_markers[marker_i][2]
            marker_i += 1

        # Slice to the next showtimeId rather than depending on object field
        # ordering or trying to balance nested braces with one regex.
        seg_end = (
            showtime_matches[idx + 1].start()
            if idx + 1 < len(showtime_matches)
            else min(len(text), sm.start() + 16000)
        )
        segment = text[sm.start():seg_end]

        local_title = None
        for pattern in marker_patterns[2:]:
            lm = re.search(pattern, segment, re.I)
            if lm:
                candidate = _normalize_space(_decode_text(lm.group(1)))
                if candidate and looks_like_title_text(candidate):
                    local_title = candidate
                    break
        title = local_title or current_title
        if not title:
            continue

        status_m = re.search(r'"status"\s*:\s*"([^"]+)"', segment, re.I)
        if status_m and re.search(r"cancel", status_m.group(1), re.I):
            continue

        utc_m = re.search(r'"showDateTimeUtc"\s*:\s*"([^"]+)"', segment, re.I)
        if not utc_m:
            continue
        utc_txt = _decode_text(utc_m.group(1)).strip()

        local_dt = None
        try:
            local_dt = datetime.fromisoformat(utc_txt.replace("Z", "+00:00"))
            if local_dt.tzinfo is not None and pacific is not None:
                local_dt = local_dt.astimezone(pacific)
            if local_dt.date().isoformat() != wanted:
                continue
        except Exception:
            local_dt = None

        display_m = re.search(r'"display"\s*:\s*\{(.{0,1200}?)\}', segment, re.I | re.S)
        display_text = display_m.group(1) if display_m else segment
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

        add_row({
            "movie_title": title,
            "theatre": theatre_name,
            "show_date": wanted,
            "show_time": time_txt,
            "format_label": None,
            "runtime_min": None,
            "a_list_excluded": False,
        })

    return out
'''

AMC_SCRAPE_REPLACEMENT = r'''def scrape_amc_showtimes_for_date(session: requests.Session, theatre_name: str, showtimes_url: str, d: date) -> List[dict]:
    # Merge static JSON/flight data, current rendered aria-labelled regions,
    # and the legacy semantic DOM parser. If static content is suspiciously
    # sparse, force a fully rendered Playwright page and merge that too.
    wanted = d.isoformat()
    status, html_txt = fetch_amc_html(session, showtimes_url, params={"date": wanted})
    if status != 200:
        return []

    out: List[dict] = []
    seen = set()

    def add_row(row: dict) -> None:
        if not row or row.get("show_date") != wanted:
            return
        title = _normalize_space(str(row.get("movie_title") or ""))
        show_time = _normalize_space(str(row.get("show_time") or "")).lower()
        if not title or not TIME_RE.search(show_time):
            return
        row = dict(row)
        row["movie_title"] = title
        row["show_time"] = show_time
        key = (
            title.casefold(),
            str(row.get("theatre") or theatre_name).casefold(),
            wanted,
            show_time,
            str(row.get("format_label") or "").strip().casefold(),
        )
        if key not in seen:
            out.append(row)
            seen.add(key)

    def merge_html(page_html: str) -> None:
        if not page_html:
            return

        for row in extract_showtimes_from_json_scripts(page_html, theatre_name, d):
            add_row(row)

        soup = BeautifulSoup(page_html, "html.parser")

        # Current AMC rendered DOM, also used by current independent tooling:
        # [aria-label^="Showtimes for"] regions containing /showtimes/<id> links.
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
                if "/showtimes/" not in href:
                    continue
                txt = _normalize_space(a.get_text(" ", strip=True))
                tm = TIME_RE.search(txt)
                if not tm:
                    tm = TIME_RE.search(_normalize_space(str(a.get("aria-label") or "")))
                if not tm:
                    continue
                add_row({
                    "movie_title": title,
                    "theatre": theatre_name,
                    "show_date": wanted,
                    "show_time": tm.group(1).lower(),
                    "format_label": _extract_local_format_near_tag(a),
                    "runtime_min": runtime_min,
                    "a_list_excluded": is_a_list_excluded_near_tag(a),
                })

        # Keep the older DOM parser additive. v11 accidentally skipped this
        # entire path as soon as SSR found even one row.
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
                        })

    merge_html(html_txt)

    unique_titles = {r["movie_title"].casefold() for r in out}
    browser_html = ""
    if len(unique_titles) < 5:
        target = requests.Request("GET", showtimes_url, params={"date": wanted}).prepare().url
        print(
            f"[INFO] AMC static page for {theatre_name} {wanted} yielded only "
            f"{len(unique_titles)} movie(s); merging fully rendered browser DOM from {target}"
        )
        try:
            status2, browser_html = fetch_html_with_browser(
                showtimes_url,
                params={"date": wanted},
                timeout_ms=45000,
            )
            if status2 == 200 and browser_html:
                merge_html(browser_html)
        except Exception as e:
            print(f"[WARN] Sparse-page browser enrichment failed for {theatre_name} {wanted}: {e}")

    out = [r for r in out if r.get("show_date") == wanted]

    final_unique = {r["movie_title"].casefold() for r in out}
    if not out or len(final_unique) < 5:
        debug_dir = Path("build/amc_debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", theatre_name.lower()).strip("-")
        debug_path = debug_dir / f"{slug}-{wanted}.html"
        debug_path.write_text(browser_html or html_txt or "", encoding="utf-8")
        print(f"[INFO] Saved AMC debug HTML to {debug_path}")

    return out
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

# A three-theatre weekend report with fewer than five unique movies is almost
# certainly a sparse/partial AMC response. Reuse the workflow retry path rather
# than silently publishing an obviously incomplete report.
_unique_movie_count = int(df_show["movie_title"].nunique())
if _unique_movie_count < 5:
    raise RuntimeError(
        "No fresh AMC showtimes were parsed for the requested dates. "
        f"The AMC scrape was suspiciously incomplete: only {_unique_movie_count} unique movie(s) were recovered."
    )
"""


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
    ]

    updated = "".join(lines[:start]) + "".join(block_lines) + "".join(lines[end:])

    _validate_imdb_patch(updated)
    return updated, True


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
    rt_parser_replacements = 0
    rt_display_insertions = 0
    rt_desired_replacements = 0
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

        source, did_patch_rt = _replace_function(
            source,
            "rt_parse_scores",
            RT_PARSE_REPLACEMENT,
        )
        rt_parser_replacements += int(did_patch_rt)

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
        "rt_parse_scores replacement": rt_parser_replacements,
        "RT display column insertion": rt_display_insertions,
        "RT desired-column replacement": rt_desired_replacements,
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

    args.output_notebook.parent.mkdir(parents=True, exist_ok=True)
    with args.output_notebook.open("w", encoding="utf-8") as fh:
        json.dump(patched, fh, ensure_ascii=False, indent=1)
        fh.write("\n")

    print(
        "[OK] AST-scoped patch applied and validated: title normalization, "
        "non-movie filtering, IMDb variant scoring, AMC sparse-page browser fallback -> "
        f"{args.output_notebook}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
