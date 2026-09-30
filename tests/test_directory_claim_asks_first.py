"""A directory write claim over a live claim warns, and asks before it proceeds.

A declared directory sweeps up every path under it, so it can collide with a
peer's exact file claim without either caller intending it. The check here is
narrow on purpose: a directory claim that overlaps a live run warns, names the
overlapping claim, its owning run and the exact alternative the brief's files
give, and does not proceed; the same claim proceeds only when it carries
``--accept-directory-claim``, and the acceptance is recorded on the result. A
file path that overlaps nothing, and a directory that overlaps no live claim,
dispatch as before with no warning.

Every case drives the ``crew dispatch`` entry the operator uses, through its
``--dry-run`` mode, so the warning is the one a real dispatch would raise and no
worktree or process is created. The live claimant's launcher pid is this process,
so its claim is binding for the honest reason — its worker is alive — rather than
because a stub named an exited pid.

A declared directory need not exist on disk yet: a node writes into a topic
directory the live claim already covers, so an absent path with no file suffix
that sits inside a live claim is judged the same as one that is already there.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.crew.dispatch import _directory_claim_overlaps
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "directory-claim-fixture"
NODE_ID = "directory-claim-asks-first"
SESSION = "session-directory-claim"
CLAIMANT_RUN = "r-20260101T000000000000-holding-a-test-file"
CLAIMANT_NODE = "peer-holding-one-test-file"
CLAIMANT_PATH = "tests/test_x.py"
REAL_LIVE = Path.home() / ".config" / "reckon" / "crew" / "live"

CONFIG: dict[str, Any] = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": False,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)


def _plan_document() -> str:
    head = "<!doctype html><html><head>"
    metas = (
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="fixture">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-impl" content="0">'
        '<meta name="plan-version" content="0">'
    )
    body = '<body><h2 id="s5">A directory claim asks first</h2></body>'
    return f"{head}{metas}</head>{body}</html>"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A temporary crew home and a repository shaped like a reckon mount."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / "fixture.html").write_text(
        _plan_document(), encoding="utf-8"
    )
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("# a peer's test file\n", encoding="utf-8")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    _git(repo, "add", "seed.txt", "docs", "tests")
    _git(repo, "commit", "-q", "-m", "chore: seed")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo


@pytest.fixture(autouse=True)
def real_live_directory_is_not_a_fixture_target() -> Any:
    """No case may write this fixture's pointer into the real crew home."""

    def fixture_pointers() -> list[str]:
        found = []
        for path in REAL_LIVE.glob("*.json"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if PROJECT in text or CLAIMANT_RUN in text:
                found.append(path.name)
        return found

    assert fixture_pointers() == []
    yield
    assert fixture_pointers() == []


def _publish_claim(repo: Path, write_paths: list[str]) -> None:
    """A live pointer holding the peer's claim, its launcher pid alive."""
    _write_json(
        pointer_path(CLAIMANT_RUN),
        {
            "run_id": CLAIMANT_RUN,
            "project": PROJECT,
            "repo": str(repo),
            "pid": os.getpid(),
            "phase": "starting",
            "node": {"id": CLAIMANT_NODE, "write_paths": write_paths},
        },
    )


def _dry_run(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *write_paths: str,
    accept_directory_claim: bool = False,
):
    """One ``crew dispatch --dry-run`` issuing from the operator's hand."""
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *a, **k: CONFIG)
    arguments = [
        "crew",
        "dispatch",
        "--project",
        PROJECT,
        "--plan",
        "fixture",
        "--section",
        "s5",
        "--spec-level",
        "guided",
        "--node",
        NODE_ID,
        "--goal",
        "edit a region the brief names",
        "--done-when",
        "the brief's files are the scope: 0 unresolved paths",
        "--role",
        "implement",
        "--negative-control",
        "remove the overlap check; the no-flag case must fail on its warning assertion",
        "--session",
        SESSION,
        "--repo",
        str(repo),
        "--dry-run",
    ]
    for path in write_paths:
        arguments += ["--write-path", path]
    if accept_directory_claim:
        arguments.append("--accept-directory-claim")
    return CliRunner().invoke(cli_module.main, arguments)


def _warnings(payload: dict[str, Any]) -> list[str]:
    return [str(line) for line in payload.get("warnings") or ()]


