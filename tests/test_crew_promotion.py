"""Promotion records narrative and measurements at their durable homes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from reckon import _plan_html, _store, crew, ledger
from reckon.cli import main as cli_main
from reckon.crew import promotion
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    # Promotion commits the two tracked stores it writes, so a fixture that
    # promotes must be a git worktree with a committed head to land into.
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _resolves_as_commit(repository: Path, revision: str) -> bool:
    result = subprocess.run(
        [
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{revision}^{{commit}}",
        ],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _repository_with_candidate(repository: Path) -> tuple[str, str]:
    candidate = repository / "candidate.txt"
    candidate.write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "candidate.txt"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(repository, *arguments)
    base = _git(repository, "rev-parse", "HEAD")
    candidate.write_text("seed\ndelivered\n", encoding="utf-8")
    _git(repository, "add", "candidate.txt")
    _git(repository, "commit", "-q", "-m", "test: record candidate")
    return base, _git(repository, "rev-parse", "HEAD")


def _write_commit_pointer(repository: Path, run_id: str, base: str) -> None:
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
            "created_at": "2026-09-04T12:00:00Z",
            "node": {
                "id": "commit-resolution",
                "plan": PLAN,
                "section": "commit-resolution",
                "time_budget": "25m",
                "write_paths": ["candidate.txt"],
            },
        },
    )


def _write_complete_manifest_pointer(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    *,
    base: str,
    changed_paths: str,
    commits: str | None,
) -> Path:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    commit_line = "" if commits is None else f"commits: {commits}\n"
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        f"{commit_line}"
        f"changed_paths: {changed_paths}\n"
        "tests: focused promotion check passed\n",
        encoding="utf-8",
    )
    _write_commit_pointer(repository, run_id, base)
    record = json.loads(pointer_path(run_id).read_text(encoding="utf-8"))
    record["manifest_path"] = str(manifest)
    _write_json(pointer_path(run_id), record)
    _stored_review(run_id, dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20))
    return manifest


def test_promotion_splits_narrative_from_run_measurements(repository: Path) -> None:
    run_id = "r-20260824T190800000000-node-a"
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-08-24T19:08:00Z",
            "manifest_path": "/durable/manifest.md",
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "35m",
                "write_paths": [],
            },
        },
    )

    narrative = "The shared write boundary now refuses an unclaimed closure."
    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome=narrative,
        tests_added=23,
        completed_at="2026-08-24T19:10:17Z",
        root=repository,
    )

    plan, _version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    comment = plan["comments"]["s2"][0]
    assert promoted["plan_comment"] == {
        "recorded": True,
        "comment_id": comment["id"],
        "section": "s2",
        "already_recorded": False,
    }
    assert narrative in comment["body"]
    assert "23" not in comment["body"]
    assert "137" not in comment["body"]

    run = ledger.load(PROJECT, repository)[0]["runs"][0]
    assert run["tests_added"] == 23
    assert run["wall_seconds"] == 137
    assert run["gate"] == "passed"
    assert run["outcome"] == ""
    assert narrative not in json.dumps(run)
    assert not (repository / "docs" / "evidence").exists()


# ── the attached review travels onto the committed ledger row ───────────────


def _stored_review(run_id: str, values: dict[str, int]) -> None:
    emitted = "\n".join(
        f"SCORE {dimension}: {values[dimension]}"
        for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
        }
    )
    review_module.store_review(record)


def _promote(repository: Path, run_id: str, *, outcome: str = "") -> dict:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-09-04T12:00:00Z",
            "manifest_path": "/durable/manifest.md",
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )
    return crew.complete(run_id, gate="passed", outcome=outcome, root=repository)


def test_a_reviewed_run_carries_its_dimensions_on_the_ledger_row(
    repository: Path,
) -> None:
    run_id = "r-20260904T120000000101-reviewed-carried"
    _stored_review(
        run_id,
        {
            "goal_fidelity": 18,
            "evidence": 15,
            "scope_discipline": 17,
            "durability": 19,
            "fit": 16,
        },
    )
    _promote(repository, run_id)

    row = ledger.load(PROJECT, repository)[0]["runs"][0]
    assert row["run_id"] == run_id
    assert row["review"] == {
        "status": "parsed",
        "scores": {
            "goal_fidelity": 18,
            "evidence": 15,
            "scope_discipline": 17,
            "durability": 19,
            "fit": 16,
        },
        "absent": [],
        "total": 85,
    }


def test_a_run_promoted_without_a_review_is_distinguishable_from_a_zero_scored_one(
    repository: Path,
) -> None:
    plain_run = "r-20260904T120000000102-unreviewed"
    zero_run = "r-20260904T120000000103-scored-zero"
    _promote(repository, plain_run)
    _stored_review(zero_run, dict.fromkeys(review_module.REVIEW_DIMENSIONS, 0))
    _promote(repository, zero_run)

    rows = ledger.load(PROJECT, repository)[0]["runs"]
    plain = next(row for row in rows if row["run_id"] == plain_run)
    zero = next(row for row in rows if row["run_id"] == zero_run)
    # Distinguishable in both directions: an unreviewed row is not one whose
    # review measured zero, and a zero-scored review is not an absent one.
    assert "review" in plain
    assert plain["review"] is None
    assert zero["review"] is not None
    assert zero["review"] == {
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 0),
        "absent": [],
        "total": 0,
    }
    assert plain["review"] != zero["review"]


def test_terminal_write_requires_a_back_linking_evidence_record(
    repository: Path,
) -> None:
    plan, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    with pytest.raises(_store.OpError) as excinfo:
        _store.write_plan(
            PROJECT,
            PLAN,
            {**plan, "status": "done"},
            version,
            repository,
            artifact_type="plan",
        )

    detail = str(excinfo.value)
    assert f"docs/evidence/archive/{PLAN}-landed.html" in detail
    assert f'plan-evidence-for" content="{PLAN}' in detail
    with pytest.raises(_store.OpError, match=f"{PLAN}-landed.html"):
        _store.validate_landing_patch({**plan, "status": "done"}, {"status": "done"})
    refused, refused_version = _store.read_plan(
        PROJECT, PLAN, repository, artifact_type="plan"
    )
    assert refused["status"] == "active"
    assert refused_version == version

    evidence_path = repository / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"
    _write_resource(
        evidence_path,
        {
            "type": "evidence",
            "slug": f"{PLAN}-landed",
            "title": "Plan A execution evidence",
            "evidence_for": [PLAN],
            "version": 0,
        },
    )
    assert ledger.evidence_records_for_plan(PROJECT, PLAN, repository) == [
        evidence_path
    ]

    new_version = _store.write_plan(
        PROJECT,
        PLAN,
        {**refused, "status": "done"},
        refused_version,
        repository,
        artifact_type="plan",
    )
    terminal, stored_version = _store.read_plan(
        PROJECT, PLAN, repository, artifact_type="plan"
    )
    assert new_version == stored_version == refused_version + 1
    assert terminal["status"] == "done"


def test_a_commit_from_another_repository_names_that_repository(
    tmp_path, monkeypatch
) -> None:
    """A sha that resolves elsewhere is a routing mistake, not a bad sha.

    A node dispatched without ``--repo`` has its run repository set to the
    dispatching one, so a commit it made in a foreign checkout cannot resolve
    and the refusal is correct. Stating only that it does not resolve leaves the
    reader to guess; naming the repository it does belong to makes the refusal
    the instruction for the next dispatch.
    """
    from reckon.crew import promotion

    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    def _repo(name: str) -> Path:
        root = tmp_path / name
        (root / "docs").mkdir(parents=True)
        (root / "seed.txt").write_text(f"{name}\n")
        for arguments in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "worker@example.invalid"],
            ["config", "user.name", "Worker"],
            ["add", "seed.txt"],
            ["commit", "-q", "-m", "chore: seed"],
        ):
            subprocess.run(
                ["git", *arguments], cwd=root, check=True, capture_output=True
            )
        return root

    run_repo = _repo("dispatching")
    foreign = _repo("written-to")
    (config_home / "mounts.json").write_text(
        json.dumps(
            {
                "dispatching": str(run_repo / "docs"),
                "written-to": str(foreign / "docs"),
            }
        )
    )
    stray = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=foreign,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with pytest.raises(crew.CrewError) as refusal:
        promotion._resolve_commits(cwd=run_repo, revisions=[stray], run_id="r-x")

    message = str(refusal.value)
    assert str(run_repo) in message, "the repository it was checked against"
    assert str(foreign) in message, "the repository it belongs to"
    assert f"--repo {foreign}" in message, "the remedy for the next dispatch"


def test_an_unresolvable_commit_says_what_else_to_check(tmp_path, monkeypatch) -> None:
    """With no other repository holding it, the likely cause is a worker that
    staged without committing — so the refusal says that instead of guessing."""
    from reckon.crew import promotion

    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "solo"
    root.mkdir()
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["commit", "-q", "--allow-empty", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)

    with pytest.raises(crew.CrewError) as refusal:
        promotion._resolve_commits(cwd=root, revisions=["0" * 40], run_id="r-y")

    assert "committed rather than only staging" in str(refusal.value)


def test_full_width_non_object_is_refused_before_a_passing_gate_is_recorded(
    repository: Path,
) -> None:
    base, _candidate = _repository_with_candidate(repository)
    run_id = "r-full-width-non-object"
    _write_commit_pointer(repository, run_id, base)
    missing = "0" * 40
    assert not any(
        _resolves_as_commit(repository, missing[:width])
        for width in range(4, len(missing) + 1)
    )

    with pytest.raises(crew.CrewError, match=missing):
        crew.complete(
            run_id,
            gate="passed",
            commits=[missing],
            root=repository,
        )

    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()


def test_log_abbreviation_is_resolved_to_its_full_commit(repository: Path) -> None:
    base, commit = _repository_with_candidate(repository)
    run_id = "r-log-abbreviation"
    _write_commit_pointer(repository, run_id, base)
    abbreviation = _git(repository, "log", "-1", "--oneline").split(maxsplit=1)[0]
    assert 0 < len(abbreviation) < len(commit)

    stored = crew.complete(
        run_id,
        gate="passed",
        commits=[abbreviation],
        root=repository,
    )["record"]

    assert stored["commits"] == [commit]


def test_unresolvable_abbreviation_names_the_repository_it_consulted(
    repository: Path,
) -> None:
    base, _candidate = _repository_with_candidate(repository)
    run_id = "r-unresolvable-abbreviation"
    _write_commit_pointer(repository, run_id, base)
    missing = "0" * 12
    assert not _resolves_as_commit(repository, missing)

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(
            run_id,
            gate="passed",
            commits=[missing],
            root=repository,
        )

    message = str(refusal.value)
    assert missing in message
    assert f"run repository ({repository})" in message
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()


def test_full_commit_promotes_without_rewriting_its_identity(
    repository: Path,
) -> None:
    base, commit = _repository_with_candidate(repository)
    run_id = "r-full-commit"
    _write_commit_pointer(repository, run_id, base)

    stored = crew.complete(
        run_id,
        gate="passed",
        commits=[commit],
        root=repository,
    )["record"]

    assert stored["commits"] == [commit]
    assert not pointer_path(run_id).exists()


def _invoke_complete_cli(run_id: str, gate_log: Path, commit: str) -> Result:
    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "passed",
            "--commit",
            commit,
            "--gate-command",
            "probe check",
            "--gate-exit-status",
            "0",
            "--gate-log-path",
            str(gate_log),
        ],
    )


def test_a_successful_store_write_is_reported_written_on_the_command_payload(
    repository: Path, tmp_path: Path
) -> None:
    base, commit = _repository_with_candidate(repository)
    run_id = "r-store-written-payload"
    _write_commit_pointer(repository, run_id, base)
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("probe check passed\n", encoding="utf-8")

    invoked = _invoke_complete_cli(run_id, gate_log, commit)

    assert invoked.exit_code == 0
    payload = json.loads(invoked.output)
    assert payload["ok"] is True
    # Index health is reported on the payload without changing the run record.
    assert payload["store"] == {"status": "written"}
    assert "store_write" not in payload["record"]
    assert payload["record"]["run_id"] == run_id
    run_file = ledger.run_path(PROJECT, run_id, repository)
    assert run_file.read_text() == ledger.serialize_run(payload["record"])
    assert ledger.index_lag(PROJECT, repository) == 0


def test_a_failing_store_write_is_reported_on_the_ordinary_success_payload(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reckon import run_store

    base, commit = _repository_with_candidate(repository)
    run_id = "r-store-fail-payload"
    _write_commit_pointer(repository, run_id, base)
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("probe check passed\n", encoding="utf-8")
    with run_store.RunStore():
        pass
    assert ledger.index_lag(PROJECT, repository) == 0
    attempts = []

    def broken_append(_project: str, _record_entry: dict) -> None:
        path = ledger.run_path(PROJECT, run_id, repository)
        assert json.loads(path.read_text()) == _record_entry
        attempts.append(_record_entry["run_id"])
        raise RuntimeError("injected store failure")

    monkeypatch.setattr(run_store, "append", broken_append)

    invoked = _invoke_complete_cli(run_id, gate_log, commit)

    # The committed row survives an index failure, which leaves observable lag.
    assert invoked.exit_code == 0
    payload = json.loads(invoked.output)
    assert payload["ok"] is True
    assert payload["store"]["status"] == "failed"
    assert "RuntimeError" in payload["store"]["error"]
    assert "store_write" not in payload["record"]
    assert payload["record"]["run_id"] == run_id
    assert attempts == [run_id]
    run_file = ledger.run_path(PROJECT, run_id, repository)
    assert run_file.read_text() == ledger.serialize_run(payload["record"])
    assert ledger.index_lag(PROJECT, repository) == 1
    assert ledger.index_lag(PROJECT, repository) == 1
    assert [row["run_id"] for row in ledger.runs(PROJECT, repository)] == [run_id]
    assert ledger.index_lag(PROJECT, repository) == 1
    run_store.import_ledger(PROJECT, root=repository)
    assert ledger.index_lag(PROJECT, repository) == 0


@pytest.mark.parametrize("commits", [None, "none"])
def test_complete_manifest_with_changed_paths_requires_commits_field(
    repository: Path, tmp_path: Path, commits: str | None
) -> None:
    _base, head = _repository_with_candidate(repository)
    run_id = f"r-changed-manifest-{commits or 'absent'}"
    _write_complete_manifest_pointer(
        repository,
        tmp_path,
        run_id,
        base=head,
        changed_paths="candidate.txt",
        commits=commits,
    )

    with pytest.raises(crew.CrewError, match="manifest field 'commits' is missing"):
        crew.complete(
            run_id,
            gate="passed",
            no_commit="the coordinator supplied a rationale",
            root=repository,
        )

    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()


def test_complete_manifest_with_changed_paths_and_commit_promotes(
    repository: Path, tmp_path: Path
) -> None:
    _base, commit = _repository_with_candidate(repository)
    run_id = "r-changed-manifest-with-commit"
    _write_complete_manifest_pointer(
        repository,
        tmp_path,
        run_id,
        base=commit,
        changed_paths="candidate.txt",
        commits=commit,
    )

    promoted = crew.complete(
        run_id,
        gate="passed",
        commits=[commit],
        root=repository,
    )

    assert promoted["record"]["commits"] == [commit]
    assert not pointer_path(run_id).exists()


def test_complete_report_only_manifest_without_commit_promotes(
    repository: Path, tmp_path: Path
) -> None:
    _base, head = _repository_with_candidate(repository)
    run_id = "r-report-only-manifest"
    _write_complete_manifest_pointer(
        repository,
        tmp_path,
        run_id,
        base=head,
        changed_paths="none",
        commits=None,
    )

    promoted = crew.complete(
        run_id,
        gate="passed",
        root=repository,
    )

    assert promoted["record"]["commits"] == []
    assert not pointer_path(run_id).exists()


PROSE_NO_CHANGED_PATHS_VALUES = (
    "none under the repository; the sole deliverable is the report",
    "none (the node changed no file under the repository)",
)


@pytest.mark.parametrize("changed_paths", PROSE_NO_CHANGED_PATHS_VALUES)
def test_complete_manifest_with_prose_none_in_changed_paths_promotes(
    repository: Path, tmp_path: Path, changed_paths: str
) -> None:
    _base, head = _repository_with_candidate(repository)
    run_id = f"r-report-only-manifest-prose-{len(changed_paths)}"
    _write_complete_manifest_pointer(
        repository,
        tmp_path,
        run_id,
        base=head,
        changed_paths=changed_paths,
        commits=None,
    )

    promoted = crew.complete(
        run_id,
        gate="passed",
        root=repository,
    )

    assert promoted["record"]["commits"] == []
    assert not pointer_path(run_id).exists()


# ── A promotion that would delete a resume path ─────────────────────────────
#
# Promotion removes the pointer, and the pointer is where a resume finds its
# session. The measured loss: a wave refused per-request on a spend limit, the
# blocked runs promoted, and sixty-four turns of worker orientation discarded
# while promotion reported success. The refusal below is the one this operation
# was missing; the waiver is what keeps a deliberate discard possible and
# afterwards legible.

STREAM_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "backends" / "claude-turn.jsonl"
)


def _blocked_pointer(
    repository: Path,
    run_id: str,
    *,
    manifest: Path,
    session_id: str = "",
    stream: Path | None = None,
    status: str = "blocked",
    worktree: Path | None = None,
) -> None:
    """A pointer and manifest in the shape dispatch leaves behind."""
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: node-a\nstatus: {status}\ncommits: none\n"
        "blockers: the provider refused the turn on a spend limit\n",
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "launch": "cli" if stream else "in-harness",
        "role": "implement",
        "member": "worker-a",
        "backend": "beta",
        "created_at": "2026-09-03T10:00:00Z",
        "manifest_path": str(manifest),
        "node": {
            "id": "node-a",
            "plan": PLAN,
            "section": "§2",
            "time_budget": "35m",
            "write_paths": [],
        },
    }
    if session_id:
        record["session_id"] = session_id
    if stream:
        # What dispatch writes for a CLI backend: the argv it launched and the
        # stream that turn produced, which is where a resume re-reads a session
        # id the pointer never carried.
        stream.parent.mkdir(parents=True, exist_ok=True)
        stream.write_bytes(STREAM_FIXTURE.read_bytes())
        record["argv"] = ["claude", "-p"]
        record["log_path"] = str(stream)
    if worktree is not None:
        record["worktree"] = str(worktree)
    _write_json(pointer_path(run_id), record)


def _stream_session_id() -> str:
    """The session the fixture stream carries, read the way promotion reads it."""
    events = [
        json.loads(line)
        for line in STREAM_FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    found = next(
        (str(event["session_id"]) for event in events if event.get("session_id")), ""
    )
    assert found
    return found


def _real_crew_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where this workstation's pointers actually live, with the patch lifted."""
    with monkeypatch.context() as fresh:
        fresh.delenv("RECKON_HOME", raising=False)
        return crew.crew_home()


