"""A run whose changed paths lie outside its repository promotes with no commit.

A report-only run — a review run, an investigate run — delivers its artefact
somewhere that is not a repository at all: a parsed review JSON under the crew
reviews directory, scratch files beside it. Its manifest lists those paths in
``changed_paths`` and its ``commits`` field is correctly empty, because there is
nothing inside the repository to commit. Promotion refused anyway, so such a run
could never be reconciled: it sat unreconciled forever and every later dispatch
in the project had to spend the unreconciled-runs waiver on it.

The guard's real question is narrower than "does changed_paths name anything":
it is whether any named path resolves under *this run's repository root* and so
needs the commit that contains it. This module fixes that question in place and
runs a fixture repository and pointer tree under tmp_path, asserting afterwards
that the real crew pointer directory is untouched, because an isolated read does
not prove an isolated write.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "report-only-fixture"
PLAN = "report-only-target"
RUN_IDS = (
    "r-20260917T120000000001-outside-only",
    "r-20260917T120000000002-inside-relative",
    "r-20260917T120000000003-inside-mixed",
    "r-20260917T120000000004-inside-absolute",
)


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_plan(root: Path) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Report-only target",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    """No fixture may reach the workstation's real crew pointer directory."""
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    real_pointers = [real_live / f"{run_id}.json" for run_id in RUN_IDS]
    assert not any(path.exists() for path in real_pointers)
    yield
    assert not any(path.exists() for path in real_pointers)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_hook = tmp_path / "config"
    config_hook.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_hook))
    root = tmp_path / "repo"
    _write_plan(root)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "candidate.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs", "candidate.txt"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (config_hook / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _manifest(
    tmp_path: Path,
    run_id: str,
    *,
    changed_paths: str,
    commits: str | None = None,
) -> Path:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    commit_line = "" if commits is None else f"commits: {commits}\n"
    manifest.write_text(
        "node: report-only\n"
        "status: complete\n"
        f"{commit_line}"
        f"changed_paths: {changed_paths}\n"
        "tests: focused report-only promotion check passed\n",
        encoding="utf-8",
    )
    return manifest


def _pointer(repository: Path, run_id: str, manifest: Path) -> None:
    head = _git(repository, "rev-parse", "HEAD")
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": head,
            "launch": "in-harness",
            "role": "review",
            "backend": "native",
            "created_at": "2026-09-17T12:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "report-only",
                "plan": PLAN,
                "section": "report-only",
                "time_budget": "25m",
                "write_paths": ["candidate.txt"],
            },
        },
    )


def test_manifest_naming_only_outside_paths_promotes(
    repository: Path, tmp_path: Path
) -> None:
    """(a) Outside-in-repository deliverable paths with no commit promote."""
    run_id = RUN_IDS[0]
    delivered = tmp_path / "elsewhere" / "review.json"
    scratch = tmp_path / "elsewhere" / "scratch.txt"
    manifest = _manifest(
        tmp_path,
        run_id,
        changed_paths=f"{delivered}, {scratch}",
    )
    _pointer(repository, run_id, manifest)

    promoted = crew.complete(run_id, gate="passed", root=repository)

    assert promoted["record"]["commits"] == []
    assert not pointer_path(run_id).exists()


def test_manifest_naming_an_in_repository_relative_path_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """(b) A relative path under the repository still requires its commit."""
    run_id = RUN_IDS[1]
    manifest = _manifest(tmp_path, run_id, changed_paths="candidate.txt")
    _pointer(repository, run_id, manifest)

    with pytest.raises(crew.CrewError, match="manifest field 'commits' is missing"):
        crew.complete(run_id, gate="passed", root=repository)

    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_manifest_naming_an_in_repository_path_beside_outside_paths_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """(c) One in-repository path among outside paths is still refused."""
    run_id = RUN_IDS[2]
    delivered = tmp_path / "elsewhere" / "review.json"
    manifest = _manifest(
        tmp_path,
        run_id,
        changed_paths=f"candidate.txt, {delivered}",
    )
    _pointer(repository, run_id, manifest)

    with pytest.raises(crew.CrewError, match="manifest field 'commits' is missing"):
        crew.complete(run_id, gate="passed", root=repository)

    assert ledger.runs(PROJECT, root=repository) == []


def test_manifest_naming_an_in_repository_path_written_absolutely_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """(d) An absolute path that resolves under the repository is inside."""
    run_id = RUN_IDS[3]
    inside = (repository / "candidate.txt").resolve()
    elsewhere = (tmp_path / "elsewhere" / "scratch.txt").resolve()
    manifest = _manifest(
        tmp_path,
        run_id,
        changed_paths=f"{inside}, {elsewhere}",
    )
    _pointer(repository, run_id, manifest)

    with pytest.raises(crew.CrewError, match="manifest field 'commits' is missing"):
        crew.complete(run_id, gate="passed", root=repository)

    assert ledger.runs(PROJECT, root=repository) == []
