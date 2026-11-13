"""A batch entry carries the test its section's done-when names.

A section's write-paths sentence sometimes names a file only by description —
"and the new test", "and its regression test" — so a fence built from the
sentence alone omits the test the worker is told to write. A batch of seven
entries over one fixture plan shows the rule: where the sentence describes the
test and the done-when names exactly one file under ``tests/``, that path joins
the entry's write paths; where the done-when names two, none, or one file that
is not a ``.py`` test, the entry is refused by name and the rest are still
stored; a sentence that names its test
path outright derives exactly what it did before; and an entry that supplies
its own write paths keeps them, the section's sentence never being read for it.
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

SECTIONS = ("s1", "s2", "s3", "s4", "s5", "s6", "s7")

#: Each section's prose. Two describe the test their sentence would name with
#: the done-when naming exactly one test file; two more name two and none, which
#: are refused; the fifth names its test path outright, so its derivation is the
#: one the base revision made. The sixth describes a test its done-when does not
#: name at all, so only an entry that supplies its own write paths can queue it.
#: The seventh names one file under ``tests/`` that is not a ``.py`` test, so the
#: path the repository recognises as a test path is the only one on offer.
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
    "s6": (
        "<p>Six.</p>"
        "<p>Write paths: <code>six.py</code> and the new test.</p>"
        "<p><strong>Done when</strong> the six check passes.</p>"
    ),
    "s7": (
        "<p>Seven.</p>"
        "<p>Write paths: <code>seven.py</code> and the new test.</p>"
        "<p><strong>Done when</strong> a new <code>tests/seven.json</code> "
        "records the seven check.</p>"
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
        if name != "s6"
    ] + [
        {
            # s6 describes a test its done-when does not name, so the section
            # supplies no write paths at all. The entry gives its own, and the
            # section's sentence is never read for it.
            "project": PROJECT,
            "plan": PLAN,
            "section": "s6",
            "node": "n-s6-own",
            "goal": "build s6 with its own paths",
            "time_budget": "20m",
            "spec_level": "exact",
            "write_paths": ["custom/own_six.py"],
        }
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

    # A done-when naming two tests/ files, one naming none, and one naming a
    # single file under tests/ that is not a .py test are all refused by name —
    # each naming the description and its sentence — while the other entries
    # are stored. The third case is the acceptance set the repository's own test
    # path pattern defines, so a looser extractor written here would queue it.
    for node_id in ("n-s2", "n-s3", "n-s7"):
        assert rows[node_id]["status"] == "refused", rows[node_id]
        reason = rows[node_id]["reason"]
        assert "the new test" in reason, reason
    # Each refusal quotes the sentence it could not resolve.
    assert "two.py and the new test" in rows["n-s2"]["reason"], rows["n-s2"]["reason"]
    assert "three.py and the new test" in rows["n-s3"]["reason"], rows["n-s3"]
    assert "seven.py and the new test" in rows["n-s7"]["reason"], rows["n-s7"]
    assert "s2" not in stored and "s3" not in stored and "s7" not in stored

    # The rule is not one phrase: a sentence ending "and its regression test"
    # resolves the same way.
    assert rows["n-s4"]["status"] == "queued", rows["n-s4"]
    assert stored["s4"]["write_paths"] == ["four.py", "tests/test_four.py"]

    # A sentence that names its test path outright derives what the base
    # revision derived: the literal path, and only it.
    assert rows["n-s5"]["status"] == "queued", rows["n-s5"]
    assert stored["s5"]["write_paths"] == ["five.py", "tests/test_five.py"]

    # An entry that supplies its own write paths is stored with them, and the
    # section's sentence is never read for it: this section's description
    # resolves to no path at all, so reading it would refuse the entry.
    assert rows["n-s6-own"]["status"] == "queued", rows["n-s6-own"]
    assert stored["s6"]["write_paths"] == ["custom/own_six.py"]

    assert result["queued"] == 4
    assert result["refused"] == 3


def test_an_entry_that_gave_its_own_write_paths_is_not_re_asked_for_them(
    batch_repo: Path,
) -> None:
    """A release re-derives only the fields the request took from the section.

    The entry that supplied its own write paths carries no mark on them, so the
    release never reads the write-paths sentence for it — and a described test
    the sentence cannot resolve cannot refuse that release. The section read is
    still the authority for the field the entry did take, and a request that
    did take its write paths is still refused when the sentence cannot be
    resolved.
    """
    from reckon.crew.plan_review import _digest

    stored = {"done_when": "the old three check", "write_paths": ["custom/own.py"]}

    values, digests = dispatch_batch.rederived_section_fields(
        project=PROJECT,
        plan=PLAN,
        section="s3",
        repo_root=batch_repo,
        marked={"done_when": _digest("the old three check")},
        stored=stored,
    )
    assert set(values) == {"done_when"}
    assert values["done_when"].endswith("the three check passes.")
    assert "write_paths" not in digests

    # A request that did take its write paths from the section still gets the
    # sentence read, and the ambiguous description still refuses it.
    with pytest.raises(dispatch_batch.BatchEntryRefusedError):
        dispatch_batch.rederived_section_fields(
            project=PROJECT,
            plan=PLAN,
            section="s3",
            repo_root=batch_repo,
            marked={"write_paths": _digest(["three.py"])},
            stored=stored,
        )
