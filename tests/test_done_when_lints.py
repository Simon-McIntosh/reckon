"""Dispatch refuses measures that can pass without the claimed evidence."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew, crew_dispatch_commands

CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m"},
}


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "package").mkdir()
    (root / "tests").mkdir()
    (root / "docs" / "plans" / "sample-plan.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="sample-plan">'
        '<h2 id="sample">Sample</h2>',
        encoding="utf-8",
    )
    (root / "package" / "target.py").write_text("value = 1\n", encoding="utf-8")
    (root / "tests" / "test_preexisting.py").write_text(
        "def test_preexisting_failure():\n    assert 1 == 2\n", encoding="utf-8"
    )
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs", "package", "tests"),
        (
            "commit",
            "-q",
            "-m",
            "chore: seed fixture",
            "-m",
            "Create isolated repository for dispatch validation.",
        ),
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    home = tmp_path / "home"
    home.mkdir()
    (home / "mounts.json").write_text(
        json.dumps({"sample": str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setattr(crew_dispatch_commands, "_resolved_flight", lambda *_args, **_kwargs: CONFIG)
    return root


def _verdict(repository: Path, done_when: str) -> tuple[int, dict]:
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "dispatch",
            "--project",
            "sample",
            "--plan",
            "sample-plan",
            "--section",
            "sample",
            "--spec-level",
            "exact",
            "--node",
            "sample",
            "--goal",
            "record one measured result",
            "--done-when",
            done_when,
            "--write-path",
            "package/target.py",
            "--session",
            "fixture-session",
            "--repo",
            str(repository),
            "--dry-run",
        ],
    )
    return result.exit_code, json.loads(result.output)


def _commit_assertion(repository: Path) -> None:
    subprocess.run(
        ["git", "add", "tests/test_preexisting.py"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "commit",
            "-q",
            "-m",
            "test: change base assertion",
            "-m",
            "Record the fixture's new committed base verdict.",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
    )


def test_independent_reading_requires_numeric_agreement(repository: Path) -> None:
    derived_seconds = 6885.08
    independent_seconds = 6000.0
    assert abs(derived_seconds - independent_seconds) / independent_seconds > 0.0001
    band = (
        "pytest tests/test_model_time.py reports derived share inside 1% to 99% "
        "and compares the same quantity with an independent reading"
    )
    refused, payload = _verdict(repository, band)
    assert refused != 0
    assert "agreement" in json.dumps(payload).lower()

    accepted, payload = _verdict(repository, band + "; values agree within 0.01%")
    assert accepted == 0, payload
    accepted_tolerance, payload = _verdict(
        repository, band + "; agreement tolerance of 0.01%"
    )
    assert accepted_tolerance == 0, payload

    refused_again, payload = _verdict(repository, band)
    assert refused_again != 0

    unrelated, payload = _verdict(
        repository,
        "pytest tests/test_preconditions.py checks independent preconditions; "
        "measure 2 recorded refusals",
    )
    assert unrelated == 0, payload


def test_absence_markers_need_coverage_floor_and_base_count(repository: Path) -> None:
    cells = ["explicitly-unmeasured"] * 5
    assert all(cell in {"measured", "explicitly-unmeasured"} for cell in cells)
    absence = (
        "pytest tests/test_coverage.py reports all 5 cells measured or "
        "explicitly-unmeasured, before value of 0 fully measured rows"
    )
    refused, payload = _verdict(repository, absence)
    assert refused != 0
    assert "coverage" in json.dumps(payload).lower()

    missing_base, _ = _verdict(
        repository, absence + "; measured coverage is at least 5% of rows"
    )
    assert missing_base != 0
    missing_floor, _ = _verdict(repository, absence + "; base count 803 rows")
    assert missing_floor != 0

    accepted, payload = _verdict(
        repository,
        absence + "; measured coverage is at least 5% of base count 803 rows",
    )
    assert accepted == 0, payload


def test_absolute_green_accounts_for_red_named_base(repository: Path) -> None:
    check = "pytest tests/test_preexisting.py exits 0, before value of 14 failing tests"
    admitted, payload = _verdict(repository, check)
    assert admitted == 0, payload
    assert payload["validation"]["ok"]
    assert len(payload["done_when_warnings"]) == 1
    assert payload["done_when_warnings"][0]["check"] == "tests/test_preexisting.py"
    assert "unstated or uncited" in payload["done_when_warnings"][0]["detail"]

    for delta in (
        "adds no failure to base",
        "adds no failure",
        "no regressions versus base",
        "same failures as base",
        "no added failures by id against the base",
    ):
        admitted_delta, payload = _verdict(repository, check + "; " + delta)
        assert admitted_delta == 0, payload
        assert payload["done_when_warnings"] == []

    flagged_check = "pytest -q tests/test_preexisting.py exits 0"
    flagged_admitted, payload = _verdict(repository, flagged_check)
    assert flagged_admitted == 0, payload
    assert payload["done_when_warnings"][0]["check"] == "tests/test_preexisting.py"

    (repository / "tests" / "test_preexisting.py").write_text(
        "def test_preexisting_failure():\n    assert 1 == 1\n", encoding="utf-8"
    )
    _commit_assertion(repository)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    accepted, payload = _verdict(repository, check + f"; green at base {revision}")
    assert accepted == 0, payload
    assert payload["done_when_warnings"] == []
    cited, payload = _verdict(repository, check + f"; against base {revision}")
    assert cited == 0, payload
    assert payload["done_when_warnings"] == []

    (repository / "tests" / "test_preexisting.py").write_text(
        "def test_preexisting_failure():\n    assert 1 == 2\n", encoding="utf-8"
    )
    _commit_assertion(repository)
    admitted_again, payload = _verdict(repository, check)
    assert admitted_again == 0, payload
    assert payload["done_when_warnings"][0]["check"] == "tests/test_preexisting.py"

    marker = repository / "test-was-executed"
    (repository / "tests" / "test_side_effect.py").write_text(
        "from pathlib import Path\n"
        "def test_side_effect():\n"
        f"    Path({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    side_effect_admitted, payload = _verdict(
        repository, "pytest tests/test_side_effect.py exits 0, before value of 0"
    )
    assert side_effect_admitted == 0, payload
    assert payload["done_when_warnings"][0]["check"] == "tests/test_side_effect.py"
    assert not marker.exists()

    record = crew.dispatch(
        node=crew.TaskNode(
            id="actual-dispatch",
            goal="record one measured result",
            plan="sample-plan",
            section="sample",
            spec_level="exact",
            done_when=check,
            write_paths=["package/target.py"],
            time_budget="20m",
        ),
        project="sample",
        repo=repository,
        config=CONFIG,
        session="fixture-session",
        launcher=lambda *_args, **_kwargs: 4242,
    )
    assert record["done_when_warnings"][0]["check"] == "tests/test_preexisting.py"
    assert not marker.exists()
