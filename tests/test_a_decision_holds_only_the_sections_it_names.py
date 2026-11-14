"""Dispatch and admission consume the roadmap's section-scoped decision holds."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from reckon._plan_html import parse_plan, read_state_file, write_state
from reckon._schema import gen_json_schema
from reckon.crew import plan_review, queue_admission, runs
from reckon.crew.queue_admission import LaneHeld
from reckon.roadmap import build_roadmap
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures
from tests import test_codex_inbox_trust as trust_tests

dispatch_module = importlib.import_module("reckon.crew.dispatch")
hook_probe = trust_tests.hook_probe


@pytest.fixture()
def decision_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(tmp_path / "config" / "mounts.json"))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(repository / "docs" / "state"))
    return repository


def _seed(repository: Path, sections: list[str], choice: str = "") -> Path:
    path = repository / "docs" / "plans" / f"{fixtures.PLAN}.html"
    state = {
        "slug": fixtures.PLAN,
        "type": "plan",
        "status": "active",
        "section_declarations": {"s2": "implementable", "s3": "implementable"},
        "decisions": {
            "transport": {
                "title": "Which transport carries the payload?",
                "choice": choice,
                "sections": sections,
            }
        },
    }
    bare = (
        '<html><head><meta name="docs-project" content="proj"></head><body>'
        '<h2 id="s2">Independent work</h2><h2 id="s3">Transport work</h2>'
        "</body></html>"
    )
    path.write_text(write_state(bare, state), encoding="utf-8")
    fixtures._run(repository, "add", "docs/plans/plan-a.html")
    fixtures._run(
        repository,
        "commit",
        "-q",
        "-m",
        "docs: declare transport decision",
        "-m",
        "Declare the sections that depend on selecting a transport.",
    )
    return path


def _node(repository: Path, section: str):
    return fixtures._node(
        f"build-{section}",
        section,
        manifest_path=str(repository / f"{section}-manifest.md"),
    )


def _dispatch(repository: Path, section: str) -> dict:
    config = {
        **fixtures.CONFIG,
        "backends": {
            "clive": {
                "launch": "in-harness",
                "model": "test-model",
                "sandbox": "worktree-full",
                "time_budget": "25m",
            }
        },
    }
    return dispatch_module.dispatch(
        node=_node(repository, section),
        project=fixtures.PROJECT,
        repo=repository,
        config=config,
        session=fixtures.SESSION,
        backend_override="clive",
        route="deterministic",
        check_budget=False,
        watch_override=True,
        unreviewed_plan_override=True,
    )


@pytest.mark.parametrize(
    ("sections", "choice", "held_sections"),
    [
        (["s3"], "", {"s3"}),
        ([], "", {"s2", "s3"}),
        (["s3"], "socket", set()),
        ([], "socket", set()),
    ],
    ids=["scoped-open", "whole-open", "scoped-locked", "whole-locked"],
)
def test_dispatch_holds_only_the_sections_the_open_decision_names(
    decision_repo: Path, sections: list[str], choice: str, held_sections: set[str]
) -> None:
    _seed(decision_repo, sections, choice)
    for section in ("s2", "s3"):
        if section in held_sections:
            with pytest.raises(LaneHeld) as caught:
                _dispatch(decision_repo, section)
            assert caught.value.queued["phase"] == "awaiting-upstream"
            assert "transport" in caught.value.queued["reason"]
            assert any(
                row["kind"] == "decision" and row["id"] == "transport"
                for row in caught.value.queued["blockers"]
            )
        else:
            result = _dispatch(decision_repo, section)
            assert result.get("queued") is not True, result


@pytest.mark.parametrize(
    ("sections", "choice", "held_sections"),
    [
        (["s3"], "", {"s3"}),
        ([], "", {"s2", "s3"}),
        (["s3"], "socket", set()),
        ([], "socket", set()),
    ],
    ids=["scoped-open", "whole-open", "scoped-locked", "whole-locked"],
)
def test_admission_uses_the_same_decision_hold(
    decision_repo: Path,
    sections: list[str],
    choice: str,
    held_sections: set[str],
    hook_probe,
) -> None:
    _seed(decision_repo, sections, choice)
    stored = queue_admission.queue_requests(
        [_node(decision_repo, section) for section in ("s2", "s3")],
        project=fixtures.PROJECT,
        session=fixtures.SESSION,
        repo_root=decision_repo,
        config=fixtures.CONFIG,
    )
    assert len(stored) == 2
    for pointer in stored.values():
        expected = (
            "awaiting-upstream" if pointer["section"] in held_sections else "queued"
        )
        assert pointer["phase"] == expected, pointer
    launched: list[str] = []
    result = queue_admission.admit_once(
        config_for=fixtures._config_for(), launcher=fixtures._launcher(launched)
    )
    expected_runs = {
        run_id
        for run_id, pointer in stored.items()
        if pointer["section"] not in held_sections
    }
    assert not result["failed"], result.get("failure_detail")
    assert set(result["launched"]) == expected_runs, result
    assert len(launched) == len(expected_runs), result
    assert len(hook_probe) == len(launched)
    for pointer in runs.list_live(project=fixtures.PROJECT):
        if pointer["node"]["section"] in held_sections:
            assert pointer["phase"] == "awaiting-upstream", pointer


def test_the_reader_schema_and_roadmap_report_the_named_sections(
    decision_repo: Path,
) -> None:
    path = _seed(decision_repo, ["s3"])
    assert read_state_file(path)["decisions"]["transport"]["sections"] == ["s3"]
    parsed = parse_plan(path)
    assert parsed["decisions"][0]["sections"] == ["s3"]
    schema = gen_json_schema()["$defs"]["Decision"]["properties"]["sections"]
    assert schema["type"] == "array"
    assert schema["items"]["type"] == "string"
    report = build_roadmap(
        fixtures.PROJECT, [parsed], [], docs_dir=decision_repo / "docs", review={}
    )
    row = next(item for item in report["pending_work"] if item["slug"] == fixtures.PLAN)
    assert row["section_verdicts"]["s2"]["ready"] is True
    held = row["section_verdicts"]["s3"]
    assert held["ready"] is False
    assert [(item["kind"], item["id"]) for item in held["blockers"]] == [
        ("decision", "transport")
    ]
    # Admission's bounded reader consumes these verdicts rather than deriving
    # another rule for the same decision.
    readiness, _ = plan_review.section_readiness_rows(
        fixtures.PROJECT, decision_repo / "docs", fixtures.PLAN
    )
    assert readiness[fixtures.PLAN]["sections"] == row["section_verdicts"]


def test_hold_details_reuse_the_verdict_that_supplied_the_wait(
    decision_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(decision_repo, ["s3"])
    original = plan_review.section_readiness_rows
    builds = []

    def counting(*args, **kwargs):
        builds.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(plan_review, "section_readiness_rows", counting)
    node = _node(decision_repo, "s3")
    cache = {}
    refs, _, unmet = queue_admission.dispatch_waits(
        node, repo_root=decision_repo, project=fixtures.PROJECT, readiness_cache=cache
    )
    assert unmet
    assert builds
    before = len(builds)
    blockers = queue_admission.dispatch_wait_blockers(
        node,
        repo_root=decision_repo,
        project=fixtures.PROJECT,
        refs=refs,
        readiness_cache=cache,
    )
    assert [(item["kind"], item["id"]) for item in blockers] == [
        ("decision", "transport")
    ]
    assert len(builds) == before
