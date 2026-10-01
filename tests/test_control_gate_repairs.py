"""A gate log is judged against the arm it is cited for.

The manifest template requires a gate log's first line to name the revision it
ran at, the tree and the command, and nothing checked it: a log named for one
arm could carry another arm's run — measured as a log named for the new file
alone carrying the whole population — so a claim about one arm rested on a
different log than the one cited. The write-time audit now holds each suite
arm's log against the revision that arm records, so a log naming no revision,
or another arm's, is reported while its writer still holds a turn.

The presented side of the same question is repaired beside it: a promotion
presenting a commit that resolves to no object is refused by value where the
presentation is read, rather than continuing past that guard with the value
silently filtered out of its comparison.
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
from reckon.crew.reports import audit_manifest
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "gate-log"

# A forty-character hexadecimal value that resolves to no commit object.
_GONE_40 = "0123456789abcdef0123456789abcdef01234567"

_BASE_REVISION = "1f0c5a7d94e2b6839c4d5e6f708192a3b4c5d6e7"
_HEAD_REVISION = "9a8b7c6d5e4f3021122334455667788990aabbcc"

_GIT = "git"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        [_GIT, *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
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


def _commit_of(repository: Path, name: str) -> str:
    path = f"{name}.txt"
    (repository / path).write_text(f"{name}\n", encoding="utf-8")
    _git(repository, "add", path)
    _git(repository, "commit", "-q", "-m", f"feat: add {name}")
    return _git(repository, "rev-parse", "HEAD")


def _arm_manifest(
    manifest_path: Path,
    *,
    base_revision: str,
    head_revision: str,
    base_log: str,
    head_log: str,
) -> str:
    """A manifest whose two suite arms cite their logs, as the template writes them."""
    baseline = {
        "revision": base_revision,
        "command": "pytest -q",
        "exit_status": 0,
        "log_path": base_log,
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
    }
    after = dict(baseline, revision=head_revision, log_path=head_log)
    body = [
        "node: gate-log-fixture",
        "status: complete",
        f"commits: {head_revision}",
        "changed_paths: reckon/crew/promotion.py",
        "tests: pytest tests/example.py -> 1 passed",
        "baseline_suite: " + json.dumps(baseline),
        "after_suite: " + json.dumps(after),
    ]
    manifest_path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return manifest_path.read_text(encoding="utf-8")


def _log(path: Path, first_line: str) -> Path:
    path.write_text(f"{first_line}\n1 passed\nEXIT=0\n", encoding="utf-8")
    return path


# ── A gate log that names no revision on its first line ─────────────────────


def test_a_gate_log_naming_no_revision_on_its_first_line_is_reported(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.md"
    head_log = _log(
        tmp_path / "head-gate.log",
        "the runner's progress line, written before anything was observed",
    )
    _log(tmp_path / "base-gate.log", f"rev={_BASE_REVISION} tree=/tmp/base")
    body = _arm_manifest(
        manifest_path,
        base_revision=_BASE_REVISION,
        head_revision=_HEAD_REVISION,
        base_log="base-gate.log",
        head_log="head-gate.log",
    )

    audit = audit_manifest(body, manifest_path=manifest_path)

    findings = [finding for finding in audit["findings"] if str(head_log) in finding]
    assert findings, audit["findings"]
    assert any("revision" in finding for finding in findings)


# ── A log cited for one arm that records another arm's run ──────────────────


def test_a_gate_log_naming_another_arms_revision_is_reported_with_both(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.md"
    base_log = _log(
        tmp_path / "base-gate.log",
        f"rev={_HEAD_REVISION} tree=/tmp/base cwd=/tmp/base label=base",
    )
    _log(tmp_path / "head-gate.log", f"rev={_HEAD_REVISION} tree=/tmp/head")
    body = _arm_manifest(
        manifest_path,
        base_revision=_BASE_REVISION,
        head_revision=_HEAD_REVISION,
        base_log="base-gate.log",
        head_log="head-gate.log",
    )

    audit = audit_manifest(body, manifest_path=manifest_path)

    findings = [finding for finding in audit["findings"] if str(base_log) in finding]
    assert findings, audit["findings"]
    assert any(
        _HEAD_REVISION in finding and _BASE_REVISION in finding for finding in findings
    ), findings


def test_a_base_log_carrying_the_base_arms_own_revision_is_not_reported(
    tmp_path: Path,
) -> None:
    """The pair is accepted when each arm's log names the revision it records."""
    manifest_path = tmp_path / "manifest.md"
    _log(tmp_path / "base-gate.log", f"rev={_BASE_REVISION} tree=/tmp/base label=base")
    _log(tmp_path / "head-gate.log", f"rev={_HEAD_REVISION} tree=/tmp/head label=head")
    body = _arm_manifest(
        manifest_path,
        base_revision=_BASE_REVISION,
        head_revision=_HEAD_REVISION,
        base_log="base-gate.log",
        head_log="head-gate.log",
    )

    audit = audit_manifest(body, manifest_path=manifest_path)

    assert audit["findings"] == [], audit["findings"]


