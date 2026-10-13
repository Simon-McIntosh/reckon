"""A warm read re-identifies only the files whose stat signature moved.

Discovery identifies every HTML file it walks: it hashes the bytes and parses
the meta head, and it did so on every read although nothing changed between
reads. Each file's identification is now memoised on the file's own stat
signature — path, ``mtime_ns`` and size — so a second read of an unchanged tree
stats each file and reuses the identification, while a file whose bytes change
has a new signature and is identified again.

The count of identifications is the property under test, so the uncached
identification is wrapped and counted: the wrapper delegates to the real
function, and each file's call count is asserted.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from reckon import resources

PROJECT = "sample"
HEAD = (
    "<!doctype html><html><head>"
    f'<meta name="docs-project" content="{PROJECT}">'
    '<meta name="reckon-type" content="{type}">'
    "<!--{body}-->"
    '<meta name="plan-slug" content="{slug}">'
    "<title>{slug} | {type}</title></head>"
    "<body><main><h1>{slug}</h1></main></body></html>"
)


def _write(
    path: Path, *, slug: str, artifact_type: str = "plan", body: str = ""
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        HEAD.format(slug=slug, type=artifact_type, body=body),
        encoding="utf-8",
    )


@pytest.fixture
def docs_tree(tmp_path: Path) -> Path:
    """A docs tree holding three already-identified resources."""
    docs = tmp_path / "docs"
    _write(docs / "plans" / "alpha.html", slug="alpha")
    _write(docs / "plans" / "beta.html", slug="beta")
    _write(docs / "research" / "gamma.html", slug="gamma", artifact_type="research")
    return docs


@pytest.fixture
def counted_identifications(monkeypatch: pytest.MonkeyPatch) -> Counter:
    """Record every uncached identification, delegating to the real function."""
    counts: Counter = Counter()
    real = resources._identify_resource

    def counting(docs_dir, path, project):
        counts[Path(path).name] += 1
        return real(docs_dir, path, project)

    monkeypatch.setattr(resources, "_identify_resource", counting)
    return counts


def test_two_reads_of_an_unchanged_tree_identify_each_file_once(
    docs_tree: Path, counted_identifications: Counter
) -> None:
    resources.resource_map(docs_tree, PROJECT)
    resources.resource_map(docs_tree, PROJECT)

    assert counted_identifications == Counter(
        {"alpha.html": 1, "beta.html": 1, "gamma.html": 1}
    )


def test_only_the_changed_file_is_identified_again(
    docs_tree: Path, counted_identifications: Counter
) -> None:
    resources.resource_map(docs_tree, PROJECT)
    counted_identifications.clear()

    _write(docs_tree / "plans" / "beta.html", slug="beta", body="moved")

    resources.resource_map(docs_tree, PROJECT)

    assert counted_identifications == Counter({"beta.html": 1})
