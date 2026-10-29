"""A run whose review raises no blocking finding lands without a coordinator.

The landing pass used to accept a review only when it carried no finding of any
severity, so a review that raised two non-blocking observations left the run
waiting for a coordinator who had nothing to add. These cases drive the pass
itself: the review is stored, the run is delivered, and the assertions are about
what the pass did — whether it landed, and what it wrote down when it refused.

Every repository, store and live pointer resolves inside a temporary
configuration home, so nothing here reads or writes the operator's own fleet.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import landing_measure, queue_landing, review_dispositions, runs
from reckon.crew import review as review_module

PROJECT = "landing-fixture"
PLAN = "delivery-plan"
SESSION = "owner"
RUN = "r-delivered-implementation"
SUITE = "python -m pytest -q tests/test_alpha.py"
REAL = "tests/test_alpha.py::test_real"
LANDED_REASON = "non-blocking, noted at landing"


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


def _pointer(
    *,
    repo: Path,
    base: str,
    manifest: Path,
    session: str = SESSION,
    with_suite: bool = False,
    delivered_at: datetime | None = None,
    worktree: Path | None = None,
) -> dict:
    pointer = {
        "run_id": RUN,
        "project": PROJECT,
        "session": session,
        "repo": str(repo),
        "worktree": str(worktree or repo),
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
        "dispatched_at": (delivered_at or datetime.now(UTC)).isoformat(),
    }
    if with_suite:
        pointer["base_sha"] = base
        pointer["suite_command"] = SUITE
    return pointer


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
        _pointer(repo=repo, base=base, manifest=manifest),
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
        "manifest": manifest,
    }


def _accept(
    *,
    base: str,
    head: str,
    findings: tuple[str, ...] = (),
    score: int = 20,
    per_dimension: dict[str, int] | None = None,
) -> dict:
    """Store a review of the delivered head.
    ``findings`` are the finding lines' text, each written after its file and
    line, so a caller states the severity in the same place a reviewer does.
    ``per_dimension`` states each score, for a caller that needs a total other
    than six equal scores.
    """
    scores = (
        per_dimension
        if per_dimension is not None
        else dict.fromkeys(review_module.REVIEW_DIMENSIONS, score)
    )
    text = "\n".join(
        (
            *(f"FINDING tests/test_alpha.py:3 {line}" for line in findings),
            *(f"SCORE {dimension}: {value}" for dimension, value in scores.items()),
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


def _land(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    landed: list[str] = []

    def fake(run_id: str) -> tuple[bool, str]:
        landed.append(run_id)
        return True, ""

    monkeypatch.setattr(queue_landing, "_land", fake)
    return landed


def _stored_record(head: str) -> dict:
    record = review_dispositions.stored_record_for_disposition(
        PROJECT, RUN, reviewed_head_sha=head
    )
    assert record is not None, "the review record the pass wrote to is missing"
    return record


def _stored_review_file(head: str) -> Path:
    """The review record on disk for the delivered head."""
    home = Path(os.environ["RECKON_HOME"])
    for path in sorted(home.rglob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("reviewed_run_id") != RUN or "scores" not in record:
            continue
        if str(record.get("reviewed_head_sha") or "") != head:
            continue
        return path
    raise AssertionError("no stored review record for the delivered head")


def _rewrite_total(head: str, total: int | None) -> None:
    """Rewrite the stored total behind the review store's derivation.

    The store derives a record's total from its dimension scores on write, so
    the only way to drive a record whose total disagrees with its scores, or
    which carries none, is to write the file the reader opens.
    """
    path = _stored_review_file(head)
    record = json.loads(path.read_text(encoding="utf-8"))
    if total is None:
        record.pop("total", None)
        record.pop("reviewer_total", None)
    else:
        record["total"] = total
    path.write_text(json.dumps(record), encoding="utf-8")


def _rewrite_score(head: str, dimension: str, value: int) -> None:
    """Write one dimension score behind the parser's own range refusal.

    The parser raises rather than store a score outside the schema, so the only
    way to drive a record the reader will open with one is to write the file.
    """
    path = _stored_review_file(head)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["scores"][dimension] = value
    record["total"] = sum(record["scores"].values())
    path.write_text(json.dumps(record), encoding="utf-8")


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
        narrowed = next((token for token in argv if "::" in token), "")
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


# ── arm 1: two non-blocking findings land in one pass, both exempted ─────────


def test_two_non_blocking_findings_land_in_one_pass_and_are_recorded_exempted(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(
        base=str(delivery["base"]),
        head=str(delivery["head"]),
        findings=(
            "non-blocking: the helper re-derives a value it could cache",
            "non-blocking: the docstring names two dimensions of three",
        ),
    )
    landed = _land(monkeypatch)

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    assert landed == [RUN]
    record = _stored_record(str(delivery["head"]))
    dispositions = record["finding_dispositions"]
    assert set(dispositions) == {"0", "1"}, dispositions
    for entry in dispositions.values():
        assert entry["kind"] == "exempted"
        assert entry["reason"] == LANDED_REASON


# ── the score sets no floor ─────────────────────────────────────────────────


def test_a_review_scoring_92_with_two_non_blocking_findings_lands(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """92 is well under the nine tenths this rule used to demand, and lands.

    The reviewer scored the work and raised nothing blocking, so there is no
    reading of the record a coordinator would add.
    """
    dimensions = review_module.REVIEW_DIMENSIONS
    per_dimension = {
        dimension: 16 if index < 2 else 15 for index, dimension in enumerate(dimensions)
    }
    assert sum(per_dimension.values()) == 92
    _accept(
        base=str(delivery["base"]),
        head=str(delivery["head"]),
        findings=(
            "non-blocking: the helper re-derives a value it could cache",
            "non-blocking: the docstring names two dimensions of three",
        ),
        per_dimension=per_dimension,
    )
    landed = _land(monkeypatch)

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    assert result["obligations"] == []
    assert landed == [RUN]
    record = _stored_record(str(delivery["head"]))
    assert set(record["finding_dispositions"]) == {"0", "1"}


def test_a_total_that_disagrees_with_its_scores_is_judged_on_severity_alone(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))
    _rewrite_total(str(delivery["head"]), 5)
    landed = _land(monkeypatch)

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    assert landed == [RUN]


def test_a_record_carrying_no_total_is_judged_on_severity_alone(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))
    _rewrite_total(str(delivery["head"]), None)
    landed = _land(monkeypatch)

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    assert landed == [RUN]


# ── a refused landing records no exemption ─────────────────────────────────


def test_a_refused_landing_leaves_the_stored_record_unchanged(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exemptions say a landing happened. A refused landing must not say it did."""
    _accept(
        base=str(delivery["base"]),
        head=str(delivery["head"]),
        findings=("non-blocking: the helper re-derives a value it could cache",),
    )
    before = _stored_record(str(delivery["head"]))
    monkeypatch.setattr(
        queue_landing, "_land", lambda run_id: (False, "the branch refused the merge")
    )

    result = queue_landing.land_once()

    assert result["landed"] == [], result
    assert result["obligations"] == [
        {
            "run_id": RUN,
            "session": SESSION,
            "guard": "the branch refused the merge",
        }
    ]
    after = _stored_record(str(delivery["head"]))
    assert after == before
    assert "finding_dispositions" not in after


