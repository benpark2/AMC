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


def parse_combo(feed: object, theater: str, day: str, *, now: datetime | None = None) -> tuple[list[dict], dict]:
    """Translate only strongly grounded screenings to existing AMC-row shape.

    Screening fields ``venueId``, ``filmKey``, ``startsAt``, ``nightOf`` and
    ``verified_at`` are from the open-source SceneF MCP client. The film and
    venue IDs must exist in the SAME feed; missing fields fail closed.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    meta: dict = {"provider": "scenef", "region": REGION}
    if not isinstance(feed, dict) or feed.get("region") != REGION:
        return [], meta
    if feed.get("timezone") not in ("America/Los_Angeles", "US/Pacific"):
        return [], meta
    if not _recent(feed.get("data_as_of") or feed.get("generated"), now, MAX_BOARD_AGE_H):
        return [], meta
    meta["data_as_of"] = feed.get("data_as_of") or feed.get("generated")
    venues = feed.get("venues")
    films = feed.get("films")
    screenings = feed.get("screenings")
    if not (isinstance(venues, list) and isinstance(films, list) and isinstance(screenings, list)):
        return [], meta
    venue = _venue_for(theater, venues)
    if not venue or not venue.get("id"):
        return [], meta
    film_by_key = {f.get("key"): f for f in films if isinstance(f, dict) and f.get("key")}
    result: list[dict] = []
    seen: set[tuple] = set()
    verified = []
    for s in screenings:
        if not isinstance(s, dict) or s.get("venueId") != venue["id"]:
            continue
        film = film_by_key.get(s.get("filmKey"))
        if not film or not str(film.get("title") or "").strip():
            continue
        instant = _datetime(s.get("startsAt"))
        if instant is None:
            continue
        local = instant.astimezone(PACIFIC)
        # Respect the requested *calendar* date, not SceneF's 4am nightOf
        # label. Cross-midnight screenings must not shift across days.
        if local.date().isoformat() != day:
            continue
        verified_at = s.get("verified_at") or (s.get("provenance") or {}).get("lastVerifiedAt")
        if not _recent(verified_at, now, MAX_VERIFY_AGE_H):
            continue
        fresh = s.get("freshness")
        if isinstance(fresh, dict) and (
            fresh.get("status") in ("stale", "unknown")
            or fresh.get("retained") is True
            or fresh.get("source_status") in ("held", "failed", "unknown")
        ):
            continue
        confidence = s.get("confidence") or (s.get("provenance") or {}).get("confidence")
        if confidence not in ("venue-published", "corroborated", "single-source"):
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
            # SceneF does NOT certify AMC A-List eligibility. Postprocessor
            # shows an explicit status warning for SceneF-sourced combos.
            "a_list_excluded": False,
            "scenef_alist_unverified": True,
        })
    if result:
        if venue.get("scenef_url") and str(venue["scenef_url"]).startswith("https://scenef.com/"):
            meta["scenef_url"] = venue["scenef_url"]
        else:
            meta["scenef_url"] = "https://scenef.com/theater/" + quote(str(venue["id"]), safe="") + "?region=" + REGION
        meta["verified_at"] = min(verified).isoformat() if verified else None
        meta["rows"] = len(result)
    return result, meta


@lru_cache(maxsize=1)
def fetch_board() -> dict | None:
    """One modest SceneF request per Papermill attempt, no extra retries."""
    try:
        response = requests.get(
            URL, params={"region": REGION, "compact": "1"},
            headers={"User-Agent": "AMC-weekend-report/1.0 (SceneF fallback; https://scenef.com/agents)", "Accept": "application/json"},
            timeout=12,
        )
        response.raise_for_status()
        if "json" not in response.headers.get("content-type", "").lower():
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except (requests.RequestException, ValueError):
        return None


def get_combo(theater: str, day: str) -> tuple[list[dict], dict]:
    return parse_combo(fetch_board(), theater, day)


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
