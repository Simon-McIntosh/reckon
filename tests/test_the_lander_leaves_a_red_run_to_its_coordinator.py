"""The lander leaves a run whose own evidence is red to its coordinator.

A delivered run recorded an ``after_suite`` exit status of 1 with a failure its
baseline did not name, and a review carrying ``added_failure_count: 1`` beside
five open findings. Within twenty minutes the admission lander promoted it with
gate ``passed`` and merged it, with no action from a coordinator who had planned
to land it only together with the fix. The primary branch carried the red test.

These cases drive the landing pass itself against that evidence: the run is
delivered, the review is stored, promotion is stubbed, and the assertions are
about what the pass did. One negative control per refusing arm removes that arm
and shows the same case promoting, so every refusal is shown to be the guard's
own doing rather than something else in the pass.

Every repository, store and live pointer resolves inside a temporary
configuration home, and the suite runner is stubbed, so nothing here runs a
suite, reads the operator's fleet, or merges anything.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import landing_measure, obligations, queue_landing, runs
from reckon.crew import review as review_module

PROJECT = "landing-fixture"
PLAN = "delivery-plan"
SESSION = "owner"
RUN = "r-red-implementation"
SUITE = "python -m pytest -q tests/test_alpha.py"
NEW_FAILURE = "tests/test_alpha.py::test_added_by_the_delivery"


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


class _Yield:
    """A context manager handing back a prepared tree, for the base arm."""

    def __init__(self, tree: Path) -> None:
        self._tree = tree

    def __enter__(self) -> Path:
        return self._tree

    def __exit__(self, *_exc) -> None:
        return None


def _completed(argv, code: int = 0, output: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(argv, code, output, "")


def _stub(calls: list[dict], *, suite_head: tuple[int, str], isolated):
    """A runner whose answers are keyed by tree and narrowed test id."""

    seen: dict[tuple[str, str], int] = {}

    def run(argv, *, cwd, capture_output=True, text=True, **_ignored):
        base = str(cwd).endswith("base-tree")
        arm = "base" if base else "head"
        narrowed = next((token for token in argv if "::" in token), "")
        calls.append({"argv": list(argv), "isolated": arm, "narrowed": narrowed})
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


@pytest.fixture()
def delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    scratch_root = tmp_path / "scratch-root"
    scratch_root.mkdir()
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(scratch_root))
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
        "from reckon.crew import queue_landing\n\ndef test_real():\n    pass\n",
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
            "dispatched_at": datetime.now(UTC).isoformat(),
            "base_sha": base,
            "suite_command": SUITE,
        },
    )
    base_tree = tmp_path / "base-tree"
    base_tree.mkdir()
    monkeypatch.setattr(
        landing_measure,
        "base_checkout",
        lambda repo, sha, timeout=0.0: _Yield(base_tree),
    )
    monkeypatch.setattr(
        landing_measure, "_RUN", _stub([], suite_head=(0, "3 passed\n"), isolated={})
    )
    # Promotion is stubbed: a refusing case that reaches it fails loudly rather
    # than merging anything into the fixture's repository.
    monkeypatch.setattr(
        queue_landing,
        "_land",
        lambda run_id: pytest.fail("a run with red evidence was promoted"),
    )
    return {
        "repo": repo,
        "base": base,
        "head": head,
        "manifest": manifest,
    }


def _write_arms(
    manifest: Path,
    base: str,
    head: str,
    *,
    base_exit: int = 0,
    base_ids: tuple[str, ...] = (),
    head_exit: int | None = 0,
    head_ids: tuple[str, ...] = (),
    head_completed: bool = True,
    record_head: bool = True,
    base_log: tuple[str, ...] = (),
    head_log: tuple[str, ...] = (),
) -> None:
    """Write the run's own suite pair into its manifest, as a worker does.

    ``base_log`` and ``head_log`` write the gate logs the arms cite, so a
    reader that derives the review's added-failure count from them sees the
    same evidence a delivered run leaves on disk.
    """
    logs = manifest.parent / "gate"
    baseline: dict[str, object] = {
        "revision": base,
        "command": SUITE,
        "exit_status": base_exit,
        "completed": True,
        "failure_ids": list(base_ids),
    }
    if base_log or head_log:
        logs.mkdir(parents=True, exist_ok=True)
    if base_log:
        path = logs / "base.log"
        path.write_text(
            f"# revision: {base}\nEXIT={base_exit}\n" + "\n".join(base_log) + "\n",
            encoding="utf-8",
        )
        baseline["log_path"] = str(path)
    lines = [f"baseline_suite: {json.dumps(baseline)}"]
    if record_head:
        after: dict[str, object] = {
            "revision": head,
            "command": SUITE,
            "completed": head_completed,
            "failure_ids": list(head_ids),
        }
        if head_exit is not None:
            after["exit_status"] = head_exit
        if head_log:
            path = logs / "head.log"
            path.write_text(
                f"# revision: {head}\nEXIT={0 if head_exit is None else head_exit}\n"
                + "\n".join(head_log)
                + "\n",
                encoding="utf-8",
            )
            after["log_path"] = str(path)
        lines.append(f"after_suite: {json.dumps(after)}")
    kept = [
        line
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if not line.startswith(("baseline_suite:", "after_suite:"))
    ]
    manifest.write_text("\n".join([*kept, *lines]) + "\n", encoding="utf-8")


def _accept(
    *,
    base: str,
    head: str,
    findings: tuple[str, ...] = (),
    score: int = 20,
) -> dict:
    """Store a review of the delivered head, as a reviewer's record arrives."""
    text = "\n".join(
        (
            *(f"FINDING tests/test_alpha.py:3 {line}" for line in findings),
            *(
                f"SCORE {dimension}: {score}"
                for dimension in review_module.REVIEW_DIMENSIONS
            ),
        )
    )
    parsed = review_module.parse_review(text)
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
    return parsed


