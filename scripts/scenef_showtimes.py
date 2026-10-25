"""Conservative, key-free SceneF showtime fallback for AMC's unavailable schedules.

SceneF's public REST contract (https://scenef.com/agents) publishes regional
``venues``, ``films`` and ``screenings`` arrays. Its licensed-feed chain data
may be stale or differ from AMC's own inventory, so this module never calls it
an AMC scrape and refuses ambiguous theater identities or stale evidence.

One bounded request per notebook process is shared by all theater/date combos.
Network behavior is deliberately untested locally: use fixtures for offline
regressions and verify the endpoint from the GitHub runner after deployment.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import urlparse, quote
from zoneinfo import ZoneInfo
import re
import unicodedata
import json
from pathlib import Path

import requests

URL = "https://scenef.com/api/listings"
REGION = "orangecounty"
PACIFIC = ZoneInfo("America/Los_Angeles")
MAX_BOARD_AGE_H = 24
MAX_VERIFY_AGE_H = 48


def _norm(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", normalized.lower().replace("@", " at ")))


def _theater_key(value: object) -> str:
    """Strip a chain prefix only for matching; never use fuzzy containment."""
    n = _norm(value)
    return re.sub(r"^amc\s+", "", n).strip()


def _theater_variants(value: object) -> set[str]:
    full = _theater_key(value)
    variants = {full}
    # Screen counts appear in AMC names but not in some independent feeds.
    variants.add(re.sub(r"\s+\d+$", "", full).strip())
    # The chain sometimes drops an AMC location's marketing suffix in the
    # independent feed (e.g. "at Plaza"). Still require a UNIQUE AMC venue.
    short = re.sub(r"\s+at\s+.+$", "", full).strip()
    if short and re.search(r"\s+\d+$", short):
        variants.add(short)
    return {x for x in variants if x}


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        d = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if d.tzinfo is None:
            return None
        return d.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _recent(value: object, now: datetime, hours: float) -> bool:
    parsed = _datetime(value)
    if parsed is None:
        return False
    age = (now - parsed).total_seconds()
    return -300 <= age <= hours * 3600


def _is_amc_venue(venue: dict) -> bool:
    """Reject non-AMC theaters sharing a location or a short ambiguous name."""
    name_bits = (venue.get("name"), venue.get("short"), venue.get("id"))
    if any("amc" in _norm(s).split() for s in name_bits):
        return True
    for k in ("website", "platform"):
        raw = str(venue.get(k) or "").lower()
        if k == "platform" and re.search(r"\bamc\b", raw):
            return True
        if k == "website" and urlparse(raw).hostname in {"amctheatres.com", "www.amctheatres.com"}:
            return True
    return False


def _venue_for(theater: str, venues: list[dict]) -> dict | None:
    # Exact names/short names after generic AMC-prefix normalization. Don't
    # silently pick the first candidate when two sites have similar labels.
    requested = _theater_key(theater)
    candidates = [v for v in venues if isinstance(v, dict) and _is_amc_venue(v)]
    exact = [v for v in candidates if requested in {
        _theater_key(v.get("name")), _theater_key(v.get("short"))
    }]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    approx = [v for v in candidates if any(
        requested in _theater_variants(v.get(k)) or
        _theater_key(v.get(k)) in _theater_variants(theater)
        for k in ("name", "short") if v.get(k)
    )]
    return approx[0] if len(approx) == 1 else None


# Diagnostics are written as build artifacts, not notebook stdout, so retries
# can expose rejection reasons in Actions without publishing them in the report.
# Values are compact counts/reason codes, never response bodies or credentials.
FETCH_DIAGNOSTIC = Path("build/scenef_fetch_diagnostic.json")
COMBO_DIAGNOSTIC_DIR = Path("build/scenef_combo_diagnostics")


def _save_diagnostic(path: Path, values: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values, sort_keys=True), encoding="utf-8")
    except OSError:
        # Diagnostics must never alter the showtime retrieval decision.
        pass


def _combo_diagnostic_path(theater: str, day: str) -> Path:
    # The same normalized combination key is used for all updates to its
    # decision; do not include arbitrary vendor content in the file path.
    slug = re.sub(r"[^a-z0-9]+", "-", str(theater).lower()).strip("-")
    return COMBO_DIAGNOSTIC_DIR / f"{slug}-{day}.json"


def _record_combo_diagnostic(theater: str, day: str, values: dict) -> None:
    path = _combo_diagnostic_path(theater, day)
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(current, dict):
            current = {}
    except (ValueError, OSError):
        current = {}
    current.update({"theater": str(theater), "date": str(day)})
    current.update(values)
    _save_diagnostic(path, current)


def record_combo_decision(theater: str, day: str, *, accepted: bool,
                          reason: str, candidate: list[dict],
                          runner: list[dict], previous: list[dict]) -> None:
    """Log coverage/recency decisions, including rejected but nonempty feeds.

    Distinct film and movie/time counts avoid misleading duplicates from
    screening formats. This function intentionally never changes eligibility.
    """
    summary = {}
    for name, rows in (("candidate", candidate), ("runner", runner), ("previous", previous)):
        films, slots = _coverage(rows)
        summary[f"{name}_films"] = len(films)
        summary[f"{name}_slots"] = len(slots)
    _record_combo_diagnostic(theater, day, {
        "decision": "accepted" if accepted else "rejected",
        "decision_reason": reason,
        **summary,
    })


def parse_combo(feed: object, theater: str, day: str, *,
                now: datetime | None = None,
                diagnostic: dict | None = None) -> tuple[list[dict], dict]:
    """Validate SceneF rows without weakening the v48 theater/date checks.

    ``diagnostic`` is an optional output dict for reason codes/counters. The
    original return shape remains unchanged for existing callers and tests.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    meta: dict = {"provider": "scenef", "region": REGION}
    diag = diagnostic if diagnostic is not None else {}

    def reject(reason: str) -> tuple[list[dict], dict]:
        diag["reason"] = reason
        return [], meta

    if not isinstance(feed, dict):
        return reject("feed_unavailable")
    if feed.get("region") != REGION:
        return reject("region_mismatch")
    if feed.get("timezone") not in ("America/Los_Angeles", "US/Pacific"):
        return reject("timezone_mismatch")
    board_time = feed.get("data_as_of") or feed.get("generated")
    if not _recent(board_time, now, MAX_BOARD_AGE_H):
        return reject("board_stale_or_undated")
    meta["data_as_of"] = board_time
    venues, films, screenings = (feed.get(k) for k in ("venues", "films", "screenings"))
    if not (isinstance(venues, list) and isinstance(films, list) and isinstance(screenings, list)):
        return reject("feed_schema_missing_arrays")
    diag.update({"venues": len(venues), "films": len(films), "screenings": len(screenings)})

    venue = _venue_for(theater, venues)
    if not venue or not venue.get("id"):
        # Distinguish missing AMC venues from ambiguous approximate name
        # matches, without changing the strict venue selector itself.
        requested = _theater_key(theater)
        amc = [v for v in venues if isinstance(v, dict) and _is_amc_venue(v)]
        exact = [v for v in amc if requested in {
            _theater_key(v.get("name")), _theater_key(v.get("short"))
        }]
        approx = [v for v in amc if any(
            requested in _theater_variants(v.get(k)) or
            _theater_key(v.get(k)) in _theater_variants(theater)
            for k in ("name", "short") if v.get(k)
        )]
        diag.update({"amc_venues": len(amc), "exact_matches": len(exact),
                     "approx_matches": len(approx)})
        return reject("theater_ambiguous" if len(exact) > 1 or len(approx) > 1
                      else "theater_not_found")

    film_by_key = {f.get("key"): f for f in films if isinstance(f, dict) and f.get("key")}
    result: list[dict] = []
    seen: set[tuple] = set()
    verified = []
    diag.update({"venue_screenings": 0, "date_screenings": 0, "invalid_film_or_time": 0,
                 "unverified_screenings": 0, "stale_screenings": 0,
                 "disputed_screenings": 0, "valid_screenings": 0})
    available_dates: set[str] = set()
    for s in screenings:
        if not isinstance(s, dict) or s.get("venueId") != venue["id"]:
            continue
        diag["venue_screenings"] += 1
        film = film_by_key.get(s.get("filmKey"))
        instant = _datetime(s.get("startsAt"))
        if not film or not str(film.get("title") or "").strip() or instant is None:
            diag["invalid_film_or_time"] += 1
            continue
        local = instant.astimezone(PACIFIC)
        available_dates.add(local.date().isoformat())
        # Local start date, not nightOf, controls the requested calendar day.
        if local.date().isoformat() != day:
            continue
        diag["date_screenings"] += 1
        provenance = s.get("provenance") if isinstance(s.get("provenance"), dict) else {}
        verified_at = s.get("verified_at") or provenance.get("lastVerifiedAt")
        if not _recent(verified_at, now, MAX_VERIFY_AGE_H):
            diag["unverified_screenings"] += 1
            continue
        fresh = s.get("freshness")
        if isinstance(fresh, dict) and (
            fresh.get("status") in ("stale", "unknown")
            or fresh.get("retained") is True
            or fresh.get("source_status") in ("held", "failed", "unknown")
        ):
            diag["stale_screenings"] += 1
            continue
        confidence = s.get("confidence") or provenance.get("confidence")
        if confidence not in ("venue-published", "corroborated", "single-source"):
            diag["disputed_screenings"] += 1
            continue
        title = str(film["title"]).strip()
        time_txt = local.strftime("%I:%M %p").lstrip("0").lower()
        key = (_norm(title), time_txt)
        if key in seen:
            continue
        seen.add(key)
        verified.append(_datetime(verified_at))
        tags = s.get("tags") if isinstance(s.get("tags"), list) else []
        runtime = film.get("runtimeMin")
        result.append({
            "movie_title": title,
            "theatre": theater,
            "show_date": day,
            "show_time": time_txt,
            "format_label": "; ".join(str(t) for t in tags if isinstance(t, str)) or None,
            "runtime_min": runtime if isinstance(runtime, int) and 0 < runtime < 500 else None,
            "a_list_excluded": False,
            "scenef_alist_unverified": True,
        })
    diag["valid_screenings"] = len(result)
    diag["available_dates"] = sorted(available_dates)[:8]
    if result:
        if venue.get("scenef_url") and str(venue["scenef_url"]).startswith("https://scenef.com/"):
            meta["scenef_url"] = venue["scenef_url"]
        else:
            meta["scenef_url"] = "https://scenef.com/theater/" + quote(str(venue["id"]), safe="") + "?region=" + REGION
        meta["verified_at"] = min(verified).isoformat() if verified else None
        meta["rows"] = len(result)
        diag["reason"] = "valid_candidate"
    elif not diag["venue_screenings"]:
        diag["reason"] = "theater_has_no_screenings"
    elif not diag["date_screenings"]:
        diag["reason"] = "requested_date_absent" if available_dates else "screenings_invalid_film_or_time"
    else:
        diag["reason"] = "date_screenings_rejected_by_validation"
    return result, meta


