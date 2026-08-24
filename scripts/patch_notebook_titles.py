#!/usr/bin/env python3
"""
Patch the AMC notebook into build/ before Papermill executes it.

This patcher is intentionally conservative:
1. Replace the notebook's candidate_title_variants() with the shared generic
   implementation in scripts/movie_titles.py.
2. Remove non-film AMC inventory before ratings/numbering/planner generation.
3. Improve IMDb candidate scoring so it considers every normalized lookup
   variant.

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

    We replace only the exact/fuzzy scoring assignments with a block that
    derives lookup_targets locally from title_variants. Because lookup_targets
    is defined immediately before it is used, there is no cross-function or
    notebook-global variable dependency.
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
        f"{indent}candidate_norms = [\n",
        f"{indent}    norm\n",
        f"{indent}    for norm in (cand['primaryNorm'], cand['originalNorm'])\n",
        f"{indent}    if norm\n",
        f"{indent}]\n",
        f"{indent}lookup_targets = title_variants.get(title) or [target]\n",
        f"{indent}exact = int(\n",
        f"{indent}    any(\n",
        f"{indent}        lookup_target in candidate_norms\n",
        f"{indent}        for lookup_target in lookup_targets\n",
        f"{indent}        if lookup_target\n",
        f"{indent}    )\n",
        f"{indent})\n",
        f"{indent}fuzz_score = max(\n",
        f"{indent}    (\n",
        f"{indent}        fuzz.token_set_ratio(candidate_norm, lookup_target)\n",
        f"{indent}        for candidate_norm in candidate_norms\n",
        f"{indent}        for lookup_target in lookup_targets\n",
        f"{indent}        if lookup_target\n",
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
    for name in ("candidate_norms", "lookup_targets"):
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
        "non-movie filtering, IMDb variant scoring -> "
        f"{args.output_notebook}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