def test_a_directory_claim_over_a_live_file_claim_warns_and_does_not_proceed(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect: a broad directory claim must not sweep over a peer silently."""
    _config_home, repo = home
    _publish_claim(repo, [CLAIMANT_PATH])

    result = _dry_run(repo, monkeypatch, "tests")
    payload = json.loads(result.output)

    assert result.exit_code == 2, result.output
    assert payload["ok"] is False
    warning = "\n".join(_warnings(payload))
    assert CLAIMANT_PATH in warning
    assert CLAIMANT_RUN in warning
    assert CLAIMANT_NODE in warning
    assert "--accept-directory-claim" in warning


def test_the_warning_lists_the_files_the_brief_names_as_the_alternative(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact alternative is the brief's own files, not the whole directory."""
    _config_home, repo = home
    _publish_claim(repo, [CLAIMANT_PATH])

    result = _dry_run(repo, monkeypatch, "tests", "tests/test_mine.py")
    payload = json.loads(result.output)

    assert result.exit_code == 2, result.output
    warning = "\n".join(_warnings(payload))
    assert "tests/test_mine.py" in warning


def test_the_flag_lets_the_directory_claim_proceed_and_is_recorded(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag the same claim proceeds, and the exception is written down."""
    _config_home, repo = home
    _publish_claim(repo, [CLAIMANT_PATH])

    result = _dry_run(repo, monkeypatch, "tests", accept_directory_claim=True)
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert payload["ok"] is True
    acceptances = payload.get("directory_claim_acceptances") or []
    assert [row["claimed_path"] for row in acceptances] == [CLAIMANT_PATH]
    assert any("--accept-directory-claim" in line for line in _warnings(payload))


def test_a_leaf_inside_a_peer_directory_claim_is_not_itself_a_directory_claim(
    tmp_path: Path,
) -> None:
    """A leaf inside a peer's directory claim keeps the plain refusal."""
    assert _directory_claim_overlaps(
        tmp_path / "tests", tmp_path / "tests" / "test_x.py"
    )
    # A sibling exact file is not a directory claim, and neither is a leaf under
    # a claim held as a directory: only the container side is a directory claim.
    assert not _directory_claim_overlaps(
        tmp_path / "tests" / "test_mine.py", tmp_path / "tests" / "test_x.py"
    )
    assert not _directory_claim_overlaps(
        tmp_path / "tests" / "test_x.py", tmp_path / "tests"
    )


def test_a_declared_file_overlapping_nothing_dispatches_without_a_warning(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exact file claim on a different file is unaffected."""
    _config_home, repo = home
    _publish_claim(repo, [CLAIMANT_PATH])

    result = _dry_run(repo, monkeypatch, "tests/test_mine.py")
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert payload["ok"] is True
    assert not any(
        "claims a directory overlapping" in line for line in _warnings(payload)
    )
    assert not payload.get("directory_claim_acceptances")


def test_a_directory_overlapping_no_live_claim_dispatches_without_a_warning(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory claim with no live claimant is unaffected."""
    _config_home, repo = home

    result = _dry_run(repo, monkeypatch, "tests")
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert payload["ok"] is True
    assert not any(
        "claims a directory overlapping" in line for line in _warnings(payload)
    )
    assert not payload.get("directory_claim_acceptances")


def test_an_absent_directory_inside_a_live_claim_warns_and_does_not_proceed(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A topic directory not yet on disk still sits inside the live claim."""
    _config_home, repo = home
    _publish_claim(repo, ["docs/evidence"])

    result = _dry_run(repo, monkeypatch, "docs/evidence/new-topic")
    payload = json.loads(result.output)
    warning = "\n".join(_warnings(payload))

    assert "docs/evidence" in warning
    assert "--accept-directory-claim" in warning
    assert result.exit_code == 2, result.output
    assert payload["ok"] is False
    assert not payload.get("directory_claim_acceptances")


def test_the_flag_lets_an_absent_directory_claim_proceed(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag the absent-directory claim proceeds, and is recorded."""
    _config_home, repo = home
    _publish_claim(repo, ["docs/evidence"])

    result = _dry_run(
        repo, monkeypatch, "docs/evidence/new-topic", accept_directory_claim=True
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert payload["ok"] is True
    acceptances = payload.get("directory_claim_acceptances") or []
    assert {row["claimed_path"] for row in acceptances} == {"docs/evidence"}
    assert "docs/evidence/new-topic" in {row["candidate_path"] for row in acceptances}


def test_an_absent_directory_claim_classifier_judges_a_suffix_as_a_file(
    tmp_path: Path,
) -> None:
    """The classifier reads an absent no-suffix path inside a claim as a tree.

    The path is not created, so the directory judgement is made from its shape
    alone: no suffix and a path-component prefix of the live claim. A path with
    a file suffix inside the same claim names a file, not a directory.
    """
    claim = tmp_path / "docs" / "evidence"
    assert _directory_claim_overlaps(claim / "new-topic", claim)
    assert not _directory_claim_overlaps(claim / "new-topic.html", claim)
    assert not _directory_claim_overlaps(claim / "sibling" / "other.py", claim)


def test_an_absent_dotted_directory_inside_a_live_claim_warns_and_does_not_proceed(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A topic directory whose name carries a dot is still a directory.

    A version-style topic name such as ``2026.09`` is declared by its tree and
    sits inside the live claim, so it must warn exactly as ``new-topic`` does;
    reading its ``.09`` as a file extension would let the broad claim proceed
    unseen, the silent collision this check exists to prevent.
    """
    _config_home, repo = home
    _publish_claim(repo, ["docs/evidence"])

    result = _dry_run(repo, monkeypatch, "docs/evidence/2026.09")
    payload = json.loads(result.output)
    warning = "\n".join(_warnings(payload))

    assert "docs/evidence" in warning
    assert "--accept-directory-claim" in warning
    assert result.exit_code == 2, result.output
    assert payload["ok"] is False
    assert not payload.get("directory_claim_acceptances")


def test_the_flag_lets_an_absent_dotted_directory_claim_proceed(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag the dotted absent-directory claim proceeds, and is recorded."""
    _config_home, repo = home
    _publish_claim(repo, ["docs/evidence"])

    result = _dry_run(
        repo, monkeypatch, "docs/evidence/2026.09", accept_directory_claim=True
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert payload["ok"] is True
    acceptances = payload.get("directory_claim_acceptances") or []
    assert {row["claimed_path"] for row in acceptances} == {"docs/evidence"}
    assert "docs/evidence/2026.09" in {row["candidate_path"] for row in acceptances}


def test_age_names_a_directory_and_a_file_extension_names_a_file(
    tmp_path: Path,
) -> None:
    """A dotted directory name and a leaf file name are told apart by their name.

    The suffix alone does not settle it: a numeric suffix such as ``.09`` is a
    directory name, while an alphabetic extension such as ``.html`` names a
    leaf file inside the peer's claim.
    """
    claim = tmp_path / "docs" / "evidence"
    assert _directory_claim_overlaps(claim / "2026.09", claim)
    assert _directory_claim_overlaps(claim / "2026.09.15", claim)
    assert not _directory_claim_overlaps(claim / "notes.html", claim)
