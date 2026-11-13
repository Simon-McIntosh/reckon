"""A batch entry carries the test its section's done-when names.

A section's write-paths sentence sometimes names a file only by description —
"and the new test", "and its regression test" — so a fence built from the
sentence alone omits the test the worker is told to write. A batch of five
entries over one fixture plan shows the rule: where the sentence describes the
test and the done-when names exactly one file under ``tests/``, that path joins
the entry's write paths; where the done-when names two or none, the entry is
refused by name and the rest are still stored; and a sentence that names its
test path outright derives exactly what it did before.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import dispatch_batch, runs

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN = "plan-a"

CONFIG = {
    "default_backend": "clive",
    "local_backend": "clive",
    "backends": {
        "clive": {
            "launch": "cli",
            "command": "codex",
            "model": "local-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

SECTIONS = ("s1", "s2", "s3", "s4", "s5")

#: Each section's prose. Four describe the test their sentence would name, with
#: the done-when naming one test file, two, and none; the fifth names its test
#: path outright, so its derivation is the one the base revision made.
PROSE = {
    "s1": (
        "<p>One.</p>"
        "<p>Write paths: <code>one.py</code> and the new test.</p>"
        "<p><strong>Done when</strong> a new <code>tests/test_one.py</code> "
        "shows the one check.</p>"
    ),
    "s2": (
        "<p>Two.</p>"
        "<p>Write paths: <code>two.py</code> and the new test.</p>"
        "<p><strong>Done when</strong> <code>tests/test_two_a.py</code> and "
        "<code>tests/test_two_b.py</code> both show the two check.</p>"
    ),
    "s3": (
        "<p>Three.</p>"
        "<p>Write paths: <code>three.py</code> and the new test.</p>"
        "<p><strong>Done when</strong> the three check passes.</p>"
    ),
    "s4": (
        "<p>Four.</p>"
        "<p>Write paths: <code>four.py</code> and its regression test.</p>"
        "<p><strong>Done when</strong> a new <code>tests/test_four.py</code> "
        "shows the four check.</p>"
    ),
    "s5": (
        "<p>Five.</p>"
        "<p>Write paths: <code>five.py</code> and <code>tests/test_five.py</code>.</p>"
        "<p><strong>Done when</strong> a new <code>tests/test_five.py</code> "
        "shows the five check.</p>"
    ),
}


def _plan(slug: str) -> str:
    quote = '"'
    body = "".join(
        f"<h2 id={quote}{name}{quote}>§{name[1:]}</h2>{PROSE[name]}"
        for name in SECTIONS
    )
    declarations = json.dumps(dict.fromkeys(SECTIONS, "implementable"))
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{slug}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations{quote} content={quote}"
        f"{declarations.replace(quote, '&quot;')}{quote}>\n"
        f"{body}\n"
    )


def _batch() -> list[dict]:
    return [
        {
            "project": PROJECT,
            "plan": PLAN,
            "section": name,
            "node": f"n-{name}",
            "goal": f"build {name}",
            "time_budget": "20m",
            "spec_level": "exact",
            # Each entry's write paths reach tests/, so the queue asks it to
            # declare the mutation its check must fail against.
            "negative_control": f"reverting the {name} guard makes its test fail",
        }
        for name in SECTIONS
    ]


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


@pytest.fixture()
def batch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repository = tmp_path / "checkout"
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_json": True}}) + "\n",
        encoding="utf-8",
    )
    plans = repository / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(_plan(PLAN), encoding="utf-8")
    # The literally named test path must be present under the checkout, while a
    # described test's path is one the worker will create.
    tests = repository / "tests"
    tests.mkdir()
    (tests / "test_five.py").write_text("", encoding="utf-8")
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs", "tests")
    _git(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _queue(repository: Path) -> dict:
    def config_for(_project: str) -> dict:
        return json.loads(json.dumps(CONFIG))

    return dispatch_batch.queue_batch(
        _batch(), session=SESSION, config_for=config_for, repo_root=repository
    )


def _stored() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for pointer in runs.list_live(project=PROJECT):
        node = dict(pointer.get("node") or {})
        found[str(node.get("section"))] = node
    return found


def test_a_batch_entry_carries_its_done_when_test(batch_repo: Path) -> None:
    result = _queue(batch_repo)
    rows = {row["node"]: row for row in result["results"]}

    # One section's sentence describes the test and the done-when names exactly
    # one file under tests/: the entry is stored with that path.
    assert rows["n-s1"]["status"] == "queued", rows["n-s1"]
    stored = _stored()
    assert stored["s1"]["write_paths"] == ["one.py", "tests/test_one.py"]

    # A done-when naming two tests/ files, and one naming none, are both
    # refused by name — each naming the description and its sentence — while
    # the other entries are stored.
    for node_id in ("n-s2", "n-s3"):
        assert rows[node_id]["status"] == "refused", rows[node_id]
        reason = rows[node_id]["reason"]
        assert "the new test" in reason, reason
    # Each refusal quotes the sentence it could not resolve.
    assert "two.py and the new test" in rows["n-s2"]["reason"], rows["n-s2"]["reason"]
    assert "three.py and the new test" in rows["n-s3"]["reason"], rows["n-s3"]
    assert "s2" not in stored and "s3" not in stored

    # The rule is not one phrase: a sentence ending "and its regression test"
    # resolves the same way.
    assert rows["n-s4"]["status"] == "queued", rows["n-s4"]
    assert stored["s4"]["write_paths"] == ["four.py", "tests/test_four.py"]

    # A sentence that names its test path outright derives what the base
    # revision derived: the literal path, and only it.
    assert rows["n-s5"]["status"] == "queued", rows["n-s5"]
    assert stored["s5"]["write_paths"] == ["five.py", "tests/test_five.py"]

    assert result["queued"] == 3
    assert result["refused"] == 2