def _linked_worktree(
    repository: Path,
    tmp_path: Path,
    name: str,
    *,
    divergent: bool = False,
) -> Path:
    """Create the clean linked tree that promotion is responsible for.

    The repository fixture already seeds and commits the git repo, so a
    linked tree is created straight from HEAD.
    """
    worktree = tmp_path / "worktrees" / name
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    if divergent:
        marker = worktree / "candidate.txt"
        marker.write_text("committed only in the worker tree\n")
        for arguments in (
            ("add", "candidate.txt"),
            ("commit", "-q", "-m", "test: record worker result"),
        ):
            subprocess.run(
                ["git", *arguments],
                cwd=worktree,
                check=True,
                capture_output=True,
            )
    return worktree


def test_promoting_a_blocked_run_with_a_live_session_is_refused(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal has to be actionable: the remedy and the session it found."""
    run_id = "r-20260903T100000000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        session_id="sess-live-1",
    )

    with pytest.raises(crew.CrewError) as excinfo:
        crew.complete(
            run_id,
            gate="not-run",
            outcome="the provider refused the turn",
            root=repository,
        )

    message = str(excinfo.value)
    assert "reckon crew resume" in message
    assert run_id in message
    assert "sess-live-1" in message
    # Refused means nothing happened: the pointer a resume needs is still there
    # and the ledger has no row for the run.
    assert pointer_path(run_id).is_file()
    data, _version = ledger.load(PROJECT, root=repository)
    assert [item for item in data["runs"] if item["run_id"] == run_id] == []
    assert not (_real_crew_home(monkeypatch) / "live" / f"{run_id}.json").exists()


def test_a_session_only_in_the_stream_also_refuses(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A null session id on the pointer is not evidence of an unresumable run.

    Resume already recovers this case by re-reading the stream, so a promotion
    that consults only the pointer discards a session that was there all along
    — which is the misreading the loss turned on.
    """
    run_id = "r-20260903T101000000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        stream=tmp_path / "runs" / run_id / "stream.jsonl",
    )
    assert not (json.loads(pointer_path(run_id).read_text()).get("session_id"))

    with pytest.raises(crew.CrewError) as excinfo:
        crew.complete(
            run_id,
            gate="not-run",
            outcome="the provider refused the turn",
            root=repository,
        )

    message = str(excinfo.value)
    assert _stream_session_id() in message
    assert "reckon crew resume" in message
    assert pointer_path(run_id).is_file()


