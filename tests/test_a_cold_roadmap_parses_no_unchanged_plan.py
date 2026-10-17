"""A fresh process over a built store parses no plan a cold roadmap reads.

The cold roadmap used to re-parse every plan document per process: the roadmap
assembles each plan's follow-ups by re-reading the docs tree, so a restarted
server or a fresh MCP process paid a soup build per plan before it could answer
— 3.6 s of parses over 183 plans in one profile. The persisted metadata index
already keeps each unchanged file's parsed state, keyed by its content digest,
and the roadmap's discovery read answers from it; the follow-up scan did not.

The measure is the number of ``_plan_html.parse_plan`` calls a fresh process
makes while answering one roadmap: a reader over a store already built for an
unchanged tree makes none, and the roadmap it returns equals the one the parse
path produces. Bypassing the store makes the scan parse every plan again — the
mutation the count is judged against.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLANS = 24

#: The child runs one roadmap and counts the plan parses it makes. It is a
#: separate process so its in-memory memo is empty, which is the state a
#: restarted server meets; it asserts the tree it imported its code from so a
#: shared environment cannot make it test a different checkout. With
#: ``PLAN_STORE_UNCACHED`` set it forces the pre-store behaviour — discovery and
#: the follow-up scan both re-derive from the files — so the roadmap can be
#: compared against the parse path.
_DRIVER = dedent(
    """
    import json
    import os
    import sys
    from pathlib import Path

    tree = os.environ["RECKON_TREE"]
    sys.path.insert(0, tree)

    from reckon import _plan_html, mcp

    imported = Path(_plan_html.__file__).resolve()
    if not imported.is_relative_to(Path(tree).resolve()):
        print(json.dumps({"error": f"imported {imported} outside the tree"}))
        raise SystemExit(2)

    if os.environ.get("PLAN_STORE_UNCACHED") == "1":
        from reckon import _store, metadata_index

        metadata_index.discovery_state = lambda *a, **k: {}
        _store._stored_plan_states = lambda *a, **k: {}

    calls = {"n": 0}
    real = _plan_html.parse_plan

    def counting(path, slug=None):
        calls["n"] += 1
        return real(path, slug)

    _plan_html.parse_plan = counting

    root = sys.argv[1]
    project = sys.argv[2]

    if len(sys.argv) > 3 and sys.argv[3] == "prime-only":
        from reckon import serve

        serve.discover_plans(Path(root) / "docs", project, None)
        print(json.dumps({"prime_parses": calls["n"]}))
        raise SystemExit(0)

    report = mcp._roadmap(project, checkout_path=root, view="raw")
    print(json.dumps({"parse_plan_calls": calls["n"], "roadmap": report}))
    """
)


def _plan_html(slug: str, status: str = "active", followups: str = "") -> str:
    """A minimal plan document carrying the fields the roadmap reads."""

    extra = ""
    if followups:
        extra = f'<section data-reckon="followups">{followups}</section>'
    return dedent(
        f"""\
        <!doctype html><html><head>
        <meta name="docs-project" content="probe">
        <meta name="reckon-type" content="plan">
        <meta name="plan-slug" content="{slug}">
        <meta name="plan-title" content="Plan {slug}">
        <meta name="plan-status" content="{status}">
        <meta name="plan-impl" content="0.25">
        <meta name="plan-version" content="1">
        </head><body>{extra}<p>body of {slug}</p></body></html>
        """
    )


def _followup(identifier: str, status: str = "open") -> str:
    return (
        f'<article class="r-fu" data-id="{identifier}" data-status="{status}">'
        f"<h4>{identifier}</h4><pre>/reckon-build x</pre></article>"
    )


def _build_tree(root: Path, plans: int = PLANS) -> Path:
    """Write a temporary docs tree of ``plans`` plans and return its docs dir."""

    docs = root / "docs"
    (docs / "plans").mkdir(parents=True)
    for index in range(plans):
        slug = f"p{index:02d}"
        # An open follow-up on one plan in three exercises the roadmap's
        # unresolved-followup read; one resolved follow-up exercises the flag
        # that excludes it.
        if index == 2:
            followups = _followup(f"fu-{slug}", status="resolved")
        elif index % 3 == 0:
            followups = _followup(f"fu-{slug}")
        else:
            followups = ""
        (docs / "plans" / f"{slug}.html").write_text(
            _plan_html(slug, followups=followups), encoding="utf-8"
        )
    return docs


def _driver(tmp_path: Path) -> Path:
    script = tmp_path / "roadmap_driver.py"
    script.write_text(_DRIVER, encoding="utf-8")
    return script


def _run(
    script: Path,
    root: Path,
    project: str,
    config_home: Path,
    *,
    uncached: bool = False,
    prime_only: bool = False,
) -> dict:
    """Run one fresh process and return the JSON it printed."""

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(config_home.parent),
        "RECKON_HOME": str(config_home),
        "RECKON_TREE": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if uncached:
        env["PLAN_STORE_UNCACHED"] = "1"
    args = [sys.executable, str(script), str(root), project]
    if prime_only:
        args.append("prime-only")
    completed = subprocess.run(
        args, capture_output=True, text=True, env=env, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.fixture
def root(tmp_path: Path) -> Path:
    tree_root = tmp_path / "checkout"
    _build_tree(tree_root)
    return tree_root


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    # The child gets RECKON_HOME in its own environment; the parent needs it too
    # to resolve the same store the child wrote, never the operator's.
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def test_a_fresh_process_over_a_built_store_parses_no_plan(tmp_path, root, config_home):
    """A fresh process answers the roadmap from the store, parsing no plan."""

    script = _driver(tmp_path)
    primed = _run(script, root, "probe", config_home, prime_only=True)
    assert primed["prime_parses"] == 0

    fresh = _run(script, root, "probe", config_home)
    assert fresh["parse_plan_calls"] == 0, (
        f"a fresh process parsed {fresh['parse_plan_calls']} plans for the roadmap"
    )
    data = fresh["roadmap"].get("data", {})
    assert data.get("scope", {}).get("plans") == PLANS, (
        "the roadmap did not read the tree it was pointed at"
    )


def test_the_roadmap_equals_the_uncached_result(tmp_path, root, config_home):
    """The store answer and the parse answer are one roadmap.

    The uncached arm re-derives discovery and the follow-up scan from the files,
    so it parses every plan; the arm over the built store parses none. Both must
    return the same roadmap, or the store is answering something other than what
    the tree says.
    """

    script = _driver(tmp_path)
    _run(script, root, "probe", config_home, prime_only=True)

    cached = _run(script, root, "probe", config_home)
    uncached = _run(script, root, "probe", config_home, uncached=True)

    assert cached["parse_plan_calls"] == 0
    assert uncached["parse_plan_calls"] == PLANS, (
        f"the uncached arm parsed {uncached['parse_plan_calls']} of {PLANS} plans; "
        "the comparison is not measuring the parse path"
    )
    assert cached["roadmap"] == uncached["roadmap"], (
        "the store answered a different roadmap than the parse path"
    )


def test_a_changed_plan_is_not_answered_from_the_store(tmp_path, root, config_home):
    """A plan whose bytes moved is read again, and its new state is returned.

    The store keys each entry on the file's content digest, so an edited plan
    falls out of it and is re-derived on the read that follows; a reader that
    answered from the old entry would serve the stale state this store exists to
    avoid.
    """

    script = _driver(tmp_path)
    _run(script, root, "probe", config_home, prime_only=True)
    before = _run(script, root, "probe", config_home)
    assert before["parse_plan_calls"] == 0
    ready_before = {
        row.get("slug") for row in before["roadmap"]["data"].get("ready_now", [])
    }
    assert "p00" in ready_before

    (root / "docs" / "plans" / "p00.html").write_text(
        _plan_html("p00", status="done"), encoding="utf-8"
    )

    after = _run(script, root, "probe", config_home)
    ready_after = {
        row.get("slug") for row in after["roadmap"]["data"].get("ready_now", [])
    }
    assert "p00" not in ready_after, "the edited plan's new state must be returned"
