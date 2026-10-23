"""A fresh roadmap shares one docs-tree listing and one read per document."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

SOURCE_TREE = Path(__file__).resolve().parents[1]
DOCUMENT_COUNT = 24
DRIVER = dedent(
    """
    import json
    import os
    import sys
    from collections import Counter
    from pathlib import Path

    source = Path(os.environ["RECKON_TREE"]).resolve()
    sys.path.insert(0, str(source))
    from reckon import mcp, metadata_index, serve, sprint_liveness

    assert Path(mcp.__file__).resolve().is_relative_to(source)
    checkout = Path(sys.argv[1]).resolve()
    docs = checkout / "docs"
    if sys.argv[2] == "prime":
        serve.discover_plans(docs, "probe", None)
        print(json.dumps({"primed": True, "module": str(mcp.__file__), "cwd": str(Path.cwd().resolve())}))
        raise SystemExit(0)

    if sys.argv[2] == "freshness":
        before = mcp._roadmap("probe", checkout_path=str(checkout), view="raw")
        existing = (docs / "plans" / "plan-00.html").read_text(encoding="utf-8")
        (docs / "plans" / "added-plan.html").write_text(
            existing.replace("plan-00", "added-plan"), encoding="utf-8"
        )
        after = mcp._roadmap("probe", checkout_path=str(checkout), view="raw")
        print(json.dumps({
            "before": before["data"]["scope"]["plans"],
            "after": after["data"]["scope"]["plans"],
            "module": str(mcp.__file__),
            "cwd": str(Path.cwd().resolve()),
        }))
        raise SystemExit(0)

    if sys.argv[2] == "unshared":
        metadata_index._covered_files = lambda root, *args: metadata_index._walk_covered(root)[0]

    real_scandir = os.scandir
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes
    listings = Counter()
    reads = Counter()
    text_reads = Counter()
    byte_reads = Counter()

    def authored(path):
        resolved = Path(path).resolve()
        return (resolved == docs or docs in resolved.parents) and docs / "state" not in resolved.parents

    def counting_scandir(path):
        resolved = Path(path).resolve()
        # Run-ledger state has its own read path. Count authored documents,
        # including the canonical sprint documents.
        if authored(resolved):
            listings[str(resolved)] += 1
        return real_scandir(path)

    def counting_read_text(path, *args, **kwargs):
        resolved = Path(path).resolve()
        if authored(resolved):
            reads[str(resolved)] += 1
            text_reads[str(resolved)] += 1
        return real_read_text(path, *args, **kwargs)

    def counting_read_bytes(path, *args, **kwargs):
        resolved = Path(path).resolve()
        if authored(resolved):
            reads[str(resolved)] += 1
            byte_reads[str(resolved)] += 1
        return real_read_bytes(path, *args, **kwargs)

    os.scandir = counting_scandir
    Path.read_text = counting_read_text
    Path.read_bytes = counting_read_bytes
    sprint_liveness.list_live = lambda project=None: [
        {"run_id": "probe-run", "plan": "plan-00", "phase": "working"}
    ]
    sprint_reads = [0]
    real_plan_sprint = sprint_liveness._plan_sprint

    def counting_plan_sprint(*args, **kwargs):
        sprint_reads[0] += 1
        return real_plan_sprint(*args, **kwargs)

    sprint_liveness._plan_sprint = counting_plan_sprint
    report = mcp._roadmap("probe", checkout_path=str(checkout), view="raw")
    print(json.dumps({
        "roadmap": report,
        "listings": listings,
        "reads": reads,
        "text_reads": text_reads,
        "byte_reads": byte_reads,
        "sprint_reads": sprint_reads[0],
        "module": str(mcp.__file__),
        "cwd": str(Path.cwd().resolve()),
    }))
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


def _build_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    docs = root / "docs"
    for kind, count in (("plan", DOCUMENT_COUNT), ("research", 4)):
        directory = docs / ("plans" if kind == "plan" else "research")
        directory.mkdir(parents=True)
        for number in range(count):
            slug = f"{kind}-{number:02d}"
            (directory / f"{slug}.html").write_text(
                _document(slug, kind, f"S{number % 3 + 1}"), encoding="utf-8"
            )
    (docs / "sprints").mkdir(parents=True)
    for number in range(1, 4):
        (docs / "sprints" / f"S{number}.html").write_text(
            f'<meta name="reckon-type" content="sprint"><meta name="sprint-id" content="S{number}">',
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
def measured(tmp_path_factory: pytest.TempPathFactory) -> dict:
    tmp_path = tmp_path_factory.mktemp("cold-roadmap")
    checkout = _build_checkout(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    assert _run(driver, checkout, home, "prime")["primed"]
    # Move the recorded shape after the store build so the cold read must
    # list the authored tree while retaining its built document inventory.
    (checkout / "docs" / "plans" / ".shape-marker").write_text("new entry")

    cold = _run(driver, checkout, home, "measure")
    unshared = _run(driver, checkout, home, "unshared")
    return {"checkout": checkout, "cold": cold, "unshared": unshared}


def test_one_cold_roadmap_lists_each_docs_directory_once(measured: dict) -> None:
    checkout = measured["checkout"]
    cold = measured["cold"]
    unshared = measured["unshared"]
    assert (
        cold["roadmap"].get("data", {}).get("scope", {}).get("plans") == DOCUMENT_COUNT
    )
    assert cold["roadmap"] == unshared["roadmap"]
    assert cold["sprint_reads"] > 0
    # The marker moved docs/plans alone, so the cold read re-scans that
    # directory and takes every other directory's documents from the persisted
    # index without listing it.
    assert cold["listings"][str(checkout / "docs" / "plans")] == 1, cold["listings"]
    unchanged = {
        str(checkout / "docs" / relative) for relative in (".", "research", "sprints")
    }
    assert not unchanged & cold["listings"].keys(), cold["listings"]
    assert max(cold["listings"].values(), default=0) <= 1, cold["listings"]


def test_one_cold_roadmap_reads_each_authored_file_once(measured: dict) -> None:
    checkout = measured["checkout"]
    cold = measured["cold"]
    expected_files = {
        str(path)
        for directory in ("plans", "research", "sprints")
        for path in (checkout / "docs" / directory).glob("*.html")
    }
    assert len(expected_files) == DOCUMENT_COUNT + 4 + 3
    assert expected_files <= cold["reads"].keys(), cold["reads"]
    assert cold["reads"][str(checkout / "docs" / "plans" / "plan-00.html")] >= 1
    assert cold["byte_reads"][str(checkout / "docs" / "plans" / "plan-00.html")] >= 1
    assert max(cold["reads"].values(), default=0) <= 1, cold["reads"]


def test_later_read_sees_plan_added_between_reads(tmp_path: Path) -> None:
    checkout = _build_checkout(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    assert _run(driver, checkout, home, "prime")["primed"]

    result = _run(driver, checkout, home, "freshness")
    assert result["before"] == DOCUMENT_COUNT
    assert result["after"] == DOCUMENT_COUNT + 1