def _refusal(delivery: dict) -> dict:
    result = queue_landing.land_once()
    assert result["landed"] == [], result
    assert len(result["obligations"]) == 1, result
    return result


def _promoted(delivery: dict, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(queue_landing, "_land", lambda run_id: (True, ""))
    result = queue_landing.land_once()
    assert result["landed"] == [RUN], result
    return result


# ── the six cases ───────────────────────────────────────────────────────────


def test_a_red_after_suite_is_not_promoted(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's own head arm exited 1 on a failure its base did not carry."""
    _write_arms(
        delivery["manifest"],
        delivery["base"],
        delivery["head"],
        head_exit=1,
        head_ids=(NEW_FAILURE,),
    )
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))

    result = _refusal(delivery)

    guard = result["obligations"][0]["guard"]
    assert guard.startswith("red-suite:"), guard
    assert "exit status is 1" in guard, guard
    landing = runs.read_pointer(RUN)["admission_landing"]
    assert landing["status"] == "refused", landing
    assert landing["guard"] == guard, landing
    duties = obligations.obligations(PROJECT, SESSION)["obligations"]
    row = next(item for item in duties if item["run_id"] == RUN)
    assert row["kind"] == "landing-refused", row
    assert "red-suite" in str(row.get("reason") or ""), row
    assert "exit status is 1" in str(row.get("reason") or ""), row


def test_a_review_that_added_a_failure_is_not_promoted(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review measured a failure its base log did not carry."""
    _write_arms(
        delivery["manifest"],
        delivery["base"],
        delivery["head"],
        base_log=("3 passed",),
        head_log=(f"FAILED {NEW_FAILURE} - AssertionError",),
    )
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))
    stored = review_module.read_review(
        PROJECT, RUN, reviewed_head_sha=str(delivery["head"])
    )
    assert stored["added_failure_count"] == 1, stored

    result = _refusal(delivery)

    guard = result["obligations"][0]["guard"]
    assert guard.startswith("red-suite:"), guard
    assert "added_failure_count 1" in guard, guard
    assert NEW_FAILURE in guard, guard


def test_a_finding_with_no_disposition_is_not_promoted(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer raised a non-blocking finding and nobody has answered it."""
    _accept(
        base=str(delivery["base"]),
        head=str(delivery["head"]),
        findings=("non-blocking: the helper re-derives a value it could cache",),
    )

    result = _refusal(delivery)

    guard = result["obligations"][0]["guard"]
    assert guard.startswith("red-suite:"), guard
    assert "no disposition" in guard, guard
    assert "non-blocking" in guard, guard


def test_an_unreadable_after_suite_is_not_promoted(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An arm that stopped before its summary line is no evidence at all."""
    _write_arms(
        delivery["manifest"],
        delivery["base"],
        delivery["head"],
        head_exit=None,
        head_completed=False,
    )
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))

    result = _refusal(delivery)

    guard = result["obligations"][0]["guard"]
    assert guard.startswith("red-suite:"), guard
    assert "completed: true" in guard, guard


def test_a_red_exit_with_every_failure_id_in_the_baseline_is_not_promoted(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero added against a red base is still a red gate, and a coordinator's call."""
    _write_arms(
        delivery["manifest"],
        delivery["base"],
        delivery["head"],
        base_exit=1,
        base_ids=(NEW_FAILURE,),
        head_exit=1,
        head_ids=(NEW_FAILURE,),
    )
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))

    result = _refusal(delivery)

    guard = result["obligations"][0]["guard"]
    assert guard.startswith("red-suite:"), guard
    assert "exit status is 1" in guard, guard
    assert "names failures its baseline_suite does not" not in guard, guard
    # No gate passed is recorded: promotion, which is where a gate verdict is
    # written, is stubbed to fail if the pass reaches it.
    assert runs.read_pointer(RUN)["admission_landing"]["status"] == "refused"


