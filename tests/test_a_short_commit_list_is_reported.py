"""A passing promotion presenting fewer commits than its manifest declares is
reported, so an incomplete presentation stops silently disabling the
out-of-scope boundary check.

The manifest's ``commits:`` line is delivered evidence the promotion already
holds, and the boundary check runs against the commits a promotion presents. A
presentation naming one commit of a manifest's declared three narrows that
check silently, so the guard reports the shortfall — naming how many were
presented, how many the manifest declared, and which revisions are missing —
and never refuses, so a passing promotion still completes.

Only the strict-subset direction is a report. Equality is a full presentation;
a superset, or a presented commit the manifest does not declare, is the
coordinator having declined the worker's revision, where the presented list is
the one that resolves in the repository — reporting it would name the list
that resolves and recreate the confusion the counts cause. A manifest with no
commits line, and a declared revision that does not resolve anywhere, are
both silent: the unresolvable one is neither counted nor named as missing.
The existing commitless guard — a passing gate with an empty list whose
worktree head moved off base — still fires unchanged.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import crew, ledger
from reckon.cli import main as cli_main
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "shortfall"

# A well-formed forty-character revision that resolves to no commit object.
_GONE_40 = "0123456789abcdef0123456789abcdef01234567"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-q", "-m", "chore: seed")
    return root


def _make_commits(repository: Path, names: list[str]) -> list[str]:
    """Create one commit per name and return the canonical shas in order."""
    shas = []
    for name in names:
        path = f"{name}.txt"
        (repository / path).write_text(f"{name}\n", encoding="utf-8")
        _git(repository, "add", path)
        _git(repository, "commit", "-q", "-m", f"feat: add {name}")
        shas.append(_git(repository, "rev-parse", "HEAD"))
    return shas


def _commitless_record(repository: Path, base: str) -> dict:
    return {
        "run_id": "r-fixture",
        "worktree": str(repository),
        "base_sha": base,
        "node": {"role": "implement"},
    }


def _record_with_manifest(repository: Path, path: Path, *, declared: list[str]) -> dict:
    manifest = path / "manifest.md"
    manifest.write_text(
        "node: fixture\n"
        "status: complete\n"
        f"commits: {', '.join(declared)}\n"
        "tests: done\n",
        encoding="utf-8",
    )
    record = _commitless_record(repository, _git(repository, "rev-parse", "HEAD"))
    record["manifest_path"] = str(manifest)
    record["manifest_baseline_mtime_ns"] = 0
    return record


def _shortfall_report(
    record: dict,
    *,
    commits: tuple[str, ...],
) -> dict | None:
    return promotion._require_gate_evidence(
        str(record["run_id"]),
        record,
        verdict="passed",
        commits=commits,
        no_commit_reason="",
    )


# ── The reported shape: a strict subset of the manifest's declared commits ──


def test_presenting_one_of_three_declared_commits_is_reported(
    repository: Path, tmp_path: Path
) -> None:
    first, second, third = _make_commits(repository, ["one", "two", "three"])
    record = _record_with_manifest(
        repository, tmp_path, declared=[first, second, third]
    )

    report = _shortfall_report(record, commits=(first,))

    assert report is not None
    assert report["kind"] == "presented_commits_subset_of_manifest"
    assert report["presented"] == 1
    assert report["declared"] == 3
    assert report["missing"] == sorted([second, third])
    assert second in report["message"]
    assert third in report["message"]


def test_presenting_all_declared_commits_is_silent(
    repository: Path, tmp_path: Path
) -> None:
    first, second, third = _make_commits(repository, ["one", "two", "three"])
    record = _record_with_manifest(
        repository, tmp_path, declared=[first, second, third]
    )

    assert _shortfall_report(record, commits=(first, second, third)) is None


def test_presenting_an_undeclared_commit_is_silent(
    repository: Path, tmp_path: Path
) -> None:
    first, second, third = _make_commits(repository, ["one", "two", "three"])
    extra = _make_commits(repository, ["extra"])[0]
    record = _record_with_manifest(
        repository, tmp_path, declared=[first, second, third]
    )

    # The coordinator declined the worker's revisions and cited its own; the
    # presented list is the one that resolves, so it is not reported.
    assert _shortfall_report(record, commits=(extra,)) is None
    assert _shortfall_report(record, commits=(first, extra)) is None


def test_presenting_a_superset_is_silent(repository: Path, tmp_path: Path) -> None:
    first, second = _make_commits(repository, ["one", "two"])
    extra = _make_commits(repository, ["extra"])[0]
    record = _record_with_manifest(repository, tmp_path, declared=[first, second])

    assert _shortfall_report(record, commits=(first, second, extra)) is None


def test_a_manifest_with_no_commits_line_is_silent_and_does_not_raise(
    repository: Path, tmp_path: Path
) -> None:
    first, second, third = _make_commits(repository, ["one", "two", "three"])
    record = _commitless_record(repository, _git(repository, "rev-parse", "HEAD"))
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "node: fixture\nstatus: complete\ntests: done\n", encoding="utf-8"
    )
    record["manifest_path"] = str(manifest)
    record["manifest_baseline_mtime_ns"] = 0

    assert _shortfall_report(record, commits=(first, second, third)) is None


def test_a_declared_revision_that_does_not_resolve_is_not_named_missing(
    repository: Path, tmp_path: Path
) -> None:
    first, second = _make_commits(repository, ["one", "two"])
    record = _record_with_manifest(
        repository, tmp_path, declared=[first, second, _GONE_40]
    )

    report = _shortfall_report(record, commits=(first,))

    # The manifest quotes a revision that resolves nowhere; it is neither
    # counted nor named as missing, so only the second commit is missing.
    assert report is not None
    assert report["declared"] == 2
    assert report["missing"] == [second]
    assert _GONE_40 not in report["missing"]


def test_a_declared_revision_that_does_not_resolve_alone_is_silent(
    repository: Path, tmp_path: Path
) -> None:
    first = _make_commits(repository, ["one"])[0]
    record = _record_with_manifest(repository, tmp_path, declared=[first, _GONE_40])

    # The only declared revision that resolves is the one presented: nothing
    # is missing, so a single unresolvable manifest entry reports nothing.
    assert _shortfall_report(record, commits=(first,)) is None


# ── The existing commitless guard is unchanged ──────────────────────────────


def test_the_commitless_guard_still_fires_on_a_moved_worktree(
    repository: Path,
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    record = {
        "run_id": "r-fixture",
        "worktree": str(repository),
        "base_sha": base,
        "node": {"role": "implement"},
    }
    _make_commits(repository, ["moved"])

    with pytest.raises(crew.CrewError) as refusal:
        promotion._require_gate_evidence(
            "r-fixture", record, verdict="passed", commits=(), no_commit_reason=""
        )
    assert "cites no commit" in str(refusal.value)
    assert "--no-commit" in str(refusal.value)


def test_the_commitless_guard_still_consults_the_manifest_first(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    landed = _make_commits(repository, ["landed"])[0]
    record = {
        "run_id": "r-fixture",
        "worktree": str(repository),
        "base_sha": base,
        "node": {"role": "implement"},
    }
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        f"node: fixture\nstatus: complete\ncommits: {landed[:8]}\n",
        encoding="utf-8",
    )
    record["manifest_path"] = str(manifest)
    record["manifest_baseline_mtime_ns"] = 0

    with pytest.raises(crew.CrewError) as refusal:
        promotion._require_gate_evidence(
            "r-fixture", record, verdict="passed", commits=(), no_commit_reason=""
        )
    assert "manifest records 1" in str(refusal.value)
    assert landed[:8] in str(refusal.value)


# ── Nothing is refused: the promotion completes and the report lands ────────


def test_a_shortfall_promotion_completes_and_reports_on_the_record(
    repository: Path, tmp_path: Path
) -> None:
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    base = _git(repository, "rev-parse", "HEAD")
    first, second, third = _make_commits(repository, ["one", "two", "three"])
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "node: fixture\n"
        "status: complete\n"
        f"commits: {first}, {second}, {third}\n"
        "tests: done\n",
        encoding="utf-8",
    )
    run_id = "r-shortfall"
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": base,
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-09-08T09:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": "missing-plan",
                "section": "guard",
                "time_budget": "20m",
                "write_paths": ["one.txt"],
            },
        },
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "passed",
            "--commit",
            first,
            "--checkout-path",
            str(repository),
            "--gate-command",
            "pytest -q",
            "--gate-exit-status",
            "0",
            "--gate-log-path",
            "/durable/shortfall.log",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    shortfall = payload["commit_list_shortfall"]
    assert shortfall["presented"] == 1
    assert shortfall["declared"] == 3
    assert shortfall["missing"] == sorted([second, third])
    stored = payload["record"]
    assert stored["commits"] == [first]
    assert stored["commit_list_shortfall"]["declared"] == 3
    assert not pointer_path(run_id).exists()
    rows = ledger.runs(PROJECT, root=repository)
    assert [row["run_id"] for row in rows] == [run_id]
