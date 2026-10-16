"""A fresh process reads discovery from the persisted parse, not the files.

Every cold read used to re-parse the whole docs tree: the parsed ``<meta>``
record, the readiness state and the section records discovery and the roadmap
derive were held only in memory, so a restarted server or a fresh MCP process
paid a soup build per plan before it could answer. The parse is now kept in the
persisted metadata index, keyed by each file's content digest, and a fresh
process serves discovery from it, parsing only the files whose bytes changed.

The measure is the number of ``read_state`` calls a fresh process makes while
answering discovery: the body parse itself, which the cold roadmap's runtime is
attributed to. A reader over a store already built for an unchanged tree makes
none; a changed file makes exactly one, and its new state is returned.

An absent or unreadable store falls back to the parse rather than to a stale
answer, and bypassing the store makes the fresh read parse every plan again —
the negative control the measure is judged against.
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

#: The child process runs discovery and counts the body parses it makes. It is
#: a separate process so its in-memory memo is empty, which is the state a
#: restarted server meets; it asserts the tree it imported its own code from so
#: a shared environment cannot make it test a different checkout.
_DRIVER = dedent(
    """
    import json
    import os
    import sys
    from pathlib import Path

    tree = os.environ["RECKON_TREE"]
    sys.path.insert(0, tree)

    from reckon import _plan_html, serve

    if os.environ.get("PLAN_PARSE_BYPASS") == "1":
        from reckon import metadata_index

        metadata_index._load_persisted = lambda *a, **k: {}

    imported = Path(_plan_html.__file__).resolve()
    if not imported.is_relative_to(Path(tree).resolve()):
        print(json.dumps({"error": f"imported {imported} outside the tree"}))
        raise SystemExit(2)

    calls = {"n": 0}
    real = _plan_html.read_state

    def counting(text):
        calls["n"] += 1
        return real(text)

    _plan_html.read_state = counting

    docs = Path(sys.argv[1])
    project = sys.argv[2]
    if len(sys.argv) > 3 and sys.argv[3] == "prime-only":
        from reckon import metadata_index

        metadata_index.discovery_state(docs, project)
        print(json.dumps({"build_parses": calls["n"]}))
        raise SystemExit(0)

    result = serve.discover_plans(docs, project, None)
    plans = {
        row["slug"]: {
            "status": row.get("status"),
            "impl": row.get("impl"),
            "followups": len(row.get("followups") or []),
        }
        for row in result["inventory"]
        if row.get("type") == "plan"
    }
    print(json.dumps({"parses": calls["n"], "count": len(plans), "plans": plans}))
    """
)


def _plan_html(slug: str, status: str = "active", followups: str = "") -> str:
    """A minimal plan document carrying the fields discovery reads."""

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


def _open_followup(identifier: str) -> str:
    return (
        f'<article class="r-fu" data-id="{identifier}" data-status="open">'
        f"<h4>{identifier}</h4><pre>/reckon-build x</pre></article>"
    )


def _build_tree(root: Path, plans: int = PLANS) -> Path:
    """Write a temporary docs tree of ``plans`` plans and return its docs dir."""

    docs = root / "docs"
    (docs / "plans").mkdir(parents=True)
    for index in range(plans):
        slug = f"p{index:02d}"
        # One plan carries an open followup, so a row's body-derived state is
        # exercised through the store and not only its scalar head.
        followups = _open_followup(f"fu-{slug}") if index == 0 else ""
        (docs / "plans" / f"{slug}.html").write_text(
            _plan_html(slug, followups=followups), encoding="utf-8"
        )
    return docs


def _driver(tmp_path: Path) -> Path:
    script = tmp_path / "discovery_driver.py"
    script.write_text(_DRIVER, encoding="utf-8")
    return script


def _run(
    script: Path,
    docs: Path,
    project: str,
    config_home: Path,
    *,
    bypass: bool = False,
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
    if bypass:
        env["PLAN_PARSE_BYPASS"] = "1"
    args = [sys.executable, str(script), str(docs), project]
    if prime_only:
        args.append("prime-only")
    completed = subprocess.run(
        args, capture_output=True, text=True, env=env, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return _build_tree(tmp_path / "loose")


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    # The child gets RECKON_HOME in its own environment; the parent needs it too
    # to resolve the same store the child wrote, never the operator's.
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def test_new_process_reusing_the_store_parses_nothing(tmp_path, tree, config_home):
    """A fresh process over an unchanged, persisted tree parses no plan."""

    script = _driver(tmp_path)
    first = _run(script, tree, "probe", config_home)
    assert first["count"] == PLANS
    assert first["parses"] == PLANS, "the first read must parse the tree once"

    second = _run(script, tree, "probe", config_home)
    assert second["count"] == PLANS
    assert second["parses"] == 0, (
        f"a fresh process re-parsed {second['parses']} plans from the store"
    )
    assert second["plans"] == first["plans"], "the store changed the answer"


def test_one_changed_plan_is_the_only_parse(tmp_path, tree, config_home):
    """Changing one plan parses exactly that plan and returns its new state."""

    script = _driver(tmp_path)
    before = _run(script, tree, "probe", config_home)
    assert before["plans"]["p03"]["status"] == "active"

    (tree / "plans" / "p03.html").write_text(
        _plan_html("p03", status="done"), encoding="utf-8"
    )

    after = _run(script, tree, "probe", config_home)
    assert after["parses"] == 1, f"a one-file change parsed {after['parses']} plans"
    assert after["plans"]["p03"]["status"] == "done", "the new state must be returned"

    settled = _run(script, tree, "probe", config_home)
    assert settled["parses"] == 0, "the rebuilt entry must be reused next time"
    assert settled["plans"] == after["plans"]


def test_unreadable_store_falls_back_to_the_parse(tmp_path, tree, config_home):
    """A store that cannot be read is re-derived, never read as stale."""

    from reckon import metadata_index

    script = _driver(tmp_path)
    built = _run(script, tree, "probe", config_home)
    index_path = metadata_index._index_path(tree, "probe")
    assert index_path.is_file(), "the read must persist a store"

    index_path.write_text("{not valid json", encoding="utf-8")

    recovered = _run(script, tree, "probe", config_home)
    assert recovered["parses"] == PLANS, "an unreadable store must re-parse the tree"
    assert recovered["plans"] == built["plans"], "the re-derived answer must match"


def test_bypassing_the_store_parses_every_plan(tmp_path, tree, config_home):
    """The negative control: the measure sees a re-parse when the store is gone.

    With the persisted store bypassed, the fresh-process read parses every plan
    again, so the measure that reads zero against the live store reads the whole
    tree here. This is the mutation the parse-count check must fail against.
    """

    script = _driver(tmp_path)
    _run(script, tree, "probe", config_home)

    bypassed = _run(script, tree, "probe", config_home, bypass=True)
    assert bypassed["parses"] == PLANS, (
        f"bypassing the store parsed {bypassed['parses']} of {PLANS} plans"
    )


def test_the_store_carries_the_body_state(tmp_path, tree, config_home):
    """The persisted parse keeps the body-derived followup, not only the head."""

    script = _driver(tmp_path)
    built = _run(script, tree, "probe", config_home)
    assert built["plans"]["p00"]["followups"] == 1

    reused = _run(script, tree, "probe", config_home)
    assert reused["parses"] == 0
    assert reused["plans"]["p00"]["followups"] == 1


def _status_of(inventory: list[dict], slug: str) -> str | None:
    for row in inventory:
        if row.get("slug") == slug:
            return row.get("status")
    return None


def test_an_in_process_edit_is_not_answered_from_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file changed in this process must not be answered from the stored parse.

    A state edit read back through the roadmap, and a review composed after its
    fixture moved, both write a plan and read discovery again with no change
    watch reporting it. The stored parse is keyed by the file's bytes, so the
    read that follows the edit must see the new bytes whichever route it takes
    to the store — a reader that answers from a parse of the file as it was is
    serving the stale state this store exists to avoid.
    """

    from reckon import metadata_index, serve

    docs = _build_tree(tmp_path / "edited")
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(serve, "load_mounts", lambda: {"probe": docs})
    monkeypatch.setattr(serve, "_STATE_ROOT", None)

    first = serve.discover_plans(docs, "probe", None)
    assert _status_of(first["inventory"], "p03") == "active", (
        "the read that establishes the store did not answer for the plan"
    )

    (docs / "plans" / "p03.html").write_text(
        _plan_html("p03", status="done"), encoding="utf-8"
    )

    second = serve.discover_plans(docs, "probe", None)
    assert _status_of(second["inventory"], "p03") == "done", (
        "the read after the edit answered from a parse of the file as it was"
    )
    assert metadata_index.index_rows(docs, "probe")
