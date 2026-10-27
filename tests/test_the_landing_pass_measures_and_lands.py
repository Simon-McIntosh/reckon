"""The landing pass measures a delivered run's suite before it lands it."""

from __future__ import annotations

import json
import os
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

#: The real base checkout, captured before the delivery fixture stubs it.
REAL_BASE_CHECKOUT = landing_measure.base_checkout


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
                "write_paths": [
                    "reckon/crew/queue_landing.py",
                    "tests/test_alpha.py",
                ],
            },
        },
    )
    base_tree = tmp_path / "base-tree"
    base_tree.mkdir()
    monkeypatch.setattr(
        landing_measure,
        "base_checkout",
        lambda repo, sha, timeout=0.0: _Yield(base_tree),
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


def _accept(*, base: str, head: str, score: int = 20, run_id: str = RUN) -> None:
    parsed = review_module.parse_review(
        "\n".join(
            f"SCORE {dimension}: {score}"
            for dimension in review_module.REVIEW_DIMENSIONS
        )
    )
    parsed.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(parsed)


def _deliver_another(run_id: str, *, order: int) -> None:
    """Register another delivered run over the same tree.

    ``order`` becomes the manifest's write time, so the landing pass sees a
    delivery order it can sort: a run delivered earlier has the earlier time.
    """
    template = runs.read_pointer(RUN)
    source = Path(str(template.get("manifest_path") or ""))
    manifest = runs.run_dir(run_id) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True)
    # The same delivered head and commit list, written a moment after RUN's.
    manifest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    when = source.stat().st_mtime_ns + order
    os.utime(manifest, ns=(when, when))
    pointer = dict(template)
    pointer.update({"run_id": run_id, "manifest_path": str(manifest)})
    runs._write_json(runs.pointer_path(run_id), pointer)


def _completed(argv, code: int = 0, output: str = ""):
    return subprocess.CompletedProcess(argv, code, output, "")


def _stub(calls: list[dict], *, suite_head: tuple[int, str], isolated):
    """A runner whose answers are keyed by tree and narrowed test id.

    ``isolated`` maps (test id, "base"|"head", repeat index) to a pass/fail.
    """

    seen: dict[tuple[str, str], int] = {}

    def run(argv, *, cwd, capture_output=True, text=True, check=False, **_ignored):
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


def test_narrowing_replaces_only_the_nodes_test_files_with_the_one_id() -> None:
    """Every token but the node's own test files belongs to the run.

    An armed command can carry an environment assignment, a shell wrapper, the
    interpreter and options whose values are themselves paths. Narrowing to one
    id replaces exactly the files the command was resolved from, so all of
    those survive and the command still means what it meant.
    """
    cases = [
        (
            ["pytest", "--cov-config", "coveragerc", "tests/test_alpha.py"],
            ["tests/test_alpha.py"],
        ),
        (
            ["env", "PYTHONPATH=.", "python", "-m", "pytest", "tests/x.py"],
            ["tests/x.py"],
        ),
        (["timeout", "300", "pytest", "-q", "tests/x.py"], ["tests/x.py"]),
        (
            [
                "PYTHONPATH=.",
                ".venv/bin/python",
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-q",
                "tests/a.py",
                "tests/b.py",
            ],
            ["tests/a.py", "tests/b.py"],
        ),
    ]
    for command, files in cases:
        expected = [token for token in command if token not in set(files)]
        assert landing_measure.isolated_command(command, REAL, files) == [
            *expected,
            REAL,
        ]


def test_an_option_value_that_is_not_a_node_test_file_survives_narrowing() -> None:
    """A path-valued option keeps its value; only the armed files are dropped."""
    command = [
        ".venv/bin/python",
        "-m",
        "pytest",
        "--basetemp",
        "build/bt",
        "--deselect",
        "tests/other.py::test_old",
        "tests/test_alpha.py",
    ]

    kept = landing_measure.isolated_command(command, REAL, ["tests/test_alpha.py"])

    assert "build/bt" in kept, kept
    assert "tests/other.py::test_old" in kept, kept
    assert "tests/test_alpha.py" not in kept, kept
    assert kept[-1] == REAL


