"""The package path classifier agrees with the census it was ported from.

The census is loaded by file path rather than imported, because it is a study
script that lives under ``docs/`` and is not a package module. Agreement is
asserted path by path against the census's own functions, so the fixture list
is the only thing this test states and the answers come from the source of
truth.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from reckon import path_classes

REPO_ROOT = Path(__file__).resolve().parents[1]
CENSUS_PATH = (
    REPO_ROOT
    / "docs"
    / "research"
    / "data"
    / "crew-pattern-review"
    / "velocity"
    / "census.py"
)

SPA_DIRS = ("docs/ui/", "docs/_ui/", "docs/_shared/")
SPA_EXTENSIONS = (".js", ".jsx", ".css")


def _load_census():
    spec = importlib.util.spec_from_file_location("velocity_census", CENSUS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


census = _load_census()


SOURCE_PATHS = (
    "reckon/velocity.py",
    "reckon/crew/dispatch.py",
    "src/engine/main.f90",
    "CMakeLists.txt",
)
TEST_PATHS = (
    "tests/test_velocity.py",
    "reckon/crew/tests/helpers.py",
    "test_smoke.py",
)
PLAN_PATHS = (
    "docs/plans/the-fleet-reports-its-velocity.html",
    "docs/evidence/archive/the-fleet-reports-its-velocity-landed.html",
    "docs/research/orchestrator-crew-pattern-review.html",
)
FIGURE_PATHS = (
    "docs/figures/velocity/week.png",
    "docs/figures/velocity/trend.svg",
    "assets/chart.jpeg",
)
STATE_PATHS = (
    "docs/state/reckon/crew.json",
    "docs/state/nova/index.json",
)
OTHER_PATHS = (
    "docs/index.json",
    "data/velocity/summary.json",
)
SPA_PATHS = tuple(
    directory + "widget" + extension
    for directory in SPA_DIRS
    for extension in SPA_EXTENSIONS
)


# One fixture list: every entry is classified by the census, and the package
# classifier must return the same name for the same path.
FIXTURE_PATHS = (
    SOURCE_PATHS
    + TEST_PATHS
    + PLAN_PATHS
    + FIGURE_PATHS
    + STATE_PATHS
    + OTHER_PATHS
    + SPA_PATHS
)


def test_the_fixture_list_covers_every_census_class():
    counts = {}
    for path in FIXTURE_PATHS:
        name = census.file_class(path)
        counts[name] = counts.get(name, 0) + 1
    assert set(counts) == set(census.CLASSES)
    for name in census.CLASSES:
        assert counts[name] >= 2, f"{name} has {counts[name]} fixture paths"


def test_the_spa_grid_is_present():
    for directory in SPA_DIRS:
        for extension in SPA_EXTENSIONS:
            matches = [
                path
                for path in SPA_PATHS
                if path.startswith(directory) and path.endswith(extension)
            ]
            assert matches, f"no {extension} path under {directory}"
            assert path_classes.path_class(matches[0]) == "source"


@pytest.mark.parametrize("path", FIXTURE_PATHS)
def test_file_class_matches_the_census(path):
    assert path_classes.file_class(path) == census.file_class(path)


@pytest.mark.parametrize("path", FIXTURE_PATHS)
def test_path_class_matches_the_census(path):
    assert path_classes.path_class(path) == census.path_class(path)