# ── completeness is the last schema check on the review side ────────────────


def _refused_as_incomplete(delivery, monkeypatch: pytest.MonkeyPatch, head: str):
    """Drive the pass over a stored record and assert it refused the schema."""
    before = _stored_record(head)
    monkeypatch.setattr(
        queue_landing, "_land", lambda run_id: pytest.fail("unreadable review landed")
    )

    result = queue_landing.land_once()

    assert result["landed"] == [], result
    assert len(result["obligations"]) == 1, result
    guard = result["obligations"][0]["guard"]
    assert "incomplete record" in guard, guard
    after = _stored_record(head)
    assert after == before
    assert "finding_dispositions" not in after
    return result


def test_a_review_missing_one_dimension_score_is_not_landed(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    dimensions = review_module.REVIEW_DIMENSIONS
    _accept(
        base=str(delivery["base"]),
        head=str(delivery["head"]),
        per_dimension=dict.fromkeys(dimensions[:-1], 20),
    )
    _refused_as_incomplete(delivery, monkeypatch, str(delivery["head"]))


def test_a_dimension_score_outside_the_schema_is_not_landed(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))
    _rewrite_score(str(delivery["head"]), review_module.REVIEW_DIMENSIONS[-1], 25)
    _refused_as_incomplete(delivery, monkeypatch, str(delivery["head"]))


# ── arm 2: a blocking finding is not landed ─────────────────────────────────