def test_a_stated_waiver_promotes_and_lands_on_the_ledger_row(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deliberate discard stays possible, and stays legible afterwards."""
    run_id = "r-20260903T102000000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        session_id="sess-live-2",
    )
    reason = "the node was re-scoped and its orientation no longer applies"

    promoted = crew.complete(
        run_id,
        gate="not-run",
        outcome="the provider refused the turn",
        root=repository,
        resume_waiver=reason,
    )

    waiver = promoted["record"]["resume_waiver"]
    assert waiver["reason"] == reason
    assert waiver["session_id"] == "sess-live-2"
    assert waiver["source"] == "pointer"
    data, _version = ledger.load(PROJECT, root=repository)
    row = next(item for item in data["runs"] if item["run_id"] == run_id)
    assert row["resume_waiver"] == waiver
    assert promoted["pointer_removed"] is True
    assert not (_real_crew_home(monkeypatch) / "live" / f"{run_id}.json").exists()


def test_complete_command_accepts_a_reasoned_resume_path_waiver(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public command reaches the promotion guard and its recorded waiver."""
    run_id = "r-20260903T102500000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        stream=tmp_path / "runs" / run_id / "stream.jsonl",
    )
    real_pointer = _real_crew_home(monkeypatch) / "live" / f"{run_id}.json"
    assert not real_pointer.exists()
    arguments = [
        "crew",
        "complete",
        "--run",
        run_id,
        "--gate",
        "not-run",
        "--outcome",
        "the provider refused the turn",
    ]

    refused = CliRunner().invoke(cli_main, arguments)

    assert refused.exit_code == 1
    assert "--waive-resume-path REASON" in refused.output
    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []

    reason = "the replacement run has already recovered the useful context"
    accepted = CliRunner().invoke(
        cli_main,
        [*arguments, "--waive-resume-path", reason],
    )

    assert accepted.exit_code == 0, accepted.output
    waiver = json.loads(accepted.output)["record"]["resume_waiver"]
    assert waiver == {
        "session_id": _stream_session_id(),
        "source": "stream",
        "reason": reason,
    }
    row = next(
        item
        for item in ledger.runs(PROJECT, root=repository)
        if item["run_id"] == run_id
    )
    assert row["resume_waiver"] == waiver
    assert not pointer_path(run_id).exists()
    assert not real_pointer.exists()


