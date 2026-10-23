"""A roadmap read answers from a snapshot keyed on the project's write generation.

A warm read composes one project's plans and sprint state plus the mounted
projects those plans name, then re-establishes on the next call that none of it
moved by walking the covered tree. The snapshot makes that re-establishment
cheap: the composed result is stored under a key of the few facts it is a
function of — each mounted project's staging index, its HEAD, and the store's
plan-write counter, with a live run's classification carried by the crew
watcher's own per-run reduction.

The driver runs out of process so the counts are taken in a process whose
imports, environment and cwd are the ones under test, and so a second process
can be asked what it answers with nothing warmed at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

SOURCE_TREE = Path(__file__).resolve().parents[1]
DOCUMENT_COUNT = 200
DIRECTORY_COUNT = 10

DRIVER = dedent(
    """
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    source = Path(os.environ["RECKON_TREE"]).resolve()
    sys.path.insert(0, str(source))
    from reckon import mcp, serve

    root = Path(sys.argv[1]).resolve()
    mode = sys.argv[2]
    home = root / "home"
    serve._resolve_paths(home / "mounts.json")

    if mode == "nocounter":
        # The declared negative control: the write counter is dropped from the
        # key, so a key match survives a plan-write and the call answers with
        # the composition made before it.
        real = serve.roadmap_project_generation
        serve.roadmap_project_generation = (
            lambda project, docs_dir: real(project, docs_dir)[:2] + ["stable"]
        )

    real_stat = os.stat
    real_open = __import__("builtins").open
    counts = {"covered": 0, "keys": {}}

    p1_docs = (root / "p1" / "docs").resolve()
    p2_docs = (root / "p2" / "docs").resolve()
    p1_root = (root / "p1").resolve()
    p2_root = (root / "p2").resolve()

    def _note(path, kind):
        try:
            resolved = Path(str(path)).resolve()
        except OSError:
            return
        if kind == "stat" or kind == "open":
            if str(resolved).startswith(str(p1_docs)) or str(resolved).startswith(
                str(p2_docs)
            ):
                counts["covered"] += 1
            for name, base in (("p1", p1_root), ("p2", p2_root)):
                if str(resolved).startswith(str(base)):
                    counts["keys"][name] = counts["keys"].get(name, 0) + 1
                    break

    def counting_stat(path, *args, **kwargs):
        _note(path, "stat")
        return real_stat(path, *args, **kwargs)

    def counting_open(file, *args, **kwargs):
        _note(file, "open")
        return real_open(file, *args, **kwargs)

    os.stat = counting_stat
    __import__("builtins").open = counting_open

    def call():
        return mcp._roadmap("p1", view="raw")

    def reconsidered(action):
        counts["covered"] = 0
        counts["keys"] = {}
        action()
        stats = counts["covered"]
        per_project = max(counts["keys"].values(), default=0)
        return stats, per_project

    result = {"mode": mode}
    first = call()

    # Case: a second call with nothing changed walks no covered file, and its
    # key stays inside the per-project binding.
    counts["covered"] = 0
    counts["keys"] = {}
    second = call()
    result["covered_stats_second_call"] = counts["covered"]
    result["key_calls_per_project"] = max(counts["keys"].values(), default=0)
    result["equal"] = first == second

    # Case: a plan-write moves the key, so the next call recomposes.
    from reckon._store import advance_write_generation

    advance_write_generation("p1")
    recomposed, _ = reconsidered(call)
    result["write_counter_recomposed"] = recomposed
    # A counter that moved without changing the plan state still recomposes to
    # the same answer: the snapshot is keyed on the write, not on the content.
    result["unchanged_after_counter_write"] = call() == first

    # Case: a git commit in either project moves the key.
    def commit(project):
        subprocess.run(
            ["git", "-C", str(root / project), "add", "-A"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(root / project),
                "-c",
                "user.email=t@example.invalid",
                "-c",
                "user.name=t",
                "commit",
                "-q",
                "-m",
                "change",
                "--allow-empty",
            ],
            check=True,
            capture_output=True,
        )

    commit("p2")
    recomposed, _ = reconsidered(call)
    result["sibling_commit_recomposed"] = recomposed

    # Case: an in-place edit that neither reckon nor git made is seen once the
    # revalidation window has passed.
    edited = root / "p1" / "docs" / "plans" / "plan-00.html"
    text = edited.read_text(encoding="utf-8")
    edited.write_text(
        text.replace("<p>plan-00</p>", "<p>edited</p>"), encoding="utf-8"
    )
    seen_inside, _ = reconsidered(call)
    result["edited_inside_window"] = seen_inside
    os.environ["RECKON_ROADMAP_REVALIDATE_S"] = "0"
    seen_after, _ = reconsidered(call)
    result["edited_after_window"] = seen_after

    # Case: a live pointer of another project leaves this project's call alone.
    os.environ["RECKON_ROADMAP_REVALIDATE_S"] = "3600"
    call()
    live = home / "crew" / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / "r-other.json").write_text(
        json.dumps(
            {
                "run_id": "r-other",
                "project": "p2",
                "phase": "working",
                "manifest_path": str(root / "p2" / "crew" / "manifest.md"),
            }
        ),
        encoding="utf-8",
    )
    counts["covered"] = 0
    call()
    result["sibling_pointer_covered_stats"] = counts["covered"]

    print(json.dumps(result))
    """
)


def _document(slug: str, project: str) -> str:
    return dedent(
        f"""\
        <!doctype html><html><head>
        <meta name="docs-project" content="{project}">
        <meta name="reckon-type" content="plan">
        <meta name="plan-slug" content="{slug}">
        <meta name="plan-title" content="{slug}">
        <meta name="plan-status" content="active">
        <meta name="plan-impl" content="0.25">
        <meta name="plan-version" content="1">
        </head><body><p>{slug}</p></body></html>
        """
    )


def _build_project(root: Path, project: str, *, depends_on: str | None) -> Path:
    docs = root / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    for number in range(DOCUMENT_COUNT):
        slug = f"plan-{number:03d}"
        name = "plan-00" if number == 0 else slug
        (plans / f"{slug}.html").write_text(
            _document(name, project), encoding="utf-8"
        )
    for directory in range(DIRECTORY_COUNT):
        branch = docs / f"area-{directory:02d}"
        branch.mkdir()
        for number in range(DOCUMENT_COUNT // DIRECTORY_COUNT):
            (branch / f"work-{number:02d}.html").write_text(
                _document(f"work-{number:02d}", project), encoding="utf-8"
            )
    if depends_on is not None:
        (plans / "plan-00.html").write_text(
            _document("plan-00", project).replace(
                "<body>",
                f'<body data-links="{depends_on}:plan-000">',
            ),
            encoding="utf-8",
        )
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
        capture_output=True,
    )
    return root


def _build(tmp_path: Path) -> Path:
    root = tmp_path / "fleet"
    root.mkdir()
    _build_project(root / "p1", "p1", depends_on="p2")
    _build_project(root / "p2", "p2", depends_on=None)
    home = root / "home"
    home.mkdir()
    (home / "mounts.json").write_text(
        json.dumps(
            {
                "p1": str((root / "p1" / "docs").resolve()),
                "p2": str((root / "p2" / "docs").resolve()),
            }
        ),
        encoding="utf-8",
    )
    return root


def _run(root: Path, mode: str, driver: Path) -> dict:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(root),
        "RECKON_HOME": str(root / "home"),
        "RECKON_TREE": os.environ.get("RECKON_TREE", str(SOURCE_TREE)),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    completed = subprocess.run(
        [sys.executable, str(driver), str(root), mode],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _driver(tmp_path: Path) -> Path:
    path = tmp_path / "driver.py"
    path.write_text(DRIVER, encoding="utf-8")
    return path


def test_a_second_read_walks_no_covered_file(tmp_path: Path) -> None:
    root = _build(tmp_path)
    measured = _run(root, "measure", _driver(tmp_path))
    assert measured["covered_stats_second_call"] == 0, measured
    assert measured["key_calls_per_project"] <= 8, measured
    assert measured["equal"] is True, measured


def test_a_store_write_makes_the_next_read_recompose(tmp_path: Path) -> None:
    root = _build(tmp_path)
    measured = _run(root, "measure", _driver(tmp_path))
    assert measured["write_counter_recomposed"] > 0, measured
    assert measured["unchanged_after_counter_write"] is True, measured


def test_the_write_counter_is_what_carries_the_store_write(tmp_path: Path) -> None:
    """The declared negative control: drop the counter and the case goes stale."""
    root = _build(tmp_path)
    measured = _run(root, "nocounter", _driver(tmp_path))
    assert measured["write_counter_recomposed"] == 0, measured


def test_a_sibling_commit_and_an_in_place_edit_are_seen(tmp_path: Path) -> None:
    root = _build(tmp_path)
    measured = _run(root, "measure", _driver(tmp_path))
    assert measured["sibling_commit_recomposed"] > 0, measured
    assert measured["edited_inside_window"] == 0, measured
    assert measured["edited_after_window"] > 0, measured


def test_a_sibling_projects_live_pointer_does_not_disturb_this_read(
    tmp_path: Path,
) -> None:
    root = _build(tmp_path)
    measured = _run(root, "measure", _driver(tmp_path))
    assert measured["sibling_pointer_covered_stats"] == 0, measured