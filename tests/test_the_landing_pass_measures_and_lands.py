"""The landing pass measures a delivered run's suite before it lands it."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import landing_measure, queue_landing, runs
from reckon.crew import review as review_module

PROJECT = "landing-fixture"
PLAN = "delivery-plan"
SESSION = "owner"
RUN = "r-delivered-implementation"
SUITE = "python -m pytest -q tests/test_alpha.py"
FLAKY = "tests/test_alpha.py::test_flaky"
REAL = "tests/test_alpha.py::test_real"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write_plan(repo: Path) -> Path:
    path = repo / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    authored = (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<title>Delivery plan</title></head><body><main class="plan-doc">'
        '<h2 id="s1">One section</h2><p>The section body.</p>'
        "</main></body></html>"
    )
    path.write_text(
        _plan_html.write_state(
            authored,
            {
                "project": PROJECT,
                "type": "plan",
                "slug": PLAN,
                "title": "Delivery plan",
                "status": "active",
                "modified": "2026-10-27",
                "version": 0,
                "section_declarations": {"s1": "implementable"},
                "sections": [
                    {
                        "id": "s1",
                        "effort_hours": 1.0,
                        "capability": {
                            "version": "1.0",
                            "class": "general",
                            "requirements": {
                                "reasoning": "standard",
                                "verification": "strict",
                                "risk": "low",
                            },
                        },
                        "attempts": 0,
                        "status": "implementable",
                        "links": [],
                    }
                ],
            },
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    _write_plan(repo)
    state = repo / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}), encoding="utf-8"
    )
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_alpha.py").write_text(
        "from reckon.crew import queue_landing\n\n"
        "def test_flaky():\n    pass\n\n"
        "def test_real():\n    pass\n",
        encoding="utf-8",
    )
    module = repo / "reckon" / "crew"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "queue_landing.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "docs", "tests", "reckon")
    _git(repo, "commit", "-q", "-m", "chore: seed fixture")
    base = _git(repo, "rev-parse", "HEAD")
    (module / "queue_landing.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "reckon")
    _git(repo, "commit", "-q", "-m", "feat: change the module")
    head = _git(repo, "rev-parse", "HEAD")
    manifest = runs.run_dir(RUN) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f"node: implementation\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(RUN),
        {
            "run_id": RUN,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repo),
            "worktree": str(repo),
            "base_sha": base,
            "suite_command": SUITE,
            "process_alive": False,
            "launch": "in-harness",
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": "implementation",
                "plan": PLAN,
                "section": "s1",
                "role": "implement",
                "write_paths": ["reckon/crew/queue_landing.py"],
            },
        },
    )
    base_tree = tmp_path / "base-tree"
    base_tree.mkdir()
    monkeypatch.setattr(
        landing_measure, "base_checkout", lambda repo, sha: _Yield(base_tree)
    )
    return {
        "repo": repo,
        "base": base,
        "head": head,
        "base_tree": base_tree,
        "plan": repo / "docs" / "plans" / f"{PLAN}.html",
    }


class _Yield:
    """A context manager handing back a prepared tree, for the base arm."""

    def __init__(self, tree: Path) -> None:
        self._tree = tree

    def __enter__(self) -> Path:
        return self._tree

    def __exit__(self, *_exc) -> None:
        return None


def _accept(*, base: str, head: str, score: int = 20) -> None:
    parsed = review_module.parse_review(
        "\n".join(
            f"SCORE {dimension}: {score}"
            for dimension in review_module.REVIEW_DIMENSIONS
        )
    )
    parsed.update(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN,
            "review_run_id": "review-of-delivery",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(parsed)


def _completed(argv, code: int = 0, output: str = ""):
    return subprocess.CompletedProcess(argv, code, output, "")


def _stub(calls: list[dict], *, suite_head: tuple[int, str], isolated):
    """A runner whose answers are keyed by tree and narrowed test id.

    ``isolated`` maps (test id, "base"|"head", repeat index) to a pass/fail.
    """

    seen: dict[tuple[str, str], int] = {}

    def run(argv, *, cwd, capture_output=True, text=True, check=False):
        base = str(cwd).endswith("base-tree")
        arm = "base" if base else "head"
        narrowed = argv[-1] if "::" in argv[-1] else ""
        calls.append({"argv": list(argv), "arm": arm, "narrowed": narrowed})
        if narrowed:
            index = seen.get((narrowed, arm), 0)
            seen[(narrowed, arm)] = index + 1
            failed = isolated.get((narrowed, arm, index), False)
            output = f"FAILED {narrowed} - AssertionError\n" if failed else "1 passed\n"
            return _completed(argv, 1 if failed else 0, output)
        if arm == "head" and suite_head:
            code, output = suite_head
            return _completed(argv, code, output)
        return _completed(argv, 0, "3 passed\n")

    return run


def _land(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    landed: list[str] = []

    def fake(run_id: str) -> tuple[bool, str]:
        landed.append(run_id)
        return True, ""

    monkeypatch.setattr(queue_landing, "_land", fake)
    return landed


def test_a_clean_population_lands_with_its_armed_suite_pair_recorded(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))
    landed = _land(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(
        landing_measure, "_RUN", _stub(calls, suite_head=(0, "5 passed\n"), isolated={})
    )

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    assert landed == [RUN]
    # The armed command ran once at each revision, through the same runner the
    # run's promotion uses.
    suite_calls = [call for call in calls if call["argv"][-1] == "tests/test_alpha.py"]
    assert {call["arm"] for call in suite_calls} == {"base", "head"}, calls
    for call in suite_calls:
        assert call["argv"][:4] == [
            "python",
            "-m",
            "pytest",
            "-q",
        ], call
    # The importer population names the test file importing the changed module
    # and runs it in parallel.
    population = [call for call in calls if "-n" in call["argv"]]
    assert population, calls
    assert population[0]["argv"][-3:] == ["tests/test_alpha.py", "-n", "8"]
    assert {call["arm"] for call in population} == {"base", "head"}
    # The pair is recorded as the run's suite evidence.
    evidence = runs.read_pointer(RUN)["suite_evidence"]
    assert evidence["command"] == SUITE
    assert evidence["suite"]["base"]["exit_status"] == 0
    assert evidence["suite"]["head"]["exit_status"] == 0
    record = runs.run_dir(RUN) / "landing-measure" / "measurement.json"
    assert json.loads(record.read_text())["command"] == SUITE


def test_a_flaky_added_id_that_fails_as_often_at_base_as_at_head_lands(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))
    landed = _land(monkeypatch)
    calls: list[dict] = []
    isolated = {
        (FLAKY, "base", 0): True,
        (FLAKY, "head", 0): True,
    }
    monkeypatch.setattr(
        landing_measure,
        "_RUN",
        _stub(
            calls,
            suite_head=(1, f"FAILED {FLAKY} - AssertionError\n"),
            isolated=isolated,
        ),
    )

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    assert landed == [RUN]
    evidence = runs.read_pointer(RUN)["suite_evidence"]
    assert evidence["isolated"][FLAKY] == {
        "base": [True, False, False],
        "head": [True, False, False],
    }
    assert evidence["real"] == []
    # Each added id was re-run alone three times at each revision.
    narrowed = [call for call in calls if call["narrowed"] == FLAKY]
    assert len(narrowed) == 6, calls


def test_a_real_added_id_is_not_landed_and_one_obligation_names_it(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))
    landed = _land(monkeypatch)
    calls: list[dict] = []
    isolated = {(REAL, "head", index): True for index in range(3)}
    monkeypatch.setattr(
        landing_measure,
        "_RUN",
        _stub(
            calls,
            suite_head=(1, f"FAILED {REAL} - AssertionError\n"),
            isolated=isolated,
        ),
    )

    result = queue_landing.land_once()

    assert result["landed"] == [], result
    assert landed == []
    assert len(result["obligations"]) == 1, result
    guard = result["obligations"][0]["guard"]
    assert REAL in guard
    assert "3/3 at head and 0/3 at base" in guard
    assert runs.pointer_path(RUN).exists()
    assert runs.read_pointer(RUN)["admission_landing"]["status"] == "refused"


def test_the_landing_beat_marks_the_section_done_with_an_evidence_anchor(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))
    _land(monkeypatch)
    monkeypatch.setattr(
        landing_measure, "_RUN", _stub([], suite_head=(0, "1 passed\n"), isolated={})
    )

    queue_landing.land_once()

    state = _plan_html.read_state(delivery["plan"].read_text(encoding="utf-8"))
    assert state["section_declarations"]["s1"] == "done"
    closing = [
        comment
        for comments in (state.get("comments") or {}).values()
        for comment in comments
        if comment.get("id") == "c-close-s1"
    ]
    assert closing, state.get("comments")
    assert "evidence/archive" in closing[0]["body"]
    record = delivery["repo"] / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"
    assert record.is_file(), "the landing beat wrote no evidence record"
    assert 'id="implementation"' in record.read_text(encoding="utf-8")


def test_a_run_whose_review_is_not_accepted_is_untouched(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(base=str(delivery["base"]), head=str(delivery["head"]), score=10)
    landed = _land(monkeypatch)
    calls: list[dict] = []

    def refuse_measurement(*_args, **_kwargs):
        raise AssertionError("a rejected run was measured")

    monkeypatch.setattr(landing_measure, "measure_run", refuse_measurement)

    result = queue_landing.land_once()

    assert result["landed"] == []
    assert landed == []
    assert not calls
    state = _plan_html.read_state(delivery["plan"].read_text(encoding="utf-8"))
    assert state["section_declarations"]["s1"] == "implementable"
    assert not any(
        comment.get("id") == "c-close-s1"
        for comments in (state.get("comments") or {}).values()
        for comment in comments
    )


def test_importer_population_names_only_files_importing_a_changed_module(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "tree"
    (tree / "tests").mkdir(parents=True)
    (tree / "tests" / "test_yes.py").write_text(
        "from reckon.crew import queue_landing\n", encoding="utf-8"
    )
    (tree / "tests" / "test_no.py").write_text(
        "from reckon.crew import runs\n", encoding="utf-8"
    )

    population = landing_measure.importer_population(
        ["reckon/crew/queue_landing.py", "tests/test_yes.py"], tree
    )

    assert population == ("tests/test_yes.py",)