@lru_cache(maxsize=1)
def fetch_board() -> dict | None:
    """One SceneF request per Papermill attempt; record why it failed."""
    diag = {"reason": "request_not_completed"}
    try:
        response = requests.get(
            URL, params={"region": REGION, "compact": "1"},
            headers={"User-Agent": "AMC-weekend-report/1.0 (SceneF fallback; https://scenef.com/agents)", "Accept": "application/json"},
            timeout=12,
        )
        diag["http_status"] = response.status_code
        response.raise_for_status()
        if "json" not in response.headers.get("content-type", "").lower():
            diag["reason"] = "non_json_response"
            return None
        try:
            data = response.json()
        except ValueError:
            diag["reason"] = "invalid_json"
            return None
        if not isinstance(data, dict):
            diag["reason"] = "invalid_json_structure"
            return None
        diag["reason"] = "ok"
        return data
    except requests.Timeout:
        diag["reason"] = "request_timeout"
        return None
    except requests.HTTPError:
        diag["reason"] = "http_error"
        return None
    except requests.RequestException:
        # Do not print exception strings: request/response details sometimes
        # embed authentication or query information.
        diag["reason"] = "network_error"
        return None
    finally:
        _save_diagnostic(FETCH_DIAGNOSTIC, diag)


def get_combo(theater: str, day: str) -> tuple[list[dict], dict]:
    diagnostic = {}
    board = fetch_board()
    rows, meta = parse_combo(board, theater, day, diagnostic=diagnostic)
    if board is None:
        try:
            fetch = json.loads(FETCH_DIAGNOSTIC.read_text(encoding="utf-8"))
            diagnostic["reason"] = fetch.get("reason") or "feed_unavailable"
        except (OSError, ValueError):
            pass
    _record_combo_diagnostic(theater, day, diagnostic)
    return rows, meta