def test_a_clean_run_with_every_finding_disposed_is_promoted(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sixth case: clean suite, answered findings, and it lands as before."""
    from reckon.crew import review_dispositions

    _write_arms(
        delivery["manifest"],
        delivery["base"],
        delivery["head"],
        head_exit=0,
        head_ids=(),
    )
    _accept(
        base=str(delivery["base"]),
        head=str(delivery["head"]),
        findings=("non-blocking: the helper re-derives a value it could cache",),
    )
    review_dispositions.record_finding_disposition(
        PROJECT,
        RUN,
        0,
        kind="exempted",
        reason="answered by its coordinator",
        reviewed_head_sha=str(delivery["head"]),
    )

    result = _promoted(delivery, monkeypatch)

    assert result["obligations"] == [], result


# ── one negative control per refusing arm ───────────────────────────────────


def _red_suite_arms(delivery: dict) -> None:
    _write_arms(
        delivery["manifest"],
        delivery["base"],
        delivery["head"],
        head_exit=1,
        head_ids=(NEW_FAILURE,),
    )
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))


def _review_added_failure(delivery: dict) -> None:
    _write_arms(
        delivery["manifest"],
        delivery["base"],
        delivery["head"],
        base_log=("3 passed",),
        head_log=(f"FAILED {NEW_FAILURE} - AssertionError",),
    )
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))


def _undisposed_finding(delivery: dict) -> None:
    _accept(
        base=str(delivery["base"]),
        head=str(delivery["head"]),
        findings=("non-blocking: the case is not exercised by any test",),
    )


def _unreadable_arm(delivery: dict) -> None:
    _write_arms(
        delivery["manifest"],
        delivery["base"],
        delivery["head"],
        head_exit=None,
        head_completed=False,
    )
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))


def _red_base_covered(delivery: dict) -> None:
    _write_arms(
        delivery["manifest"],
        delivery["base"],
        delivery["head"],
        base_exit=1,
        base_ids=(NEW_FAILURE,),
        head_exit=1,
        head_ids=(NEW_FAILURE,),
    )
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))


def _remove_the_suite_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queue_landing, "_manifest_suite_guard", lambda pointer: "")


def _remove_the_added_failure_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queue_landing, "_review_added_failure_guard", lambda review: "")


def _remove_the_undisposed_finding_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queue_landing, "_undisposed_finding_guard", lambda review: "")


def _read_every_arm_as_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treating an unreadable after_suite as clean: the arm is handed on as read."""

    def lenient(manifest):
        after = manifest.get("after_suite")
        return (after if isinstance(after, dict) else None), ""

    monkeypatch.setattr(queue_landing, "_after_suite_arm", lenient)


def _compare_failure_ids_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queue_landing, "_after_suite_exit_guard", lambda after: "")


@pytest.mark.parametrize(
    ("case", "remove"),
    [
        (_red_suite_arms, _remove_the_suite_check),
        (_review_added_failure, _remove_the_added_failure_check),
        (_undisposed_finding, _remove_the_undisposed_finding_check),
        (_unreadable_arm, _read_every_arm_as_clean),
        (_red_base_covered, _compare_failure_ids_alone),
    ],
    ids=(
        "suite-check-removed",
        "added-failure-check-removed",
        "undisposed-finding-check-removed",
        "unreadable-arm-treated-as-clean",
        "failure-ids-compared-alone",
    ),
)
def test_removing_one_arm_promotes_the_case_it_refused(
    delivery, monkeypatch: pytest.MonkeyPatch, case, remove
) -> None:
    """Each refusal is shown to be its own guard's doing, not the pass's."""
    case(delivery)
    remove(monkeypatch)

    result = _promoted(delivery, monkeypatch)

    assert result["obligations"] == [], result