def test_a_blocking_finding_is_not_landed(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(
        base=str(delivery["base"]),
        head=str(delivery["head"]),
        findings=("blocking: the new branch has no test that can fail",),
    )
    monkeypatch.setattr(
        queue_landing, "_land", lambda run_id: pytest.fail("blocked run landed")
    )

    result = queue_landing.land_once()

    assert result["landed"] == []
    assert result["obligations"] == [
        {
            "run_id": RUN,
            "session": SESSION,
            "guard": "review acceptance: a blocking finding or an incomplete record",
        }
    ]
    assert runs.pointer_path(RUN).exists()
    record = _stored_record(str(delivery["head"]))
    assert "finding_dispositions" not in record


def test_an_unstated_severity_withholds_the_landing_like_a_blocking_one(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(
        base=str(delivery["base"]),
        head=str(delivery["head"]),
        findings=("the guard is not exercised by any test",),
    )
    monkeypatch.setattr(
        queue_landing, "_land", lambda run_id: pytest.fail("unmarked finding landed")
    )

    result = queue_landing.land_once()

    assert result["landed"] == []


# ── arm 3: a persisting importer-population failure is not landed ───────────


def test_a_persisting_population_failure_is_not_landed_and_names_the_test(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))
    runs._write_json(
        runs.pointer_path(RUN),
        _pointer(
            repo=delivery["repo"],
            base=str(delivery["base"]),
            manifest=delivery["manifest"],
            with_suite=True,
        ),
    )
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
    monkeypatch.setattr(
        queue_landing, "_land", lambda run_id: pytest.fail("regressing run landed")
    )

    result = queue_landing.land_once()

    assert result["landed"] == []
    guard = result["obligations"][0]["guard"]
    assert "added suite regression" in guard, guard
    assert REAL in guard, guard
    assert "3/3 at head" in guard, guard
    # The isolated re-runs really happened, three times at each revision.
    narrowed = [call for call in calls if call["narrowed"] == REAL]
    assert len(narrowed) == 6, calls
    assert {call["arm"] for call in narrowed} == {"base", "head"}


# ── arm 4: an added failure that passes alone at both revisions lands ───────


def test_an_added_failure_that_passes_alone_at_both_revisions_lands(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))
    runs._write_json(
        runs.pointer_path(RUN),
        _pointer(
            repo=delivery["repo"],
            base=str(delivery["base"]),
            manifest=delivery["manifest"],
            with_suite=True,
        ),
    )
    calls: list[dict] = []
    landed = _land(monkeypatch)
    monkeypatch.setattr(
        landing_measure,
        "_RUN",
        _stub(calls, suite_head=(1, f"FAILED {REAL} - AssertionError\n"), isolated={}),
    )

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    assert landed == [RUN]
    narrowed = [call for call in calls if call["narrowed"] == REAL]
    assert len(narrowed) == 6, calls


# ── arm 5: a run owned by a session idle for a day still lands ──────────────


def test_a_run_owned_by_an_idle_session_lands_like_any_other(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(base=str(delivery["base"]), head=str(delivery["head"]))
    runs._write_json(
        runs.pointer_path(RUN),
        _pointer(
            repo=delivery["repo"],
            base=str(delivery["base"]),
            manifest=delivery["manifest"],
            session="coordinator-away-since-yesterday",
            delivered_at=datetime.now(UTC) - timedelta(days=1),
        ),
    )
    landed = _land(monkeypatch)

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    assert landed == [RUN]


# ── arm 6: a conflicting head is not landed and the paths are named ─────────


def test_a_conflicting_head_is_not_landed_and_names_the_paths(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = delivery["repo"]
    head = str(delivery["head"])
    _accept(base=str(delivery["base"]), head=head)
    # The run's own worktree stays at the head it delivered, as a real worker's
    # detached worktree does, while the primary branch moves on.
    run_tree = repo.parent / "run-worktree"
    _git(repo, "worktree", "add", "--detach", str(run_tree), head)
    runs._write_json(
        runs.pointer_path(RUN),
        _pointer(
            repo=repo,
            base=str(delivery["base"]),
            manifest=delivery["manifest"],
            worktree=run_tree,
        ),
    )
    # The primary branch then moves on from the run's base to a different edit
    # of the file the run changed, so neither side is the other's ancestor and
    # the two edits collide.
    _git(repo, "checkout", "-q", "--detach", str(delivery["base"]))
    (repo / "reckon" / "crew" / "queue_landing.py").write_text(
        "VALUE = 3\n", encoding="utf-8"
    )
    _git(repo, "add", "reckon/crew/queue_landing.py")
    _git(repo, "commit", "-q", "-m", "feat: a competing edit")
    primary = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "-f", "main", primary)
    _git(repo, "checkout", "-q", "main")
    monkeypatch.setattr(
        queue_landing, "_land", lambda run_id: pytest.fail("conflicting run landed")
    )

    result = queue_landing.land_once()

    assert result["landed"] == []
    guard = result["obligations"][0]["guard"]
    assert "conflicts with the primary branch" in guard, guard
    assert "reckon/crew/queue_landing.py" in guard, guard
    # Nothing was written to the branch: its head is the competing commit, not
    # a merge of the run's head.
    assert _git(repo, "rev-parse", "HEAD") == primary
    assert _git(repo, "rev-parse", "HEAD") != head