def _coverage(rows: list[dict]) -> tuple[set[str], set[tuple[str, str]]]:
    """Count distinct film identities and movie/clock slots, not raw records.

    AMC may return different formats or duplicate internal IDs for one showing.
    Counting records would overvalue duplicates and let a sparse replacement
    masquerade as better coverage. This is an intentionally conservative check.
    """
    films: set[str] = set()
    slots: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _norm(row.get("movie_title"))
        clock = _norm(row.get("show_time"))
        if title and clock:
            films.add(title)
            slots.add((title, clock))
    return films, slots


def improves_coverage(candidate: list[dict], previous: list[dict]) -> bool:
    """Prefer SceneF only if it adds valid movie/showtime information.

    If the previous report has data, require a strict *superset* of the known
    semantic slots. We never replace a full AMC schedule with fewer films or
    showtimes merely because a third-party feed has a newer build timestamp.
    Identical/equal coverage stays cached. This can reject a legitimate last-
    minute schedule change, deliberately favoring safety for this optional
    third-party fallback rather than pretending SceneF is authoritative.
    """
    new_films, new_slots = _coverage(candidate)
    old_films, old_slots = _coverage(previous)
    if not new_films or not new_slots:
        return False
    if old_slots:
        return old_films <= new_films and old_slots < new_slots
    # Reject isolated one-off records on a totally empty cache; a partial
    # third-party feed is not reliable evidence of the entire AMC program.
    return len(new_films) >= 2 and len(new_slots) >= 3
