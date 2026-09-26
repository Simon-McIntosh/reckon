"""Classify a landed path into one of six line classes.

The six classes partition every path a promotion can touch, so a reader can
count landed lines by what the work was rather than by directory:

``source``      runtime and product code
``tests``       test files
``plan_evidence_research_html``  plan, evidence and research documents
``figures``     images and plotted artifacts
``docs_state``  committed crew and project state
``other``       everything else, reported on its own

``path_class`` is the public entry point and adds the one thing the classes
cannot express on their own: the planning SPA is product code even though it
lives under ``docs/``.
"""

from __future__ import annotations

from pathlib import Path

SPA_PREFIXES = ("docs/ui/", "docs/_ui/", "docs/_shared/")
SPA_SUFFIXES = frozenset({".js", ".jsx", ".css"})
FIGURE_SUFFIXES = frozenset(
    {".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".mp4"}
)
PLAN_HTML_PREFIXES = ("docs/plans/", "docs/evidence/", "docs/research/")
OTHER_PREFIXES = ("docs/", "data/", "artifacts/", "results/")
SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".cmake",
        ".hpp",
        ".f",
        ".f90",
        ".f95",
        ".f03",
        ".for",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".rs",
        ".cu",
        ".cuh",
        ".sh",
        ".bash",
        ".css",
        ".html",
    }
)
TEST_PARTS = frozenset({"tests", "test", "testing"})


def file_class(path):
    """Class a path by what it is, without the SPA override."""
    lower = path.lower()
    if lower.startswith("docs/state/"):
        return "docs_state"
    if lower.startswith("docs/figures/") or Path(lower).suffix in FIGURE_SUFFIXES:
        return "figures"
    if lower.endswith(".html") and lower.startswith(PLAN_HTML_PREFIXES):
        return "plan_evidence_research_html"
    if any(part in TEST_PARTS for part in lower.split("/")) or Path(
        lower
    ).name.startswith("test_"):
        return "tests"
    if lower.startswith(OTHER_PREFIXES):
        return "other"
    if Path(lower).suffix in SOURCE_SUFFIXES or Path(lower).name == "cmakelists.txt":
        return "source"
    return "other"


def path_class(path):
    """Class a path as the velocity view counts it, SPA override included."""
    if path.startswith(SPA_PREFIXES) and Path(path).suffix in SPA_SUFFIXES:
        return "source"
    return file_class(path)
