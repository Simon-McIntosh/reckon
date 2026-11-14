"""Derive a delivered run's scope from the diff rather than a prediction.

A run was armed, measured and reviewed from its declared write paths, which are
a prediction of what it would change. A run dispatched with no write paths has
no prediction to read, and a declared run's prediction can fall short of its
diff. The diff is the fact both kinds of run deliver, and it answers the three
questions the prediction used to answer:

* which of the run's own test files the suite pair runs, and therefore the
  armed command;
* which test files import each module the run changed, the importer population;
* which production neighbourhood the change sits in, the review scope.

A run that declared write paths keeps the answer it had, and a run that
declared none gets the answer the diff gives. A declared run whose diff fills
its scope therefore derives exactly what a no-write-paths run with the same
diff derives, and the two are compared on that equality.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


def _as_paths(values: Iterable[Any]) -> list[str]:
    """The non-empty string forms of ``values``, in order, de-duplicated."""
    seen: dict[str, None] = {}
    for value in values:
        text = str(value).strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


def declared_scope(record: Mapping[str, Any]) -> list[str]:
    """The node's declared write paths, repository-relative, or empty.

    Read through promotion's own reader so the derivation and the armed
    command compare the same set of paths they would have been resolved from.
    """
    from reckon.crew.promotion_records import _repository_write_paths

    return _repository_write_paths(record)


def effective_scope(record: Mapping[str, Any], changed: Iterable[str]) -> list[str]:
    """The paths a run's suite, population and review are derived from.

    A declared run keeps its declared paths, which is the answer it had before
    the derivation existed. A run that declared none takes the paths its diff
    then changed, so a prediction that named nothing stops emptying its
    command, population and neighbourhood.
    """
    declared = declared_scope(record)
    if declared:
        return declared
    return _as_paths(changed)


def changed_test_files(
    record: Mapping[str, Any],
    changed: Iterable[str],
    tree: Any,
    *,
    present: Sequence[str] | None = None,
) -> list[str]:
    """The run's own test files the suite pair runs.

    A declared run's test files are the ones its scope names, exactly as the
    armed command resolved them before. A run with none takes the test files its
    diff changed, because a node with no fence still delivered the tests its
    done-when asked for. Only files pytest can collect and that exist in the
    measured tree are named, so a fixture is not handed to the runner and a
    deleted test is not passed to it.
    """
    from reckon.crew.node import is_collectible_test_path
    from reckon.crew.promotion_records import _node_test_files

    named = _node_test_files(record)
    if named:
        return named
    candidates = [
        path
        for path in _as_paths(changed)
        if is_collectible_test_path(path)
        and (path in present if present is not None else (Path(tree) / path).is_file())
    ]
    return sorted(candidates)


def review_neighbourhood(
    record: Mapping[str, Any], changed: Iterable[str], tree: Any, revision: str
) -> dict[str, list[str]]:
    """The production neighbourhood a delivered run's change sits in.

    The named paths are the run's product source: its declared product paths
    when it declared any, otherwise the product modules the diff changed. The
    modules are the ones one import away in either direction at the revision
    measured, read by the same reachability instrument a code review reads. A
    change touching a non-Python path has no production neighbourhood, so both
    lists are empty rather than naming a module that does not exist.
    """
    paths = [
        path
        for path in effective_scope(record, changed)
        if str(path).endswith(".py") and Path(path).parts[:1] != ("tests",)
    ]
    if not paths:
        return {"paths": [], "modules": []}
    from reckon.served_code import neighbourhood

    try:
        modules = sorted(neighbourhood(tree, revision, paths))
    except (OSError, ValueError):
        modules = []
    return {"paths": paths, "modules": modules}


def derive(
    record: Mapping[str, Any],
    *,
    changed: Iterable[str],
    tree: Any,
    revision: str = "",
) -> dict[str, Any]:
    """The armed command, importer population and review neighbourhood of a diff.

    One call so no two callers derive a run's scope differently. The armed
    command is resolved by promotion's own substitution over the effective
    scope, so the command this derivation reports is the command promotion
    would run, not a second spelling of it.
    """
    changed_paths = _as_paths(changed)
    scope = effective_scope(record, changed_paths)
    effective = dict(record)
    node = dict(record.get("node") or {})
    node["write_paths"] = list(scope)
    effective["node"] = node

    from reckon.crew.landing_measure import importer_population
    from reckon.crew.promotion_records import _scoped_suite_command

    command = ""
    if revision:
        command = _scoped_suite_command(effective, revision=revision)
    else:
        command = _scoped_suite_command(effective)
    population = importer_population(changed_paths, tree)
    return {
        "armed_command": command,
        "test_files": changed_test_files(effective, changed_paths, tree),
        "importer_population": list(population),
        "review_neighbourhood": review_neighbourhood(
            effective, changed_paths, tree, revision
        ),
    }
