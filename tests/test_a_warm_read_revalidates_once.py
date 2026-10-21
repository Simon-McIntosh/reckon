"""One warm roadmap read checks each covered directory's identity once.

The read reaches several readers — project discovery, the discovery change
signature and the metadata index — and each of them revalidates the covered
tree. A per-call region shares one listing and one set of stats between them,
so a directory's identity is observed once however many readers need it. A
directory that moves between two calls is still seen by the second, because the
region opens and closes with the read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

SOURCE_TREE = Path(__file__).resolve().parents[1]
DOCUMENT_COUNT = 12
DRIVER = dedent(
    """
    import contextlib
    import json
    import os
    import sys
    from collections import Counter
    from pathlib import Path

    source = Path(os.environ["RECKON_TREE"]).resolve()
    sys.path.insert(0, str(source))
    from reckon import mcp, metadata_index

    assert Path(mcp.__file__).resolve().is_relative_to(source)
    checkout = Path(sys.argv[1]).resolve()
    mode = sys.argv[2]

    if mode == "noshare":
        # The declared negative control: the scope replaces any open region
        # instead of joining it, so every nested entry starts from nothing and
        # each reader revalidates the covered tree on its own.

        @contextlib.contextmanager
        def replacing_scope():
            token = metadata_index._DISCOVERY_FILES.set({})
            try:
                yield
            finally:
                metadata_index._DISCOVERY_FILES.reset(token)

        metadata_index.discovery_file_scope = replacing_scope

    if mode == "join_only":
        # The join without the directory-identity cache, to show what the join
        # alone leaves behind: the directory checks every reader still takes.
        metadata_index.covered_dir_identity = (
            lambda path: metadata_index._dir_identity(path)
        )

    counts = Counter()
    real_dir_identity = metadata_index._dir_identity

    def counting_dir_identity(path):
        counts[str(Path(path).resolve())] += 1
        return real_dir_identity(path)

    metadata_index._dir_identity = counting_dir_identity

    def roadmap():
        report = mcp._roadmap("probe", checkout_path=str(checkout), view="raw")
        return report["data"] if "data" in report else report

    result = {"mode": mode}
    # Warm the process, then count only the call under test.
    roadmap()
    counts.clear()
    report = roadmap()
    result["plans"] = report["scope"]["plans"]

    if mode == "freshness":
        result["before"] = result["plans"]
        existing = (checkout / "docs" / "plans" / "plan-00.html").read_text(
            encoding="utf-8"
        )
        (checkout / "docs" / "plans" / "added-plan.html").write_text(
            existing.replace("plan-00", "added-plan"), encoding="utf-8"
        )
        counts.clear()
        result["after"] = roadmap()["scope"]["plans"]

    result["counts"] = counts
    result["distinct"] = len(counts)
    result["max_per_directory"] = max(counts.values(), default=0)
    result["total"] = sum(counts.values())
    result["module"] = str(mcp.__file__)
    result["cwd"] = str(Path.cwd().resolve())
    print(json.dumps(result))
    """
)


def _document(slug: str, kind: str, sprint: str) -> str:
    return dedent(
        f"""\
        <!doctype html><html><head>
        <meta name="docs-project" content="probe">
        <meta name="reckon-type" content="{kind}">
        <meta name="plan-slug" content="{slug}">
        <meta name="plan-title" content="{slug}">
        <meta name="plan-status" content="active">
        <meta name="plan-sprint" content="{sprint}">
        <meta name="plan-impl" content="0.25">
        <meta name="plan-version" content="1">
        </head><body><p>{slug}</p></body></html>
        """
    )


def _build_checkout(root: Path) -> Path:
    docs = root / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    for number in range(DOCUMENT_COUNT):
        slug = f"plan-{number:02d}"
        (plans / f"{slug}.html").write_text(
            _document(slug, "plan", f"S{number % 2 + 1}"), encoding="utf-8"
        )
    research = docs / "research"
    research.mkdir(parents=True)
    (research / "note-00.html").write_text(
        _document("note-00", "research", "S1"), encoding="utf-8"
    )
    sprints = docs / "sprints"
    sprints.mkdir(parents=True)
    for number in range(1, 3):
        (sprints / f"S{number}.html").write_text(
            f'<meta name="reckon-type" content="sprint">'
            f'<meta name="sprint-id" content="S{number}">',
            encoding="utf-8",
        )
    return root


def _run(driver: Path, checkout: Path, home: Path, mode: str) -> dict:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home.parent),
        "RECKON_HOME": str(home),
        "RECKON_TREE": os.environ.get("RECKON_TREE", str(SOURCE_TREE)),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    completed = subprocess.run(
        [sys.executable, str(driver), str(checkout), mode],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def warm(tmp_path_factory: pytest.TempPathFactory) -> dict:
    tmp_path = tmp_path_factory.mktemp("warm-roadmap")
    checkout = _build_checkout(tmp_path / "checkout")
    home = tmp_path / "home"
    home.mkdir()
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    mode = os.environ.get("RECKON_WARM_READ_MODE", "measure")
    return {"checkout": checkout, "measured": _run(driver, checkout, home, mode)}


def test_each_covered_directory_is_checked_at_most_once(warm: dict) -> None:
    measured = warm["measured"]
    checkout = warm["checkout"]
    expected = {
        str((checkout / "docs").resolve()),
        str((checkout / "docs" / "plans").resolve()),
        str((checkout / "docs" / "research").resolve()),
        str((checkout / "docs" / "sprints").resolve()),
    }
    assert expected <= set(measured["counts"]), measured["counts"]
    assert measured["max_per_directory"] <= 1, measured["counts"]


def test_a_directory_changed_between_calls_is_seen(tmp_path: Path) -> None:
    checkout = _build_checkout(tmp_path / "checkout")
    home = tmp_path / "home"
    home.mkdir()
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    measured = _run(driver, checkout, home, "freshness")
    assert measured["before"] == DOCUMENT_COUNT
    assert measured["after"] == DOCUMENT_COUNT + 1