def test_complete_command_refuses_a_resume_path_waiver_without_a_reason(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260903T102600000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        session_id="sess-live-command",
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "not-run",
            "--outcome",
            "the provider refused the turn",
            "--waive-resume-path",
            "  ",
        ],
    )

    assert result.exit_code == 1
    assert "--waive-resume-path REASON" in result.output
    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_complete_command_promotes_a_blocked_run_without_a_recoverable_session(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260903T102700000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "not-run",
            "--outcome",
            "the provider refused the turn",
        ],
    )

    assert result.exit_code == 0, result.output
    # The record always declares the waiver key for schema stability; a run
    # that waived nothing carries None rather than an absent key.
    assert json.loads(result.output)["record"]["resume_waiver"] is None
    assert not pointer_path(run_id).exists()


def test_complete_help_describes_the_recoverable_session_refusal() -> None:
    result = CliRunner().invoke(cli_main, ["crew", "complete", "--help"])

    assert result.exit_code == 0
    assert "--waive-resume-path REASON" in result.output
    assert "promotion refused because" in result.output
    assert "session is still recoverable" in result.output


def test_a_blocked_run_with_no_recoverable_session_promotes_unchanged(
    repository: Path, tmp_path: Path
) -> None:
    """The existing path has to stay reachable, or the guard is a wall."""
    run_id = "r-20260903T103000000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
    )

    promoted = crew.complete(
        run_id,
        gate="not-run",
        outcome="the provider refused the turn",
        root=repository,
    )

    assert promoted["pointer_removed"] is True
    assert promoted["record"]["resume_waiver"] is None


def test_a_passing_gate_promotes_whatever_its_session(
    repository: Path, tmp_path: Path
) -> None:
    """Delivered work is not a resume candidate, however live its session."""
    run_id = "r-20260903T104000000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        session_id="sess-live-3",
    )

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="the node landed",
        root=repository,
    )

    assert promoted["pointer_removed"] is True
    assert promoted["record"]["resume_waiver"] is None
    assert promoted["record"]["session_id"] == "sess-live-3"


def test_a_run_terminal_for_another_reason_is_unaffected(
    repository: Path, tmp_path: Path
) -> None:
    """Only the blocked classification is guarded; a delivery is not."""
    run_id = "r-20260903T105000000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        session_id="sess-live-4",
        status="complete",
    )

    promoted = crew.complete(
        run_id,
        gate="not-run",
        outcome="delivered, gate not run in this tier",
        root=repository,
    )

    assert promoted["pointer_removed"] is True
    assert promoted["record"]["resume_waiver"] is None


@pytest.mark.parametrize(
    ("divergent", "artifact_classification"),
    [(False, "integrated"), (True, "unintegrated")],
)
def test_a_complete_session_releases_integrated_work_and_audits_unintegrated_work(
    repository: Path,
    tmp_path: Path,
    divergent: bool,
    artifact_classification: str,
) -> None:
    """A completed session closes integrated work but keeps an unintegrated commit."""
    suffix = "divergent" if divergent else "reachable"
    run_id = f"r-20260903T110000000000-{suffix}"
    worktree = _linked_worktree(
        repository,
        tmp_path,
        suffix,
        divergent=divergent,
    )
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        session_id=f"session-{suffix}",
        status="complete",
        worktree=worktree,
    )
    _stored_review(run_id, dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20))

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="the node delivered its result",
        root=repository,
    )

    if divergent:
        assert worktree.is_dir()
        assert promoted["release"]["worktree_released"] is False
        assert promoted["record"].get("worktree_retention") is None
        audit = promoted["release"]["worktree_audit"]
        row = next(item for item in audit["worktrees"] if item["path"] == str(worktree))
        assert row["classification"] == artifact_classification
        assert row["reclaimable"] is False
        assert audit["counts"][artifact_classification] == 1
    else:
        assert not worktree.exists()
        assert promoted["release"]["worktree_released"] is True
        assert promoted["record"].get("worktree_retention") is None
    stored = next(
        item
        for item in ledger.runs(PROJECT, root=repository)
        if item["run_id"] == run_id
    )
    assert stored.get("worktree_retention") is None