def test_a_narrowed_arm_that_cannot_run_refuses_the_landing(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An arm that exits without reporting an id is unreadable, not a pass.

    Both revisions failing to run would otherwise read as failing equally, and
    a genuine regression would land on the comparison of two broken arms.
    """
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))
    landed = _land(monkeypatch)

    def run(argv, *, cwd, capture_output=True, text=True, check=False, **_ignored):
        if "::" in argv[-1]:
            return _completed(argv, 3, "usage: pytest [options]\n")
        if str(cwd).endswith("base-tree"):
            return _completed(argv, 0, "1 passed\n")
        return _completed(argv, 1, f"FAILED {REAL} - AssertionError\n")

    monkeypatch.setattr(landing_measure, "_RUN", run)

    result = queue_landing.land_once()

    assert result["landed"] == [], result
    assert landed == []
    guard = result["obligations"][0]["guard"]
    assert "unreadable" in guard and REAL in guard, guard
    assert runs.pointer_path(RUN).exists()


def test_a_preexisting_failure_in_the_same_file_does_not_mask_a_regression(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The isolated arm must run the id alone, not the file it lives in.

    ``test_existing`` already fails at the base. A narrowed run that still
    names the file re-runs that failure too, so the added id would read as
    failing equally at both revisions and the regression would land.
    """
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))
    landed = _land(monkeypatch)
    calls: list[dict] = []

    def run(argv, *, cwd, capture_output=True, text=True, check=False, **_ignored):
        arm = "base" if str(cwd).endswith("base-tree") else "head"
        calls.append({"argv": list(argv), "arm": arm})
        files = [token for token in argv if token.endswith(".py")]
        nodes = [token for token in argv if "::" in token]
        if files:
            # pytest runs every target it is given, so the file's own failures
            # are reported whatever else the command names.
            failures = ["tests/test_alpha.py::test_existing"]
            if arm == "head":
                failures.append(REAL)
            output = "".join(f"FAILED {node} - AssertionError\n" for node in failures)
            return _completed(argv, 1, output)
        assert nodes, argv
        failed = arm == "head" and nodes[0] == REAL
        output = f"FAILED {nodes[0]} - AssertionError\n" if failed else "1 passed\n"
        return _completed(argv, 1 if failed else 0, output)

    monkeypatch.setattr(landing_measure, "_RUN", run)

    result = queue_landing.land_once()

    assert result["landed"] == [], result
    assert landed == []
    guard = result["obligations"][0]["guard"]
    assert REAL in guard and "3/3 at head and 0/3 at base" in guard, guard
    narrowed = [call for call in calls if "::" in call["argv"][-1]]
    assert narrowed and not any(
        token.endswith(".py") for token in narrowed[0]["argv"]
    ), narrowed


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


def test_a_measurement_that_exceeds_its_bound_refuses_and_leaves_no_checkout(
    delivery, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A timed-out arm is unmeasurable, and the base checkout is removed.

    A hung suite would otherwise hold the admission loop for as long as it
    runs, and a partial log does not say what the test after it would have
    done, so the landing is refused rather than given a verdict.
    """
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))
    landed = _land(monkeypatch)

    def slow(
        argv, *, cwd=None, capture_output=True, text=True, check=False, timeout=None
    ):
        raise subprocess.TimeoutExpired(argv, timeout or 0)

    monkeypatch.setattr(landing_measure, "_RUN", slow)

    result = queue_landing.land_once()

    assert result["landed"] == [], result
    assert landed == []
    guard = result["obligations"][0]["guard"]
    assert "unreadable" in guard and "bound" in guard, guard

    # The base checkout itself is bounded, and a checkout that times out
    # leaves nothing behind.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    with (
        pytest.raises(ValueError, match="bound"),
        REAL_BASE_CHECKOUT(delivery["repo"], delivery["base"], 5),
    ):
        pytest.fail("a timed-out checkout yielded a tree")
    assert list(scratch.iterdir()) == [], "a timed-out checkout left a scratch tree"


def test_a_pass_measures_at_most_one_delivered_run(delivery, monkeypatch) -> None:
    """Three deliveries, one measurement: the others wait their turn.

    The landing pass runs inside the admission loop, so measuring every waiting
    run in one pass would hold admission for the sum of their suites.
    """
    base, head = str(delivery["base"]), str(delivery["head"])
    _accept(base=base, head=head)
    waiting = ["r-delivered-second", "r-delivered-third"]
    for index, run_id in enumerate(waiting, start=1):
        _deliver_another(run_id, order=index)
        _accept(base=base, head=head, run_id=run_id)
    _land(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(
        landing_measure, "_RUN", _stub(calls, suite_head=(0, "5 passed\n"), isolated={})
    )

    result = queue_landing.land_once()

    assert len(result["landed"]) == 1, result
    assert sorted(result["deferred"]) == sorted(waiting), result
    suites = [
        call
        for call in calls
        if call["arm"] == "head" and call["argv"][-1] == "tests/test_alpha.py"
    ]
    assert len(suites) == 1, calls


def test_a_second_pass_reuses_the_stored_measurement(delivery, monkeypatch) -> None:
    """A head already measured is not measured again.

    The stored record is keyed by the head it measured, so a later pass reads
    it instead of spending the lane on the same suites.
    """
    base, head = str(delivery["base"]), str(delivery["head"])
    _accept(base=base, head=head)
    landed = _land(monkeypatch)
    queue_landing._record_suite_evidence(
        RUN,
        landing_measure.Measurement(
            command=SUITE,
            suite={"command": SUITE, "base": {}, "head": {}},
            population={"files": []},
            head=head,
        ),
    )

    def refuse(*_args, **_kwargs):
        raise AssertionError("a stored head was measured again")

    monkeypatch.setattr(landing_measure, "_RUN", refuse)

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    assert landed == [RUN]
    assert runs.read_pointer(RUN)["suite_evidence"]["head"] == head
