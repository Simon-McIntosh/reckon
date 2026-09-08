"""Dispatch briefs point to plan authority instead of reproducing its prose."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew
from reckon.crew.dispatch import DONE_WHEN_PLAN_TEXT_SPAN_WORDS

dispatch_module = importlib.import_module("reckon.crew.dispatch")

COPIED_TEXT = (
    "The coordinator records the exact evidence boundary before launch so every "
    "later reader can distinguish a concise pointer from copied explanatory prose "
    "without revisiting the source document or guessing why the worker received "
    "duplicated authority inside its brief"
)
COPIED_WORDS = COPIED_TEXT.split()

CONFIG = {
    "default_backend": "local",
    "backends": {
        "local": {
            "launch": "in-harness",
            "model": "fixture-model",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    fleet_scripts = root / "skills" / "reckon-ship" / "scripts"
    plans.mkdir(parents=True)
    fleet_scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (fleet_scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    reflowed = "\n        ".join(COPIED_WORDS)
    (plans / "dispatch-contract.html").write_text(
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="fixture">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="dispatch-contract">'
        "</head><body>"
        '<h2 id="dispatch">Dispatch contract</h2>'
        f"<p>{reflowed}</p>"
        "<p>The gate names <code>reckon/crew/dispatch.py</code> as its file, "
        "then names <code>compose_prompt</code> as its symbol, and records "
        "<code>95</code> percent as its numeric threshold.</p>"
        '<h2 id="later">Later section</h2>'
        "<p>This text must not be included in the dispatch section.</p>"
        "</body></html>",
        encoding="utf-8",
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/dispatch-contract.html"],
        ["commit", "-q", "-m", "chore: seed repository"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"fixture": str(root / "docs")}), encoding="utf-8"
    )
    return root


def _arguments(repo: Path, done_when: str, *, dry_run: bool) -> list[str]:
    arguments = [
        "crew",
        "dispatch",
        "--project",
        "fixture",
        "--plan",
        "dispatch-contract",
        "--section",
        "dispatch",
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        "brief-overlap",
        "--goal",
        "report copied dispatch prose",
        "--done-when",
        done_when,
        "--write-path",
        "result.json",
        "--session",
        "fixture-session",
        "--repo",
        str(repo),
    ]
    if dry_run:
        arguments.append("--dry-run")
    else:
        arguments.append("--no-watch")
    return arguments


def _invoke(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    done_when: str,
    *,
    dry_run: bool = True,
):
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *_a, **_k: CONFIG)
    monkeypatch.setattr(
        cli_module, "_model_availability_refusal", lambda *_a, **_k: None
    )
    return CliRunner().invoke(
        cli_module.main,
        _arguments(repo, done_when, dry_run=dry_run),
    )


def _payload(result) -> dict:
    return json.loads(result.output.splitlines()[0])


def _overlap_reports(payload: dict) -> list[str]:
    return [
        warning
        for warning in payload["warnings"]
        if warning.startswith("done-when reproduces")
    ]


@pytest.mark.parametrize(
    "overlap_length",
    range(
        DONE_WHEN_PLAN_TEXT_SPAN_WORDS - 3,
        DONE_WHEN_PLAN_TEXT_SPAN_WORDS + 4,
    ),
)
def test_the_report_boundary_is_the_declared_word_count(
    dispatch_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    overlap_length: int,
) -> None:
    overlap = " ".join(COPIED_WORDS[:overlap_length])
    result = _invoke(
        dispatch_repo,
        monkeypatch,
        f"pytest reports the dispatch gate passing. {overlap}",
    )

    payload = _payload(result)
    reports = _overlap_reports(payload)
    assert result.exit_code == 0
    assert bool(reports) is (overlap_length >= DONE_WHEN_PLAN_TEXT_SPAN_WORDS)
    if reports:
        assert overlap in reports[0]


def test_a_copied_span_is_named_quoted_and_recorded_on_the_run(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlap = " ".join(COPIED_WORDS)
    result = _invoke(
        dispatch_repo,
        monkeypatch,
        f"pytest reports the dispatch gate passing. {overlap}",
        dry_run=False,
    )

    payload = _payload(result)
    reports = _overlap_reports(payload)
    pointer = crew.read_pointer(payload["run_id"])
    assert result.exit_code == 0
    assert len(reports) == 1
    assert "dispatch-contract" in reports[0]
    assert "dispatch" in reports[0]
    assert overlap in reports[0]
    assert reports[0] in pointer["warnings"]


def test_a_file_symbol_and_threshold_are_only_pointers(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(
        dispatch_repo,
        monkeypatch,
        "pytest proves reckon/crew/dispatch.py remains importable while "
        "compose_prompt stays named at the 95 percent threshold",
    )

    payload = _payload(result)
    assert result.exit_code == 0
    assert _overlap_reports(payload) == []


def test_reflowing_the_copied_paragraph_does_not_hide_it(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlap = "   \n  ".join(COPIED_WORDS)
    result = _invoke(
        dispatch_repo,
        monkeypatch,
        f"pytest reports the dispatch gate passing. {overlap}",
    )

    reports = _overlap_reports(_payload(result))
    assert result.exit_code == 0
    assert " ".join(COPIED_WORDS) in reports[0]


def test_unrelated_evidence_produces_no_report(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(
        dispatch_repo,
        monkeypatch,
        "pytest reports five isolated routing checks passing with no copied prose",
    )

    payload = _payload(result)
    assert result.exit_code == 0
    assert _overlap_reports(payload) == []


def test_an_unreadable_section_cannot_break_dispatch(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(**_kwargs):
        raise OSError("unreadable after visibility validation")

    monkeypatch.setattr(dispatch_module, "_resolved_plan_section_text", unreadable)
    result = _invoke(
        dispatch_repo,
        monkeypatch,
        f"pytest reports the dispatch gate passing. {' '.join(COPIED_WORDS)}",
    )

    payload = _payload(result)
    assert result.exit_code == 0
    assert _overlap_reports(payload) == []


def test_a_report_changes_no_other_dry_run_field(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatch_module, "new_run_id", lambda _node: "r-fixed")
    done_when = "pytest reports one stable dry-run result"
    monkeypatch.setattr(
        dispatch_module,
        "_done_when_plan_overlap_warning",
        lambda **_kwargs: "done-when reproduces fixture prose",
    )
    reported = _payload(_invoke(dispatch_repo, monkeypatch, done_when))
    monkeypatch.setattr(
        dispatch_module,
        "_done_when_plan_overlap_warning",
        lambda **_kwargs: None,
    )
    quiet = _payload(_invoke(dispatch_repo, monkeypatch, done_when))

    assert _overlap_reports(reported) == ["done-when reproduces fixture prose"]
    reported["warnings"] = []
    assert reported == quiet
