"""
Generic AMC title normalization for the weekend movie report.

The scraper keeps AMC's display title unchanged while deriving a cleaner
metadata lookup title for IMDb, Rotten Tomatoes, Wikipedia, and trailer search.

There are intentionally NO movie-specific title mappings in this module.
Everything is based on reusable presentation/event patterns. Non-film inventory
such as private-theatre rentals is removed before metadata lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Iterable


_WHITESPACE_RE = re.compile(r"\s+")

# Non-film inventory exposed by AMC in the same listing stream.
_NON_MOVIE_PATTERNS = (
    re.compile(
        r"private\s+theat(?:re|er)\s+rental(?:\s*[-–—:]\s*.*)?",
        re.IGNORECASE,
    ),
)

# AMC program codes such as "(HPD26)". We deliberately do not strip arbitrary
# parentheses because parentheses may be part of a legitimate movie title.
_AMC_CODE_SUFFIX_RE = re.compile(r"\s*\(([A-Z]{2,}\d{2,})\)\s*$")

# Suffixes are peeled one at a time. Iteration is essential because AMC can
# stack qualifiers, e.g. "40th Anniversary - Studio Ghibli Fest 2026".
_SUFFIX_PATTERNS = (
    # Branded repertory/festival suffix after an explicit separator, e.g.
    # "- <program name> Fest 2026". The program/brand name is not hard-coded.
    re.compile(
        r"\s*[-–—:]\s*"
        r"[A-Za-z0-9&'’.,+\- ]{1,80}"
        r"\b(?:fest|festival|series|showcase)"
        r"(?:\s+\d{4})?\s*$",
        re.IGNORECASE,
    ),

    # Accessibility / special-presentation labels.
    re.compile(
        r"\s*(?:[-–—:]\s*)?sensory\s+friendly"
        r"(?:\s+(?:screening|film))?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s*(?:[-–—:]\s*)?open\s+caption(?:ed)?"
        r"(?:\s*\(on[- ]screen\s+subtitles\))?"
        r"(?:\s+screening)?\s*$",
        re.IGNORECASE,
    ),

    # Q&A and event labels.
    re.compile(
        r"\s*(?:[-–—:]\s*)?(?:special\s+)?in[-\s]?person\s+q\s*&\s*a\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"\s*(?:[-–—:]\s*)?q\s*&\s*a\s*$", re.IGNORECASE),
    re.compile(
        r"\s*(?:[-–—:]\s*)?early\s+access(?:\s+event)?\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"\s*(?:[-–—:]\s*)?sneak\s+peek\s*$", re.IGNORECASE),
    re.compile(r"\s*(?:[-–—:]\s*)?fan\s+event\s*$", re.IGNORECASE),
    re.compile(r"\s*(?:[-–—:]\s*)?opening\s+night\s*$", re.IGNORECASE),
    re.compile(r"\s*(?:[-–—:]\s*)?live\s+event\s*$", re.IGNORECASE),
    re.compile(r"\s*(?:[-–—:]\s*)?double\s+feature\s*$", re.IGNORECASE),
    re.compile(r"\s*(?:[-–—:]\s*)?re-?release\s*$", re.IGNORECASE),
    re.compile(
        r"\s*(?:[-–—:]\s*)?the\s+imax\s+experience\s*$",
        re.IGNORECASE,
    ),

    # Generic anniversary suffix. This comes after event/program suffixes so
    # stacked labels can collapse all the way to the base title.
    re.compile(
        r"(?:\s*[-–—:]\s*|\s+)"
        r"\d{1,3}(?:st|nd|rd|th)\s+anniversary\s*$",
        re.IGNORECASE,
    ),
)

_ANNIVERSARY_RE = re.compile(
    r"\b(\d{1,3})(?:st|nd|rd|th)\s+anniversary\b",
    re.IGNORECASE,
)

# Capture an explicit year from a trailing "Fest 2026"-style program label.
# This is intentionally generic enough to be reused for future festival names.
_EVENT_YEAR_RE = re.compile(
    r"(?:fest|festival|series|showcase)\s+(\d{4})\b",
    re.IGNORECASE,
)


def clean_amc_title(title: str) -> str:
    """Normalize whitespace without changing the visible wording."""
    value = (title or "").replace("\u00a0", " ").replace("\u202f", " ")
    return _WHITESPACE_RE.sub(" ", value).strip()


def is_non_movie_title(title: str) -> bool:
    """Return True for AMC inventory rows that are not actual films."""
    value = clean_amc_title(title)
    return any(pattern.fullmatch(value) for pattern in _NON_MOVIE_PATTERNS)


def _strip_one_recognized_suffix(value: str) -> str:
    """Remove at most one known AMC presentation suffix."""
    code_stripped = _AMC_CODE_SUFFIX_RE.sub("", value).strip(" -–—:")
    if code_stripped and code_stripped != value:
        return clean_amc_title(code_stripped)

    for pattern in _SUFFIX_PATTERNS:
        stripped = pattern.sub("", value).strip(" -–—:")
        stripped = clean_amc_title(stripped)
        if stripped and stripped != value:
            return stripped

    return value


def canonical_movie_title(title: str) -> str:
    """
    Return a conservative metadata lookup title.

    A safety cap prevents a malformed future regex from creating an infinite
    loop if new suffix rules are added later.
    """
    value = clean_amc_title(title)

    for _ in range(12):
        updated = _strip_one_recognized_suffix(value)
        if updated == value:
            break
        value = updated

    return value


@dataclass(frozen=True)
class MovieTitleInfo:
    """Generic facts derived from an AMC display title."""

    display_title: str
    canonical_title: str
    anniversary_years: int | None
    event_year: int | None
    inferred_release_year: int | None


def analyze_movie_title(
    title: str,
    *,
    reference_year: int | None = None,
) -> MovieTitleInfo:
    """
    Infer an original release year when an anniversary provides enough data.

    Example:
        "<movie> 25th Anniversary" in 2026 -> likely original year 2001.

    If the AMC string itself contains a festival/program year, that year is
    preferred over the machine's clock.
    """
    display = clean_amc_title(title)
    canonical = canonical_movie_title(display)

    anniversary_match = _ANNIVERSARY_RE.search(display)
    anniversary_years = (
        int(anniversary_match.group(1)) if anniversary_match else None
    )

    event_match = _EVENT_YEAR_RE.search(display)
    event_year = int(event_match.group(1)) if event_match else None

    ref_year = reference_year or date.today().year
    basis_year = event_year or ref_year
    inferred_release_year = None

    if anniversary_years is not None:
        candidate_year = basis_year - anniversary_years
        if 1888 <= candidate_year <= ref_year:
            inferred_release_year = candidate_year

    return MovieTitleInfo(
        display_title=display,
        canonical_title=canonical,
        anniversary_years=anniversary_years,
        event_year=event_year,
        inferred_release_year=inferred_release_year,
    )


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    for value in values:
        cleaned = clean_amc_title(value)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)

    return out


def _generic_spelling_variants(title: str) -> list[str]:
    """
    Produce only mechanical spelling/punctuation variants.

    No movie names are encoded here. These variants help metadata providers
    disagreeing about "&" vs "and", apostrophe style, or AMC censor asterisks.
    """
    value = clean_amc_title(title)
    variants = [value]

    # Curly/straight apostrophe normalization.
    variants.append(value.replace("’", "'"))
    variants.append(value.replace("'", "’"))

    # "&" and "and" commonly differ between ticketing and metadata providers.
    if "&" in value:
        variants.append(re.sub(r"\s*&\s*", " and ", value))
    if re.search(r"\band\b", value, flags=re.IGNORECASE):
        variants.append(re.sub(r"\band\b", "&", value, flags=re.IGNORECASE))

    # AMC sometimes censors letters with asterisks. Removing only the asterisks
    # gives fuzzy matching/search a useful generic variant without guessing the
    # hidden letters or encoding any specific profanity/title.
    if "*" in value:
        variants.append(value.replace("*", ""))

    return _dedupe(variants)


def candidate_title_variants(title: str) -> list[str]:
    """
    Return generic metadata lookup variants, most useful first.

    Canonical variants come before the raw AMC display string so IMDb/RT search
    starts with the actual movie title rather than presentation marketing text.
    """
    display = clean_amc_title(title)
    if not display:
        return []

    canonical = canonical_movie_title(display)

    values: list[str] = []
    values.extend(_generic_spelling_variants(canonical))

    if display.casefold() != canonical.casefold():
        values.extend(_generic_spelling_variants(display))

    return _dedupe(values)


def wikipedia_title_candidates(
    title: str,
    *,
    reference_year: int | None = None,
) -> list[str]:
    """
    Build generic English-Wikipedia page-title candidates.

    Anniversary-derived release years come first because they disambiguate
    repertory releases. The current release-year heuristic is dynamic rather
    than hard-coded to any calendar year.
    """
    ref_year = reference_year or date.today().year
    info = analyze_movie_title(title, reference_year=ref_year)

    # Exact Wikipedia titles are most likely to use the canonical wording.
    # Mechanical variants are included only after that.
    names = candidate_title_variants(info.canonical_title)
    values: list[str] = []

    for name in names:
        if info.inferred_release_year is not None:
            values.append(f"{name} ({info.inferred_release_year} film)")

        for year in (ref_year, ref_year - 1, ref_year + 1):
            values.append(f"{name} ({year} film)")

        values.append(f"{name} (film)")
        values.append(name)

    return _dedupe(values)


def search_terms_for_title(title: str) -> str:
    """
    Build a provider-neutral search string from a title.

    Punctuation is reduced but words/numbers are preserved. Censor asterisks
    disappear rather than being interpreted as wildcard operators.
    """
    value = canonical_movie_title(title)
    value = value.replace("&", " and ")
    value = value.replace("*", "")
    value = re.sub(r"[^\w']+", " ", value, flags=re.UNICODE)
    return clean_amc_title(value)
