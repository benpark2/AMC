#!/usr/bin/env python3
"""
Patch the AMC notebook into build/ before Papermill executes it.

This file contains no movie-specific logic. The actual title rules live in
scripts/movie_titles.py and are generic presentation/event patterns.

The patch makes three narrow changes:
1. Use the shared generic candidate_title_variants() implementation.
2. Remove non-film AMC inventory before metadata/numbering/planner generation.
3. Make IMDb candidate ranking score against all normalized lookup variants.

The script fails if the notebook structure no longer matches expectations,
which is safer than silently producing a partially patched report.
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
"""

IMDB_TARGET_OLD = "        target = normalize_title_for_match(title)\n"
IMDB_TARGET_NEW = (
    "        # Score against every normalized generic lookup variant, not the "
    "raw AMC presentation title.\\n"
    "        targets = title_variants.get(title) or "
    "[normalize_title_for_match(title)]\\n"
)

IMDB_SCORE_OLD = """            exact = int(target in {cand['primaryNorm'], cand['originalNorm']})
            fuzz_score = max(
                fuzz.token_set_ratio(cand['primaryNorm'], target) if cand['primaryNorm'] else 0,
                fuzz.token_set_ratio(cand['originalNorm'], target) if cand['originalNorm'] else 0,
            )
"""

IMDB_SCORE_NEW = """            candidate_norms = [
                norm for norm in (cand['primaryNorm'], cand['originalNorm']) if norm
            ]
            exact = int(
                any(target in candidate_norms for target in targets if target)
            )
            fuzz_score = max(
                (
                    fuzz.token_set_ratio(candidate_norm, target)
                    for candidate_norm in candidate_norms
                    for target in targets
                    if target
                ),
                default=0,
            )
"""


def _source_text(cell: dict) -> str:
    source = cell.get("source", [])
    if isinstance(source, list):
        return "".join(source)
    return str(source)


def _set_source(cell: dict, source: str) -> None:
    cell["source"] = source.splitlines(keepends=True)


def _replace_function(
    source: str,
    function_name: str,
    replacement: str,
) -> tuple[str, bool]:
    """Replace one top-level Python function using AST line boundaries."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, False

    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    ]
    if not nodes:
        return source, False
    if len(nodes) != 1:
        raise RuntimeError(
            f"Expected one {function_name}() in a cell; found {len(nodes)}."
        )

    node = nodes[0]
    lines = source.splitlines(keepends=True)
    start = node.lineno - 1
    end = node.end_lineno

    replacement_text = replacement.rstrip() + "\n"
    return "".join(lines[:start]) + replacement_text + "".join(lines[end:]), True


def patch_notebook(notebook: dict) -> dict:
    function_replacements = 0
    df_show_insertions = 0
    imdb_target_replacements = 0
    imdb_score_replacements = 0

    for cell in notebook.get("cells", []):
        if cell.get("cell_type") != "code":
            continue

        source = _source_text(cell)

        source, did_replace = _replace_function(
            source,
            "candidate_title_variants",
            FUNCTION_WRAPPER,
        )
        function_replacements += int(did_replace)

        marker_count = source.count(DF_SHOW_MARKER)
        if marker_count:
            if marker_count != 1:
                raise RuntimeError(
                    f"Expected one df_show marker in a cell; found {marker_count}."
                )
            source = source.replace(DF_SHOW_MARKER, DF_SHOW_INSERT, 1)
            df_show_insertions += 1

        count = source.count(IMDB_TARGET_OLD)
        if count:
            if count != 1:
                raise RuntimeError(
                    f"Expected one IMDb target line in a cell; found {count}."
                )
            source = source.replace(IMDB_TARGET_OLD, IMDB_TARGET_NEW, 1)
            imdb_target_replacements += 1

        count = source.count(IMDB_SCORE_OLD)
        if count:
            if count != 1:
                raise RuntimeError(
                    f"Expected one IMDb scoring block in a cell; found {count}."
                )
            source = source.replace(IMDB_SCORE_OLD, IMDB_SCORE_NEW, 1)
            imdb_score_replacements += 1

        _set_source(cell, source)

    expected = {
        "candidate_title_variants": function_replacements,
        "df_show insertion": df_show_insertions,
        "IMDb target": imdb_target_replacements,
        "IMDb scoring": imdb_score_replacements,
    }
    failures = {name: count for name, count in expected.items() if count != 1}
    if failures:
        detail = ", ".join(f"{name}={count}" for name, count in failures.items())
        raise RuntimeError(
            "Notebook structure did not match the tested AMC_Scraper_5.ipynb; "
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
        "[OK] Patched generic title normalization, non-movie filtering, "
        f"and IMDb variant scoring -> {args.output_notebook}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