def test_an_incomplete_session_retains_its_worktree_for_resume(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260903T110100000000-incomplete"
    worktree = _linked_worktree(repository, tmp_path, "incomplete")
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        session_id="session-incomplete",
        status="blocked",
        worktree=worktree,
    )

    promoted = crew.complete(
        run_id,
        gate="not-run",
        outcome="the incomplete run remains resumable",
        resume_waiver="retain the session worktree for the next resume",
        root=repository,
    )

    assert worktree.is_dir()
    assert promoted["release"]["worktree_released"] is False
    retention = promoted["record"]["worktree_retention"]
    assert retention["classification"] == "retained-for-resume"
    assert retention["session_id"] == "session-incomplete"
    assert promoted["release"]["worktree_audit"]["counts"]["retained-for-resume"] == 1
    assert ledger.runs(PROJECT, root=repository)[0]["worktree_retention"] == retention


def test_promotion_audits_only_its_own_worktree(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_run = subprocess.run
    git_invocations: list[tuple[str, ...]] = []

    def counting_run(command, *args, **kwargs):
        if command and command[0] == "git":
            git_invocations.append(tuple(str(part) for part in command))
        return real_run(command, *args, **kwargs)

    def promote(target: str, tree: Path) -> dict:
        _stored_review(target, dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20))
        _blocked_pointer(
            repository,
            target,
            manifest=tmp_path / "manifests" / f"{target}.md",
            session_id="session-promoted",
            status="blocked",
            worktree=tree,
        )
        git_invocations.clear()
        return crew.complete(
            target,
            gate="passed",
            outcome="the node delivered its result",
            root=repository,
        )

    # The landing path makes whatever fixed git calls it needs, so the count is
    # not a literal. What this measures is that the count does not move with the
    # peer population: promotion audits its own worktree, never the ones beside
    # it. Whether the checkout ever tracked the ledger is answered through git
    # the first time the ledger is absent, so the first landing carries probes
    # no later one repeats — take that landing before measuring, so the
    # comparison below is between two warm landings.
    promote(
        "r-20260903T110200000000-warmup",
        _linked_worktree(repository, tmp_path, "warmup"),
    )
    lone_worktree = _linked_worktree(repository, tmp_path, "lone")
    monkeypatch.setattr(subprocess, "run", counting_run)
    promote("r-20260903T110400000000-lone", lone_worktree)
    calls_alone = len(git_invocations)

    for index in range(20):
        peer = tmp_path / "worktrees" / f"peer-{index}"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(peer), "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
        )
    worktree = _linked_worktree(repository, tmp_path, "promoted")
    registered = [
        line
        for line in _git(repository, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]
    assert len(registered) >= 22

    run_id = "r-20260903T110500000000-promoted"
    promoted = promote(run_id, worktree)

    assert len(git_invocations) == calls_alone
    assert promoted["release"]["worktree_released"] is False
    rows = promoted["release"]["worktree_audit"]["worktrees"]
    assert [row["path"] for row in rows] == [str(worktree.resolve())]
    assert rows[0]["classification"] == "retained-for-resume"
    assert rows[0]["artifact_classification"] == "integrated"


def test_an_unrecoverable_session_releases_its_worktree(
    repository: Path,
    tmp_path: Path,
) -> None:
    run_id = "r-20260903T111000000000-unrecoverable"
    worktree = _linked_worktree(repository, tmp_path, "unrecoverable")
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        worktree=worktree,
    )

    promoted = crew.complete(
        run_id,
        gate="not-run",
        outcome="the session could not be recovered",
        root=repository,
    )

    assert promoted["release"]["worktree_released"] is True
    assert not worktree.exists()
    assert promoted["record"]["worktree_retention"] is None


def test_a_resume_waiver_retains_the_worktree_by_default(
    repository: Path,
    tmp_path: Path,
) -> None:
    run_id = "r-20260903T112000000000-waived"
    worktree = _linked_worktree(repository, tmp_path, "waived")
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        session_id="session-waived",
        worktree=worktree,
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "not-run",
            "--outcome",
            "the provider refused the turn",
            "--waive-resume-path",
            "the result must be recorded before recovery continues",
        ],
    )
    promoted = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert worktree.is_dir()
    assert promoted["release"]["worktree_released"] is False
    assert promoted["record"]["worktree_retention"]["session_id"] == "session-waived"
    assert "worktree_discarded" not in promoted["record"]["resume_waiver"]


def test_a_resume_waiver_can_explicitly_discard_the_worktree(
    repository: Path,
    tmp_path: Path,
) -> None:
    run_id = "r-20260903T113000000000-discarded"
    worktree = _linked_worktree(repository, tmp_path, "discarded")
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        session_id="session-discarded",
        worktree=worktree,
    )

    promoted = crew.complete(
        run_id,
        gate="not-run",
        outcome="the provider refused the turn",
        resume_waiver="the replacement run has recovered the useful context",
        discard_resume_worktree=True,
        root=repository,
    )

    assert promoted["release"]["worktree_released"] is True
    assert not worktree.exists()
    assert promoted["record"]["worktree_retention"] is None
    assert promoted["record"]["resume_waiver"]["worktree_discarded"] is True


# ── A promoted run's own stream fixes its figures on the ledger row ───────────
#
# A terminal run's stream is immutable, so its two figures are computed once at
# promotion and recorded on the row; a later derive reads the ledger and never
# reopens the stream. Asserted end to end by promoting a run and reading the
# committed row back, not by unit-testing the extractor.


