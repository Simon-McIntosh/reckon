"""An implementation dispatch requires an answered review of plan content."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, flight
from reckon.crew import node as node_module
from reckon.crew import plan_review

CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "in-harness",
            "model": "test-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {
        role: {"backend": "worker", "execution_capable": True}
        for role in ("implement", "investigate", "review", "test")
    },
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


@pytest.fixture
def reviewed_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        source_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    plan_path = plans / "fixture.html"
    plan_path.write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
<meta name="plan-title" content="Fixture">
<meta name="plan-status" content="active">
<meta name="plan-impl" content="0.0">
<meta name="plan-modified" content="2026-09-25">
<meta name="plan-version" content="1">
</head><body><h2 id="delivery">Delivery</h2><p>Ship one measured change.</p></body></html>
""",
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo, plan_path


def _node(config_home: Path, *, role: str = "implement", name: str = "delivery"):
    return crew.TaskNode(
        id=name,
        goal="ship one measured change",
        plan="fixture",
        section="delivery",
        role=role,
        spec_level="exact",
        done_when="pytest reports one passing plan review gate case",
        # A restricted verifier role writes only its delivery, outside the
        # repository, so its node is otherwise dispatchable and the exemption
        # under test is the plan-review gate alone.
        write_paths=(
            [str(config_home / "crew" / "reports" / f"{name}.md")]
            if role == "test"
            else ["src/change.py"]
        ),
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


def _plan(
    node: crew.TaskNode,
    repo: Path,
    *,
    mode: str | None = None,
    config: dict | None = None,
    **kwargs,
):
    """Resolve one dispatch, selecting the gate's mode through the config key.

    ``mode`` is layered onto the base config the way a caller setting the key
    would; ``config`` replaces the base outright, which is how a case drives the
    gate from a config a flight layer actually produced. With neither, the key
    is absent from the base and the gate takes its shipped report-only default,
    which is what the report-mode cases assert.
    """
    base = CONFIG if config is None else config
    if mode is not None:
        base = {**base, "plan_review_gate": mode}
    return crew.plan_dispatch(
        node=node,
        config=base,
        project="sample",
        repo=repo,
        base="HEAD",
        **kwargs,
    )


def _store_answered_review(plan_path: Path, config_home: Path) -> None:
    plan_review.store_plan_review(
        {
            "project": "sample",
            "plan_slug": "fixture",
            "plan_version": 1,
            "rubric": "plan_review",
            "reviewed_blob_sha": "a" * 40,
            "plan_fingerprint": plan_review.plan_fingerprint(plan_path),
            "findings": [
                {
                    "id": "reuse-owner",
                    "type": "reuse",
                    "text": "name the existing owner",
                }
            ],
            "responses": {
                "reuse-owner": {
                    "action": "declined",
                    "reason": "the named owner is already the module being extended",
                }
            },
            "status": "declined",
            "review_run_id": "r-plan-review",
        },
        # Stored to the crew review store root the gate reads through, resolved
        # from RECKON_HOME, so the fixture writes where production reads.
        base_dir=config_home / "crew" / "reviews",
    )


UNANSWERED_FINDING_ID = "orphan-finding"


def _store_unanswered_review(plan_path: Path, config_home: Path) -> str:
    """Store a review whose one finding no response answers.

    The finding is listed and ``responses`` is empty, which is the state the
    gate must treat as unread rather than answered. Returns the finding id so a
    case can assert the refusal names the specific finding left open.
    """
    plan_review.store_plan_review(
        {
            "project": "sample",
            "plan_slug": "fixture",
            "plan_version": 1,
            "rubric": "plan_review",
            "reviewed_blob_sha": "b" * 40,
            "plan_fingerprint": plan_review.plan_fingerprint(plan_path),
            "findings": [
                {
                    "id": UNANSWERED_FINDING_ID,
                    "type": "coverage",
                    "text": "the plan omits its rollback step",
                }
            ],
            "responses": {},
            "status": "open",
            "review_run_id": "r-plan-review",
        },
        base_dir=config_home / "crew" / "reviews",
    )
    return UNANSWERED_FINDING_ID


def test_unreviewed_implementation_is_refused_with_a_composed_remedy(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, _plan_path = reviewed_project

    expected_error = getattr(node_module, "PlanReviewMissingError", crew.CrewError)
    with pytest.raises(expected_error) as excinfo:
        _plan(_node(config_home), repo, mode="enforce")

    refusal = str(excinfo.value)
    assert "fixture" in refusal
    assert "composed plan-review dispatch" in refusal


def test_answered_advisory_findings_admit_the_implementation(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, plan_path = reviewed_project
    _store_answered_review(plan_path, config_home)

    resolution = _plan(_node(config_home), repo)

    assert resolution.validation.ok is True


@pytest.mark.parametrize("role", ["review", "investigate", "test"])
def test_non_implementation_roles_are_exempt(
    reviewed_project: tuple[Path, Path, Path], role: str
) -> None:
    config_home, repo, _plan_path = reviewed_project

    resolution = _plan(_node(config_home, role=role, name=f"{role}-fixture"), repo)

    assert resolution.validation.ok is True


def test_composed_review_identity_uses_the_same_exemption(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, _plan_path = reviewed_project

    resolution = _plan(
        _node(config_home, role="implement", name="review-of-fixture"), repo
    )

    assert resolution.validation.ok is True


def test_metadata_only_edit_keeps_the_review_current(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, plan_path = reviewed_project
    original_fingerprint = plan_review.plan_fingerprint(plan_path)
    _store_answered_review(plan_path, config_home)
    edited = plan_path.read_text(encoding="utf-8")
    edited = edited.replace('content="0.0"', 'content="0.5"')
    edited = edited.replace('content="2026-09-25"', 'content="2026-09-26"')
    edited = edited.replace('content="1"', 'content="2"')
    plan_path.write_text(edited, encoding="utf-8")
    subprocess.run(
        ["git", "add", "docs/plans/fixture.html"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "docs: update plan metadata"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    assert plan_review.plan_fingerprint(plan_path) == original_fingerprint
    assert _plan(_node(config_home), repo).validation.ok is True


def test_unreviewed_waiver_is_recorded_on_the_run_pointer(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, _plan_path = reviewed_project
    node = _node(config_home, name="waived-delivery")

    record = crew.dispatch(
        node=node,
        project="sample",
        repo=repo,
        config=CONFIG,
        session="waiver-session",
        unreviewed_plan_override=True,
    )

    waiver = record["unreviewed_plan_override"]
    assert waiver == {"requested": True, "plan": "fixture"}
    assert crew.read_pointer(record["run_id"])["unreviewed_plan_override"] == waiver


def test_a_finding_left_unanswered_refuses_the_build_by_name(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, plan_path = reviewed_project
    finding_id = _store_unanswered_review(plan_path, config_home)

    expected_error = getattr(node_module, "PlanReviewMissingError", crew.CrewError)
    with pytest.raises(expected_error) as excinfo:
        _plan(_node(config_home), repo, mode="enforce")

    refusal = str(excinfo.value)
    assert finding_id in refusal
    assert "unanswered" in refusal


def test_report_only_mode_records_a_missing_review_and_lets_the_dispatch_run(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    """The shipped default: no review yet, so a warning and the dispatch goes.

    Nearly no plan carries a review while the node that writes one is unbuilt,
    so an enforced gate would be disabled by its operators rather than answered.
    The default records the same sentence an enforced refusal would carry as a
    warning on the result, so the condition is visible without being fatal.
    """
    config_home, repo, _plan_path = reviewed_project

    resolution = _plan(_node(config_home), repo)

    assert resolution.validation.ok is True
    warnings = resolution.as_dict()["warnings"]
    assert any("report-only" in warning for warning in warnings)
    assert any("fixture" in warning for warning in warnings)


def test_report_only_mode_records_an_unanswered_finding_by_name(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, plan_path = reviewed_project
    finding_id = _store_unanswered_review(plan_path, config_home)

    resolution = _plan(_node(config_home), repo)

    assert resolution.validation.ok is True
    warnings = resolution.as_dict()["warnings"]
    assert any(finding_id in warning for warning in warnings)


def test_dry_run_names_the_plan_review_error_key(
    reviewed_project: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validating dry run points at the review, not at the plan's mounts.

    The same condition answered ``plan-unavailable`` on this path while the
    launching path answered ``plan-review-missing``, so an operator diagnosing
    with --dry-run was sent to the mounts for what was a missing review.
    """
    _config_home, repo, _plan_path = reviewed_project
    monkeypatch.setattr(
        cli_module,
        "_resolved_flight",
        lambda *args, **kwargs: {**CONFIG, "plan_review_gate": "enforce"},
    )

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "dispatch",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--section",
            "delivery",
            "--role",
            "implement",
            "--spec-level",
            "exact",
            "--node",
            "dry-run-delivery",
            "--goal",
            "ship one measured change",
            "--done-when",
            "pytest reports one passing plan review gate case",
            "--write-path",
            "src/change.py",
            "--session",
            "dry-run-session",
            "--repo",
            str(repo),
            "--dry-run",
        ],
    )

    payload = json.loads(result.stdout.splitlines()[0])
    assert result.exit_code == 4
    assert payload["error"] == "plan-review-missing"
    assert "fixture" in payload["detail"]


def test_dry_run_in_report_only_mode_admits_and_carries_the_warning(
    reviewed_project: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_home, repo, _plan_path = reviewed_project
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *args, **kwargs: CONFIG)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "dispatch",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--section",
            "delivery",
            "--role",
            "implement",
            "--spec-level",
            "exact",
            "--node",
            "dry-run-delivery",
            "--goal",
            "ship one measured change",
            "--done-when",
            "pytest reports one passing plan review gate case",
            "--write-path",
            "src/change.py",
            "--session",
            "dry-run-session",
            "--repo",
            str(repo),
            "--dry-run",
        ],
    )

    payload = json.loads(result.stdout.splitlines()[0])
    assert result.exit_code == 0
    assert any("report-only" in warning for warning in payload["warnings"])


def test_the_shipped_default_layer_names_the_report_only_mode(
    tmp_path: Path,
) -> None:
    resolved = flight.resolve(host_path=tmp_path / "absent.yaml")

    assert resolved.config[flight.PLAN_REVIEW_GATE_KEY] == "report"


def test_a_config_layer_selects_the_enforced_mode(tmp_path: Path) -> None:
    host = tmp_path / "enforce.yaml"
    host.write_text("plan_review_gate: enforce\n", encoding="utf-8")

    resolved = flight.resolve(host_path=host)

    assert resolved.config[flight.PLAN_REVIEW_GATE_KEY] == "enforce"
    assert flight.plan_review_gate_enforces(resolved.config) is True


def test_the_layer_selected_mode_drives_the_gate(
    reviewed_project: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """End to end through a layer: a host file's mode becomes the gate's.

    The key is checked by flight rather than by the generated schema, so this is
    the case that proves the mode a real configuration layer sets is the mode
    the gate reads — not merely a value passed straight into a resolver.
    """
    config_home, repo, _plan_path = reviewed_project
    host = tmp_path / "enforce.yaml"
    host.write_text(
        json.dumps(
            {
                **CONFIG,
                # The shipped layer declares the review role read-only, and a
                # role overlay merges onto it rather than replacing it, so this
                # layer states the sandbox its own execution-capable roles need.
                "roles": {
                    role: {**settings, "sandbox": "worktree-full"}
                    for role, settings in CONFIG["roles"].items()
                },
                "plan_review_gate": "enforce",
            }
        ),
        encoding="utf-8",
    )
    resolved = flight.resolve(host_path=host)

    expected_error = getattr(node_module, "PlanReviewMissingError", crew.CrewError)
    with pytest.raises(expected_error):
        _plan(_node(config_home), repo, config=resolved.config)


def test_a_mode_outside_the_declared_set_is_refused_naming_the_key(
    tmp_path: Path,
) -> None:
    host = tmp_path / "bad.yaml"
    host.write_text("plan_review_gate: off\n", encoding="utf-8")

    with pytest.raises(flight.FlightConfigError) as excinfo:
        flight.resolve(host_path=host)

    refusal = str(excinfo.value)
    assert "plan_review_gate" in refusal
    assert "report" in refusal and "enforce" in refusal