# ── The presented list: an identifier that resolves to no object ────────────


def _record(repository: Path, manifest_path: Path, *, base: str, name: str) -> dict:
    return {
        "run_id": name,
        "worktree": str(repository),
        "base_sha": base,
        "manifest_path": str(manifest_path),
        "manifest_baseline_mtime_ns": 0,
    }


def _presented_manifest(repository: Path, tmp_path: Path, committed: str) -> Path:
    manifest = tmp_path / "presented-manifest.md"
    manifest.write_text(
        "node: presented-fixture\n"
        "status: complete\n"
        f"commits: {committed}\n"
        "tests: pytest tests/example.py -> 1 passed\n",
        encoding="utf-8",
    )
    return manifest


def test_a_presented_sha_that_resolves_to_no_object_is_refused_by_value(
    tmp_path: Path,
) -> None:
    """The unit the promotion reads: the presented value is reported, not dropped.

    At the unfixed guard both calls below return ``None``: the second drops the
    unresolvable entry from its comparison and reports nothing about it, so the
    assertion under test here is the refusal that names the value.
    """
    repository = _repository(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")
    landed = _commit_of(repository, "landed")
    manifest = _presented_manifest(repository, tmp_path, landed)
    record = _record(repository, manifest, base=base, name="r-presented")

    assert (
        promotion._require_gate_evidence(
            "r-presented",
            record,
            verdict="passed",
            commits=(landed,),
            no_commit_reason="",
        )
        is None
    )

    with pytest.raises(crew.CrewError) as refusal:
        promotion._require_gate_evidence(
            "r-presented",
            record,
            verdict="passed",
            commits=(landed, _GONE_40),
            no_commit_reason="",
        )

    assert _GONE_40 in str(refusal.value)
    assert "does not resolve" in str(refusal.value)


def test_a_promotion_presenting_an_unresolvable_sha_names_it_in_the_refusal(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")
    landed = _commit_of(repository, "landed")
    manifest = _presented_manifest(repository, tmp_path, landed)
    run_id = "r-presented-cli"
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
            "manifest_baseline_mtime_ns": 0,
            "node": {
                "id": "node-presented",
                "plan": "missing-plan",
                "section": "presented",
                "time_budget": "20m",
                "write_paths": ["landed.txt"],
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
            landed,
            "--commit",
            _GONE_40,
            "--checkout-path",
            str(repository),
            "--gate-command",
            "pytest -q",
            "--gate-exit-status",
            "0",
            "--gate-log-path",
            "/durable/presented.log",
        ],
    )

    assert result.exit_code != 0, result.output
    assert _GONE_40 in result.output
    # Nothing was recorded, so the refusal is not a report beside a stored row.
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).exists()