def test_promotion_records_stream_figures_on_the_committed_row(
    repository: Path, tmp_path: Path
) -> None:
    _base, head = _repository_with_candidate(repository)
    run_id = "r-20260909T155900000000-figured"
    run_directory = tmp_path / "config" / "crew" / "runs" / run_id
    run_directory.mkdir(parents=True)
    stream = run_directory / "stream.jsonl"
    events = [
        {
            "type": "assistant",
            "session_id": "figures-session",
            "message": {
                "id": "probe",
                "content": [{"type": "text", "text": "reading"}],
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 68,
                },
            },
        },
        {
            "type": "assistant",
            "session_id": "figures-session",
            "message": {
                "id": "first-write",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "input": {"file_path": "result.txt"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {"file_path": "result.txt"},
                    },
                ],
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 98,
                },
            },
        },
        {"type": "result", "result": "ok"},
    ]
    stream.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\nstatus: complete\nchanged_paths: none\n"
        "tests: focused promotion check passed\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": head,
            "launch": "cli",
            "role": "implement",
            "member": "worker-a",
            "backend": "beta",
            "created_at": "2026-09-09T11:00:00Z",
            "manifest_path": str(manifest),
            "log_path": str(stream),
            "argv": ["claude", "-p"],
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "25m",
                "write_paths": ["result.txt"],
            },
        },
    )

    _stored_review(run_id, dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20))
    promoted = crew.complete(run_id, gate="passed", root=repository)

    data, _version = ledger.load(PROJECT, root=repository)
    row = next(item for item in data["runs"] if item["run_id"] == run_id)
    # The stream fixes both figures: one charged probe turn and one write turn
    # carrying two tool-use blocks. The committed row carries them as numbers,
    # so nothing downstream needs to reopen the stream.
    assert promoted["record"]["tool_steps"] == 2.0
    assert promoted["record"]["orientation_input_tokens"] == 220.0
    assert row["tool_steps"] == 2.0
    assert row["orientation_input_tokens"] == 220.0


# ── a promotion commits the two stores it writes ─────────────────────────────
#
# A landing appends the run to the project ledger and records its plan
# comment, then commits both in one landing whose subject names the promoted
# run and its gate verdict. Exactly those two paths are staged, never a
# whole-tree add, and a checkout that cannot host the commit refuses before
# either store is written.


