"""Queued requests follow section and request dependencies across projects."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import queue_admission, runs
from reckon.crew.dispatch_admission import LaneHeld
from reckon.crew.node import CrewError, TaskNode
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

dispatch_module = importlib.import_module("reckon.crew.dispatch")


@pytest.fixture()
def projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    first = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    second = tmp_path / "other-checkout"
    (second / "docs" / "plans").mkdir(parents=True)
    (second / "docs" / "state" / "other").mkdir(parents=True)
    (second / "docs" / "state" / "other" / "index.json").write_text(
        json.dumps({"project": "other", "data": {"_version": 0}}),
        encoding="utf-8",
    )
    _write_other_plan(second)
    _git(second, "init", "-q", "-b", "main")
    _git(second, "config", "user.email", "worker@example.invalid")
    _git(second, "config", "user.name", "Worker")
    _git(second, "add", "--", "docs")
    _git(second, "commit", "-q", "-m", "chore: seed checkout")
    mounts = tmp_path / "config" / "mounts.json"
    mounts.write_text(
        json.dumps(
            {
                fixtures.PROJECT: str(first / "docs"),
                "other": str(second / "docs"),
            }
        ),
        encoding="utf-8",
    )
    return first, second


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write_other_plan(
    repository: Path,
    *,
    declarations: dict[str, str] | None = None,
    waits: dict[str, list[str]] | None = None,
    gates: str = "",
) -> None:
    declarations = declarations or {"s1": "implementable", "s2": "implementable"}
    content = (
        '<!doctype html><html><head><meta name="docs-project" content="other">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="plan-b">'
        '<meta name="plan-status" content="active">'
        f'<meta name="plan-section-declarations" content="{_attribute(declarations)}">'
        f'<meta name="plan-section-depends-on" content="{_attribute(waits or {})}">'
        '</head><body><h2 id="s1">Section one</h2>'
        '<h2 id="s2">Section two</h2>'
        f"{gates}</body></html>"
    )
    (repository / "docs" / "plans" / "plan-b.html").write_text(
        content, encoding="utf-8"
    )


def _attribute(value: object) -> str:
    return json.dumps(value).replace('"', "&quot;")


def _node(identifier: str, section: str, *, after: list[str] | None = None) -> TaskNode:
    return TaskNode(
        id=identifier,
        goal="build the waiting work",
        plan=fixtures.PLAN,
        section=section,
        after=after or [],
        done_when="pytest reports passed for the waiting work",
        write_paths=[f"{identifier}.txt"],
        time_budget="20m",
        spec_level="exact",
    )


def _queue(
    first: Path, *nodes: TaskNode, run_ids: dict[str, str] | None = None
) -> dict:
    return queue_admission.queue_requests(
        nodes,
        project=fixtures.PROJECT,
        session=fixtures.SESSION,
        repo_root=first,
        config=fixtures.CONFIG,
        run_ids=run_ids,
    )


def _dispatch(first: Path, node: TaskNode, *, run_id: str) -> dict:
    try:
        return crew.dispatch(
            node=node,
            project=fixtures.PROJECT,
            repo=first,
            config=fixtures.CONFIG,
            session=fixtures.SESSION,
            run_id=run_id,
            check_budget=False,
            watch_override=True,
            unreviewed_plan_override=True,
            local=True,
            launcher=lambda *args, **kwargs: pytest.fail("wait must not launch"),
        )
    except LaneHeld as held:
        return dict(held.queued or {})


def _admit() -> tuple[dict, list[str]]:
    launched: list[str] = []
    result = queue_admission.admit_once(
        config_for=fixtures._config_for(),
        launcher=fixtures._launcher(launched),
    )
    return result, launched


def _upstream_record(
    first: Path, run_id: str, *, status: str, revision: str | None
) -> None:
    manifest = runs.run_dir(run_id) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"status: {status}\n", encoding="utf-8")
    pointer = runs.pointer_path(run_id)
    if pointer.exists():
        pointer.unlink()
    row = ledger.run_path(fixtures.PROJECT, run_id)
    row.parent.mkdir(parents=True, exist_ok=True)
    row.write_text(
        json.dumps(
            {"run_id": run_id, **({"promoted_revision": revision} if revision else {})}
        ),
        encoding="utf-8",
    )


def test_foreign_section_waits_for_its_declaration(projects: tuple[Path, Path]) -> None:
    first, second = projects
    fixtures._declare_wait_refs(first, section="s2", refs=["other:plan-b#s2"])
    stored = _queue(first, _node("waiting", "s2"), run_ids={"waiting": "r-waiting"})
    assert stored["r-waiting"]["phase"] == "awaiting-upstream"
    _, launched = _admit()
    assert launched == []

    _write_other_plan(second, declarations={"s1": "implementable", "s2": "done"})
    _git(second, "add", "--", "docs/plans/plan-b.html")
    _git(second, "commit", "-q", "-m", "chore: finish section")
    result, launched = _admit()
    assert result["promoted_upstream"] == ["r-waiting"]
    assert launched, result


def test_foreign_section_already_done_queues_directly(
    projects: tuple[Path, Path],
) -> None:
    first, second = projects
    fixtures._declare_wait_refs(first, section="s2", refs=["other:plan-b#s2"])
    _write_other_plan(second, declarations={"s1": "implementable", "s2": "done"})
    _git(second, "add", "--", "docs/plans/plan-b.html")
    _git(second, "commit", "-q", "-m", "chore: finish section")
    stored = _queue(first, _node("waiting", "s2"), run_ids={"waiting": "r-waiting"})
    assert stored["r-waiting"]["phase"] == "queued"


def test_section_preflight_does_not_scan_scope_claims(
    projects: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = projects
    fixtures._declare_wait_refs(first, section="s2", refs=["other:plan-b#s2"])
    _write_other_plan(second, declarations={"s1": "implementable", "s2": "done"})
    monkeypatch.setattr(
        queue_admission,
        "_readiness_rows",
        lambda *args, **kwargs: pytest.fail("dispatch scanned portfolio claims"),
    )
    assert (
        queue_admission.unmet_waits(
            _node("waiting", "s2"), repo_root=first, project=fixtures.PROJECT
        )
        == []
    )


def test_batch_and_dispatch_share_the_queue_writer(
    projects: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = projects
    calls: list[str] = []
    original = dispatch_module._queue_validated_waits

    def record_write(pointer, **kwargs):
        calls.append(str(pointer["run_id"]))
        return original(pointer, **kwargs)

    monkeypatch.setattr(dispatch_module, "_queue_validated_waits", record_write)
    _queue(first, _node("batch", "s2"), run_ids={"batch": "r-batch"})
    _dispatch(first, _node("ordinary", "s3"), run_id="r-ordinary")

    assert calls == ["r-batch", "r-ordinary"]


def test_mutual_section_cycle_refuses_without_storage(
    projects: tuple[Path, Path],
) -> None:
    first, second = projects
    fixtures._declare_wait_refs(first, section="s1", refs=["other:plan-b#s1"])
    _write_other_plan(second, waits={"s1": ["proj:plan-a#s1"]})
    _git(second, "add", "--", "docs/plans/plan-b.html")
    _git(second, "commit", "-q", "-m", "chore: declare dependency")
    with pytest.raises(CrewError) as caught:
        _dispatch(first, _node("cyclic", "s1"), run_id="r-cyclic")
    assert "other:plan-b" in str(caught.value)
    assert "proj:plan-a" in str(caught.value)
    assert runs.list_live(project=fixtures.PROJECT) == []


def test_request_and_section_cycle_refuses_without_storing_second(
    projects: tuple[Path, Path],
) -> None:
    first, _ = projects
    fixtures._declare_wait_refs(first, section="s2", refs=["plan-a#s1"])
    _dispatch(first, _node("prior", "s2"), run_id="r-prior")
    with pytest.raises(CrewError) as caught:
        _dispatch(first, _node("cyclic", "s1", after=["r-prior"]), run_id="r-cyclic")
    assert "r-prior" in str(caught.value)
    assert "plan-a" in str(caught.value)
    assert [row["run_id"] for row in runs.list_live(project=fixtures.PROJECT)] == [
        "r-prior"
    ]


@pytest.mark.parametrize(
    "ref", ["other:absent#s1", "other:plan-b#absent", "absent:plan#s1"]
)
def test_missing_plan_section_or_project_refuses_at_queue_time(
    projects: tuple[Path, Path], ref: str
) -> None:
    first, _ = projects
    fixtures._declare_wait_refs(first, section="s2", refs=[ref])
    with pytest.raises(CrewError, match="missing"):
        _dispatch(first, _node("waiting", "s2"), run_id="r-waiting")
    assert runs.list_live(project=fixtures.PROJECT) == []


def test_missing_request_refuses_at_queue_time(projects: tuple[Path, Path]) -> None:
    first, _ = projects
    with pytest.raises(CrewError, match="missing request"):
        _dispatch(first, _node("waiting", "s1", after=["r-absent"]), run_id="r-waiting")
    assert runs.list_live(project=fixtures.PROJECT) == []


def test_unshipped_plan_dependency_holds_its_section(
    projects: tuple[Path, Path],
) -> None:
    first, _ = projects
    path = first / "docs" / "plans" / "plan-a.html"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        '<meta name="plan-status" content="active">',
        '<meta name="plan-status" content="active">'
        '<meta name="plan-depends-on" content="other:plan-b">',
    )
    path.write_text(text, encoding="utf-8")
    _git(first, "add", "--", "docs/plans/plan-a.html")
    _git(first, "commit", "-q", "-m", "chore: declare dependency")

    stored = _queue(first, _node("waiting", "s1"), run_ids={"waiting": "r-waiting"})
    assert "other:plan-b" in stored["r-waiting"]["waits"]
    assert stored["r-waiting"]["phase"] == "awaiting-upstream"


@pytest.mark.parametrize("status", ["blocked", "failed"])
def test_closed_upstream_blocks_its_waiter(
    projects: tuple[Path, Path], status: str
) -> None:
    first, _ = projects
    _queue(first, _node("prior", "s1"), run_ids={"prior": "r-prior"})
    stored = _queue(
        first,
        _node("waiting", "s2", after=["r-prior"]),
        run_ids={"waiting": "r-waiting"},
    )
    assert stored["r-waiting"]["phase"] == "awaiting-upstream"
    _upstream_record(first, "r-prior", status=status, revision=None)
    result, launched = _admit()
    assert launched == []
    if status == "blocked":
        # A blocked manifest is a resumable stop, not a closure: the upstream
        # run may still deliver when it is resumed, so its waiter keeps
        # waiting rather than being closed as abandoned.
        assert result["blocked_upstream"] == []
        live = {
            str(row["run_id"]): row for row in runs.list_live(project=fixtures.PROJECT)
        }
        assert live["r-waiting"]["phase"] == "awaiting-upstream"
        return
    assert result["blocked_upstream"] == ["r-waiting"]


def test_complete_upstream_waits_for_its_revision_to_land(
    projects: tuple[Path, Path],
) -> None:
    first, _ = projects
    fixtures._declare_wait_refs(first, section="s2", refs=[])
    _queue(first, _node("prior", "s1"), run_ids={"prior": "r-prior"})
    _queue(
        first,
        _node("waiting", "s2", after=["r-prior"]),
        run_ids={"waiting": "r-waiting"},
    )
    _upstream_record(first, "r-prior", status="complete", revision=None)
    result, launched = _admit()
    assert launched == []
    assert result["upstream_obligations"] == [
        {"run_id": "r-waiting", "detail": "r-prior: promoted_revision absent"}
    ]

    base = _git(first, "rev-parse", "HEAD")
    revision = _git(
        first,
        "commit-tree",
        f"{base}^{{tree}}",
        "-p",
        base,
        "-m",
        "chore: upstream result",
    )
    _upstream_record(first, "r-prior", status="complete", revision=revision)
    result, launched = _admit()
    assert result["promoted_upstream"] == []
    assert launched == []

    _git(first, "merge", "--ff-only", revision)
    result, launched = _admit()
    assert result["promoted_upstream"] == ["r-waiting"]
    assert launched


def test_base_revision_releases_a_promoted_request_that_committed_nothing(
    projects: tuple[Path, Path],
) -> None:
    first, _ = projects
    fixtures._declare_wait_refs(first, section="s2", refs=[])
    _queue(first, _node("prior", "s1"), run_ids={"prior": "r-prior"})
    _queue(
        first,
        _node("waiting", "s2", after=["r-prior"]),
        run_ids={"waiting": "r-waiting"},
    )
    _upstream_record(
        first, "r-prior", status="complete", revision=_git(first, "rev-parse", "HEAD")
    )
    result, launched = _admit()
    assert result["promoted_upstream"] == ["r-waiting"]
    assert launched


def test_refused_manifest_status_holds_and_raises_one_obligation(
    projects: tuple[Path, Path],
) -> None:
    first, _ = projects
    _queue(first, _node("prior", "s1"), run_ids={"prior": "r-prior"})
    _queue(
        first,
        _node("waiting", "s2", after=["r-prior"]),
        run_ids={"waiting": "r-waiting"},
    )
    _upstream_record(
        first, "r-prior", status="promoted", revision=_git(first, "rev-parse", "HEAD")
    )
    result, launched = _admit()
    assert launched == []
    assert len(result["upstream_obligations"]) == 1
    assert "r-prior" in result["upstream_obligations"][0]["detail"]


@pytest.mark.parametrize("gated", ["s2", "s1,s2"])
def test_foreign_gate_hold_keeps_waiter_waiting(
    projects: tuple[Path, Path], gated: str
) -> None:
    first, second = projects
    fixtures._declare_wait_refs(first, section="s2", refs=["other:plan-b#s2"])
    gate = (
        '<section data-reckon="gates"><div class="r-gate" data-id="measure" '
        f'data-status="open" data-verdict="" data-gated-sections="{gated}">'
        '<h4 class="r-gate-measure">pass</h4></div></section>'
    )
    _write_other_plan(second, gates=gate)
    _git(second, "add", "--", "docs/plans/plan-b.html")
    _git(second, "commit", "-q", "-m", "chore: author gate")
    stored = _queue(first, _node("waiting", "s2"), run_ids={"waiting": "r-waiting"})
    assert stored["r-waiting"]["phase"] == "awaiting-upstream"
    _, launched = _admit()
    assert launched == []


def test_readiness_recheck_uses_project_section_verdict(
    projects: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = projects
    from reckon.crew import plan_review

    calls: list[str] = []
    real = plan_review.project_section_readiness

    def observed(project: str, docs_dir: Path) -> dict:
        calls.append(project)
        return real(project, docs_dir)

    monkeypatch.setattr(plan_review, "project_section_readiness", observed)
    fixtures._declare_wait_refs(first, section="s2", refs=["plan-a#s1"])
    _queue(first, _node("waiting", "s2"), run_ids={"waiting": "r-waiting"})
    calls.clear()
    _admit()
    assert calls.count(fixtures.PROJECT) == 1


def test_scoped_decision_holds_only_its_section(
    projects: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = projects
    from reckon.crew import plan_review

    fixtures._declare_wait_refs(first, section="s2", refs=[])
    real = plan_review.project_section_readiness

    def scoped(project: str, docs_dir: Path) -> dict:
        answer = real(project, docs_dir)
        if project == fixtures.PROJECT:
            answer[fixtures.PLAN]["readiness"] = "blocked"
            answer[fixtures.PLAN]["sections"]["s1"] = {
                "section": "s1",
                "ready": True,
                "blockers": [],
            }
            answer[fixtures.PLAN]["sections"]["s2"] = {
                "section": "s2",
                "ready": False,
                "blockers": [{"kind": "decision", "key": "choice"}],
            }
        return answer

    monkeypatch.setattr(plan_review, "project_section_readiness", scoped)
    stored = _queue(
        first,
        _node("ready", "s1"),
        _node("held", "s2"),
        run_ids={"ready": "r-ready", "held": "r-held"},
    )
    assert stored["r-ready"]["phase"] == "queued"
    assert stored["r-held"]["phase"] == "awaiting-upstream"


def test_after_is_serialized_for_a_brief_only_node() -> None:
    node = TaskNode(id="brief", goal="work", plan="", after=["r-prior"])
    assert node.as_dict()["after"] == ["r-prior"]


def test_after_survives_the_queued_pointer_round_trip(
    projects: tuple[Path, Path],
) -> None:
    first, _ = projects
    _queue(first, _node("prior", "s1"), run_ids={"prior": "r-prior"})
    _queue(
        first,
        _node("waiting", "s2", after=["r-prior"]),
        run_ids={"waiting": "r-waiting"},
    )
    pointer = runs.read_pointer("r-waiting")
    assert queue_admission._node_from_pointer(pointer).after == ["r-prior"]