def _porcelain(repository: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _landing_commit_paths(repository: Path, revision: str = "HEAD") -> list[str]:
    return _git(repository, "show", "--format=", "--name-only", revision).split()


def _promotion_commit(repository: Path, run_id: str) -> str:
    """Locate the commit a promotion made for one run.

    A promotion records its release outcome in a commit on top of the landing,
    so the landing is no longer what HEAD names. It is found instead by the
    subject promotion gives it, which names the run and its gate verdict.
    """
    prefix = f"promote({run_id}):"
    matches = [
        line.split(" ", 1)[0]
        for line in _git(repository, "log", "--format=%H %s").splitlines()
        if line.split(" ", 1)[1].startswith(prefix)
    ]
    assert len(matches) == 1, matches
    return matches[0]


def test_a_successful_promotion_leaves_its_two_stores_committed_and_clean(
    repository: Path,
) -> None:
    run_id = "r-20260914T190321183448-landing"
    _promote(repository, run_id, outcome="the landing leaves no dirty state")

    ledger_file = ledger.run_path(PROJECT, run_id, repository)
    plan_file = repository / "docs" / "plans" / f"{PLAN}.html"
    assert ledger_file.is_file()
    assert run_id in plan_file.read_text(encoding="utf-8")

    # No uncommitted change remains at either path promotion wrote.
    porcelain = _porcelain(repository)
    assert not any(
        str(ledger_file.relative_to(repository)) in line
        or f"docs/plans/{PLAN}.html" in line
        for line in porcelain
    )
    assert not ledger.ledger_path(PROJECT, repository).exists()
    assert (
        _git(repository, "show", f"HEAD:{ledger_file.relative_to(repository)}")
        == ledger_file.read_text().strip()
    )


def test_the_landing_commit_names_the_run_id_and_the_gate_verdict(
    repository: Path,
) -> None:
    run_id = "r-20260914T190322000000-verdict"
    _promote(repository, run_id, outcome="the landing carries both stores")

    promote_commit = _promotion_commit(repository, run_id)
    subject = _git(repository, "log", "-1", "--format=%s", promote_commit)
    assert run_id in subject
    assert "passed" in subject
    assert _git(repository, "log", "-1", "--format=%b", promote_commit).strip()
    assert set(_landing_commit_paths(repository, promote_commit)) == {
        f"docs/state/{PROJECT}/runs/{run_id}.json",
        f"docs/plans/{PLAN}.html",
    }


def test_a_landing_commit_never_stages_an_unrelated_dirty_file(
    repository: Path,
) -> None:
    untracked = repository / "loose.txt"
    untracked.write_text("uncommitted\n", encoding="utf-8")
    tracked = repository / "seed.txt"
    tracked.write_text("seed\nmodified\n", encoding="utf-8")

    run_id = "r-20260914T190323000000-scoped"
    _promote(repository, run_id, outcome="the landing is scoped to its own paths")

    porcelain = _porcelain(repository)
    assert any(line.endswith("loose.txt") for line in porcelain)
    assert any(
        line.startswith(" M ") and line.endswith("seed.txt") for line in porcelain
    )
    committed = _landing_commit_paths(repository, _promotion_commit(repository, run_id))
    assert "loose.txt" not in committed
    assert "seed.txt" not in committed
    assert set(committed) == {
        f"docs/state/{PROJECT}/runs/{run_id}.json",
        f"docs/plans/{PLAN}.html",
    }


def test_a_checkout_that_cannot_commit_refuses_before_any_store_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "nongit"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    plan_file = root / "docs" / "plans" / f"{PLAN}.html"
    _write_resource(
        plan_file,
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    before = plan_file.read_text(encoding="utf-8")

    run_id = "r-20260914T190324000000-refused"
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(root),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-09-14T19:03:00Z",
            "manifest_path": "/durable/manifest.md",
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )

    with pytest.raises(crew.CrewError, match="not a git worktree"):
        crew.complete(
            run_id,
            gate="passed",
            outcome="cannot land into a checkout that cannot commit",
            root=root,
        )

    # Neither store was written: no ledger row and an untouched plan file.
    assert not (root / "docs" / "state" / PROJECT / "crew.json").exists()
    assert not ledger.run_path(PROJECT, run_id, root).exists()
    assert plan_file.read_text(encoding="utf-8") == before


def test_a_commit_failure_restores_both_stores_and_refuses(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-20260914T190325000000-commit-fails"
    _promote_failing(repository, run_id, monkeypatch)

    # The refusal left neither store: the ledger row was committed to nobody,
    # the plan file returned to its committed state, and the pointer survives
    # for a retry.
    assert not (repository / "docs" / "state" / PROJECT / "crew.json").exists()
    assert not ledger.run_path(PROJECT, run_id, repository).exists()
    plan_file = repository / "docs" / "plans" / f"{PLAN}.html"
    assert "commit-fails" not in plan_file.read_text(encoding="utf-8")
    assert pointer_path(run_id).exists()


def _promote_failing(
    repository: Path, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Promote a run whose landing commit is made to fail."""
    real_git = promotion._git

    def _failing_landing_commit(checkout, *arguments, **kwargs):
        if arguments and arguments[0] == "commit":
            return subprocess.CompletedProcess(
                arguments, returncode=1, stdout="", stderr="simulated commit failure"
            )
        return real_git(checkout, *arguments, **kwargs)

    monkeypatch.setattr(promotion, "_git", _failing_landing_commit)
    with pytest.raises(crew.CrewError, match="could not commit the landing writes"):
        _promote(repository, run_id, outcome="the landing commit fails and is restored")
    assert pointer_path(run_id).is_file()


# ── a worker's own landing record stops promotion authoring a second ─────────
#
# A worker that followed the landing contract wrote its record under the same
# run-derived comment id promotion derives, into its own tree, before the
# coordinator merges it. Promotion reads that record from the run's submitted
# commit and appends nothing, so the plan file stays untouched and the merge
# cannot collide on a duplicate comment id. A run without such a record still
# lands exactly one comment.


def _worker_plan_pointer(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    plan_html: str,
    *,
    write_paths: list[str] | None = None,
    code_file: bool = False,
) -> tuple[str, str]:
    """Write a real worker worktree holding ``plan_html`` and a pointer citing it.

    Returns (worker_sha, worker_tree); the worktree shares the repository's
    object store, so promotion resolves the worker's commit even though the
    ledger root has not merged it. ``code_file`` stages a separate deliverable
    (candidate.txt) so a worker with no plan record still has a commit to cite.
    """
    worker_tree = tmp_path / "worker"
    # The worker's own tree starts detached at main (main is checked out in the
    # ledger root), then commits its record onto that detached head — the same
    # state a real worker leaves before the coordinator merges it.
    _git(repository, "worktree", "add", "-q", "--detach", str(worker_tree), "main")
    if code_file:
        (worker_tree / "candidate.txt").write_text("delivered\n", encoding="utf-8")
        _git(worker_tree, "add", "candidate.txt")
    plan_file = worker_tree / "docs" / "plans" / f"{PLAN}.html"
    plan_file.write_text(plan_html, encoding="utf-8")
    _git(worker_tree, "add", f"docs/plans/{PLAN}.html")
    _git(worker_tree, "commit", "-q", "-m", "docs: author the landing record")
    worker_sha = _git(worker_tree, "rev-parse", "HEAD")
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        f"commits: {worker_sha}\n"
        f"changed_paths: {f'docs/plans/{PLAN}.html candidate.txt' if code_file else f'docs/plans/{PLAN}.html'}\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worker_tree),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-09-15T05:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "25m",
                "write_paths": write_paths
                or (["candidate.txt"] if code_file else [f"docs/plans/{PLAN}.html"]),
            },
        },
    )
    return worker_sha, worker_tree


def test_promotion_appends_no_second_comment_when_the_worker_authored_the_record(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260915T050001000001-worker-authored"
    comment_id = f"c-run-{run_id}"
    worker_html = _write_resource_html(
        repository,
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {
                "s2": [
                    {
                        "id": comment_id,
                        "who": "worker-a",
                        "when": "2026-09-15T05:01:00Z",
                        "body": "<p>the worker wrote its own landing record</p>",
                    }
                ]
            },
        },
    )
    worker_sha, _worker_tree = _worker_plan_pointer(
        repository, tmp_path, run_id, worker_html
    )
    _stored_review(run_id, dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20))

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="the worker already recorded it",
        commits=[worker_sha],
        root=repository,
    )

    assert promoted["plan_comment"] == {
        "recorded": False,
        "comment_id": comment_id,
        "section": "s2",
        "reason": "worker_authored_landing_record",
    }
    # No second comment: the ledger root plan holds no comment for this run and
    # the landing commit carried only the ledger row.
    plan, _version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    assert (plan["comments"].get("s2") or []) == []
    assert set(_landing_commit_paths(repository)) == {
        f"docs/state/{PROJECT}/runs/{run_id}.json"
    }
    assert not pointer_path(run_id).exists()


def test_a_run_without_a_worker_authored_record_still_lands_exactly_one_comment(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260915T050002000000-worker-silent"
    comment_id = f"c-run-{run_id}"
    bare = _store._resolve_html_file(PROJECT, PLAN, repository, artifact_type="plan")
    worker_sha, _worker_tree = _worker_plan_pointer(
        repository,
        tmp_path,
        run_id,
        bare.read_text(encoding="utf-8"),
        code_file=True,
    )
    _stored_review(run_id, dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20))

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="no worker record, so promotion lands the comment",
        commits=[worker_sha],
        root=repository,
    )

    assert promoted["plan_comment"]["recorded"] is True
    assert promoted["plan_comment"]["already_recorded"] is False
    plan, _version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    matching = [
        c for c in (plan["comments"].get("s2") or []) if c.get("id") == comment_id
    ]
    assert len(matching) == 1
    assert set(
        _landing_commit_paths(repository, _promotion_commit(repository, run_id))
    ) == {
        f"docs/state/{PROJECT}/runs/{run_id}.json",
        f"docs/plans/{PLAN}.html",
    }


def _write_resource_html(repository: Path, state: dict) -> str:
    """Return the serialized plan HTML a worker worktree would commit."""
    path = _store._resolve_html_file(PROJECT, PLAN, repository, artifact_type="plan")
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    return _plan_html.write_state(bare, state)


def _write_green_gate(repository: Path) -> str:
    """Land an in-tree gate that passes against the node deliverable."""
    (repository / "gate.sh").write_text(
        '#!/bin/sh\ngrep -q "satisfied" node.txt\n', encoding="utf-8"
    )
    (repository / "node.txt").write_text("satisfied\n", encoding="utf-8")
    _git(repository, "add", "gate.sh", "node.txt")
    _git(repository, "commit", "-q", "-m", "test: gate green at the base revision")
    return _git(repository, "rev-parse", "HEAD")


def test_rerun_catches_a_contract_landing_after_the_workers_base(
    repository: Path,
) -> None:
    base = _write_green_gate(repository)
    # The contract lands after the worker's base, so it never bound the
    # worker's own run. The merged tree now adds a requirement the node
    # deliverable does not satisfy, and the very same gate command exits red
    # at the integrated head: the base verdict was passed, the one that ships
    # is not.
    (repository / "gate.sh").write_text(
        '#!/bin/sh\ngrep -q "satisfied" node.txt '
        '&& grep -q "contract-marker" node.txt\n',
        encoding="utf-8",
    )
    _git(repository, "add", "gate.sh")
    _git(repository, "commit", "-q", "-m", "test: the contract test lands")
    integrated = _git(repository, "rev-parse", "HEAD")
    assert integrated != base

    rerun = promotion.rerun_gate_at_integrated_revision(
        repository=repository,
        gate_check={"command": "sh gate.sh", "exit_status": 0, "log_digest": "x"},
        base_verdict="passed",
        integrated_revision=integrated,
    )

    assert rerun["base_verdict"] == "passed"
    assert rerun["integrated_verdict"] == "failed"
    assert rerun["ran"] is True
    assert rerun["checkout_revision"] == integrated
    finding = rerun["finding"]
    assert finding is not None
    assert finding["base_verdict"] == "passed"
    assert finding["integrated_verdict"] == "failed"


def test_rerun_produces_no_finding_when_base_and_the_merged_head_agree(
    repository: Path,
) -> None:
    base = _write_green_gate(repository)

    rerun = promotion.rerun_gate_at_integrated_revision(
        repository=repository,
        gate_check={"command": "sh gate.sh", "exit_status": 0, "log_digest": "x"},
        base_verdict="passed",
        integrated_revision=base,
    )

    assert rerun["base_verdict"] == "passed"
    assert rerun["integrated_verdict"] == "passed"
    assert rerun["ran"] is True
    assert rerun["finding"] is None


def test_rerun_refuses_to_verify_a_tree_that_is_not_the_integrated_revision(
    repository: Path,
) -> None:
    base = _write_green_gate(repository)
    # A later commit moves the checkout off the integrated revision the caller
    # names. The re-run must never execute against the wrong tree, and the
    # refusal is the behaviour under test: silent non-verification is the
    # failure this guard exists to prevent.
    (repository / "seed.txt").write_text("seed\nchanged\n", encoding="utf-8")
    _git(repository, "add", "seed.txt")
    _git(repository, "commit", "-q", "-m", "test: a later integration commit")

    rerun = promotion.rerun_gate_at_integrated_revision(
        repository=repository,
        gate_check={"command": "sh gate.sh", "exit_status": 0, "log_digest": "x"},
        base_verdict="passed",
        integrated_revision=base,
    )

    assert rerun["ran"] is False
    assert rerun["integrated_verdict"] == "not-run"
    assert rerun["checkout_on_integrated_revision"] is False
    assert "wrong tree" in (rerun["reason"] or "")
    # A base-green gate the re-run could not establish on the tree that ships
    # is surfaced, never allowed to read as verified.
    assert rerun["finding"] is not None


def _promote_into_ledger(repository: Path, run_id: str) -> None:
    """Seed a promoted run with a stored gate, as a completed run records."""
    ledger.append_run(
        PROJECT,
        {
            "run_id": run_id,
            "gate": "passed",
            "gate_check": {
                "command": "sh gate.sh",
                "exit_status": 0,
                "log_digest": "x",
            },
        },
        root=repository,
    )


def test_verify_gate_cli_records_a_finding_against_the_run(repository: Path) -> None:
    """One direction: a contract landing after base leaves the gate red at the
    merged head, the verb re-runs it, and the finding is recorded on the run."""
    _write_green_gate(repository)
    run_id = "r-20260914T190400000000-verify-gate-perished"
    _promote_into_ledger(repository, run_id)
    # A contract lands after the promoted run's base, so the very same gate
    # command that was green when the run completed now exits red at the
    # integrated head: the re-run must produce a finding, not silence.
    (repository / "gate.sh").write_text(
        '#!/bin/sh\ngrep -q "satisfied" node.txt '
        '&& grep -q "contract-marker" node.txt\n',
        encoding="utf-8",
    )
    _git(repository, "add", "gate.sh")
    _git(repository, "commit", "-q", "-m", "test: a contract lands after the base")
    integrated = _git(repository, "rev-parse", "HEAD")

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "verify-gate",
            "--project",
            PROJECT,
            "--run",
            run_id,
            "--checkout-path",
            str(repository),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    finding = payload["finding"]
    assert finding["base_verdict"] == "passed"
    assert finding["integrated_verdict"] == "failed"
    assert payload["checkout_revision"] == integrated
    # The finding is recorded against the run, not only printed: the committed
    # ledger row the verb re-ran now carries the full re-run report.
    row = ledger.load(PROJECT, root=repository)[0]["runs"][0]
    report = row["integrated_gate_check"]
    assert report["integrated_verdict"] == "failed"
    assert report["checkout_revision"] == integrated
    assert report["finding"]["integrated_verdict"] == "failed"
    path = ledger.run_path(PROJECT, run_id, repository)
    assert payload["ledger_path"] == str(path)
    assert path.read_text() == ledger.serialize_run(row)
    assert not ledger.ledger_path(PROJECT, repository).exists()
    assert _landing_commit_paths(repository) == [
        path.relative_to(repository).as_posix()
    ]
    assert (
        json.loads(_git(repository, "show", f"HEAD:{path.relative_to(repository)}"))
        == row
    )


def test_verify_gate_cli_records_an_agreeing_head_without_a_finding(
    repository: Path,
) -> None:
    """The other direction: base and merged head agree, so the re-run finds
    nothing — but the re-check is still recorded, so a reader can tell an
    unchecked merge from a green one."""
    _write_green_gate(repository)
    run_id = "r-20260914T190401000000-verify-gate-withstand"
    _promote_into_ledger(repository, run_id)

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "verify-gate",
            "--project",
            PROJECT,
            "--run",
            run_id,
            "--checkout-path",
            str(repository),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["finding"] is None
    assert payload["report"]["integrated_verdict"] == "passed"
    row = ledger.load(PROJECT, root=repository)[0]["runs"][0]
    assert row["integrated_gate_check"]["integrated_verdict"] == "passed"
    assert row["integrated_gate_check"]["finding"] is None
    path = ledger.run_path(PROJECT, run_id, repository)
    assert payload["ledger_path"] == str(path)
    assert path.read_text() == ledger.serialize_run(row)
    assert not ledger.ledger_path(PROJECT, repository).exists()
    assert _landing_commit_paths(repository) == [
        path.relative_to(repository).as_posix()
    ]
    assert (
        json.loads(_git(repository, "show", f"HEAD:{path.relative_to(repository)}"))
        == row
    )


def test_verify_gate_cli_refuses_a_run_without_a_ledger_row(repository: Path) -> None:
    """The verb reads the run's stored gate from its committed row, so a run the
    coordinator never promoted is refused with the repair named, not re-run."""
    _write_green_gate(repository)
    run_id = "r-20260914T190402000000-verify-gate-unpromoted"

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "verify-gate",
            "--project",
            PROJECT,
            "--run",
            run_id,
            "--checkout-path",
            str(repository),
        ],
    )

    assert result.exit_code != 0
    assert "has no row in the 'proj' ledger" in result.output


def test_the_gate_rerun_caller_is_reachable_from_shipped_code() -> None:
    """The production caller is a CLI-reachable verb, not a test-only helper:
    its name must appear in the shipped tree — the promotion implementation,
    the crew facade export, and the CLI registration — none of them under
    tests/."""
    repo_root = Path(__file__).resolve().parent.parent
    shipped = sorted(
        path for path in (repo_root / "reckon").rglob("*.py") if "test" not in path.name
    )
    assert shipped, "expected shipped python under reckon/"
    hits = {
        str(path.relative_to(repo_root))
        for path in shipped
        if "record_gate_rerun_at_integrated_revision"
        in path.read_text(encoding="utf-8")
    }
    assert {
        "reckon/crew/promotion.py",
        "reckon/crew.py",
        "reckon/cli.py",
    } <= hits
