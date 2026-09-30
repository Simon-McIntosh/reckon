"""A review's promotion scope is the declaration its manifest was checked against.

A review's only deliverable is the record it stores beside the run it read — an
absolute path outside every repository, granted by its dispatch — together with
the copy the store keys by the revision the review read. The write-time audit
accepts both: a declaration that resolves to no repository root is compared as
the absolute path it already is, and the head-keyed sibling is that record under
another name. The promotion-time scope test kept the older containment rule,
which drops such a declaration and reports the grant it made as stray, so one
delivery was accepted by the check a worker runs against its own manifest and
refused by the surface that reads it hours later.

The review case is entered through both surfaces here: the manifest check
accepts the manifest, the promotion-time scope test reports nothing outside, and
the run promotes. Every acceptance is paired with what the same rule still
refuses — a neighbour of the store record that is not its head-keyed sibling,
and a committed path outside every declaration — so the acceptance is shown to
be declaration-bound rather than a blanket admission of paths outside a
repository.

The fixture asserts afterwards that the workstation's real crew pointer
directory is untouched, because an isolated read does not prove an isolated
write.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import crew, ledger
from reckon.cli import main as cli_main
from reckon.crew.promotion import _outside_declared_scope
from reckon.crew.review import review_path
from reckon.crew.runs import (
    _write_json,
    crew_home,
    pointer_path,
    read_pointer,
)

PROJECT = "review-promotion-fixture"
PLAN = "review-promotion-target"
RUN_ID = "r-20260930T120000000000-review-of-a-landed-node"
REVIEWED_HEAD = "a3a7c58ce5792eed6d57adae6e54c48b08a86410"
DECLARED_PATH = "candidate.txt"

# Neighbours of the store record that are not the record's head-keyed sibling:
# another stem, a suffix too short to be the store's revision key, and a
# different extension. Each is a file beside the granted record and none of them
# is a deliverable this run declared.
NOT_THE_SIBLING = ("another-stem", "revision-suffix-too-short", "another-extension")


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    """No fixture may reach the workstation's real crew pointer directory."""
    real_pointer = (
        Path.home() / ".config" / "reckon" / "crew" / "live" / f"{RUN_ID}.json"
    )
    assert not real_pointer.exists()
    yield
    assert not real_pointer.exists()


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A repository whose docs directory is the project's mount."""
    assert crew_home().is_relative_to(isolated_reckon_home)
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n',
        encoding="utf-8",
    )
    (root / DECLARED_PATH).write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs", DECLARED_PATH),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _store_paths() -> tuple[Path, Path]:
    """The record a review's dispatch grants and the head-keyed copy beside it."""
    return (
        review_path(PROJECT, RUN_ID),
        review_path(PROJECT, RUN_ID, reviewed_head_sha=REVIEWED_HEAD),
    )


def _write_store() -> tuple[Path, Path]:
    record, sibling = _store_paths()
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{}\n", encoding="utf-8")
    sibling.write_text("{}\n", encoding="utf-8")
    return record, sibling


def _manifest_body(changed_paths: list[str], *, commits: str = "none") -> str:
    return (
        "node: review-of-a-landed-node\n"
        "status: complete\n"
        f"commits: {commits}\n"
        "changed_paths:\n"
        + "".join(f"  - {path}\n" for path in changed_paths)
        + "tests: record parsed, stored and parse-checked\n"
    )


def _review_run(repository: Path, tmp_path: Path) -> dict[str, Path]:
    """A review run whose whole declared scope is the record it stores."""
    record, sibling = _write_store()
    manifest = tmp_path / "manifest.md"
    manifest.write_text(_manifest_body([str(record), str(sibling)]), encoding="utf-8")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "backend": "native",
            "created_at": "2026-09-30T12:00:00Z",
            "role": "review",
            "manifest_path": str(manifest),
            "node": {
                "id": "review-of-a-landed-node",
                "plan": PLAN,
                "section": "s4",
                "time_budget": "25m",
                "role": "review",
                "write_paths": [str(record), str(sibling)],
            },
        },
    )
    return {"record": record, "sibling": sibling, "manifest": manifest}


def _check_manifest() -> tuple[int, list[str]]:
    result = CliRunner().invoke(cli_main, ["crew", "check-manifest", "--run", RUN_ID])
    return result.exit_code, json.loads(result.output)["findings"]


def _outside(changed: list[str]) -> tuple[str, ...]:
    """Enter the promotion-time scope test as promotion itself enters it."""
    record = read_pointer(RUN_ID)
    return _outside_declared_scope(
        changed,
        record["node"]["write_paths"],
        record=record,
        tree=Path(str(record["worktree"])),
    )


def test_the_manifest_check_accepts_the_review_record_and_its_sibling(
    repository: Path, tmp_path: Path
) -> None:
    declared = _review_run(repository, tmp_path)

    exit_code, findings = _check_manifest()

    assert exit_code == 0, findings
    assert findings == []
    assert declared["record"].is_file() and declared["sibling"].is_file()


def test_promotion_finds_nothing_outside_the_review_declarations(
    repository: Path, tmp_path: Path
) -> None:
    """The granted record and the copy keyed beside it are both in scope."""
    declared = _review_run(repository, tmp_path)

    outside = _outside([str(declared["record"]), str(declared["sibling"])])

    assert outside == ()


def test_the_review_run_promotes_over_its_own_record(
    repository: Path, tmp_path: Path
) -> None:
    _review_run(repository, tmp_path)

    promoted = crew.complete(RUN_ID, gate="passed", root=repository)

    assert promoted["record"]["commits"] == []
    assert not pointer_path(RUN_ID).exists()
    [row] = ledger.runs(PROJECT, root=repository)
    assert row["run_id"] == RUN_ID


def _neighbour(record: Path, kind: str) -> Path:
    """A file beside the granted record that is not its head-keyed sibling."""
    return {
        "another-stem": record.with_name("a-different-record.json"),
        "revision-suffix-too-short": record.with_name(f"{record.stem}.at-abc123.json"),
        "another-extension": record.with_name(f"{record.stem}.txt"),
    }[kind]


@pytest.mark.parametrize("kind", NOT_THE_SIBLING)
def test_a_neighbour_of_the_store_record_is_still_outside_at_promotion(
    repository: Path, tmp_path: Path, kind: str
) -> None:
    """Beside the record is not the same as being the record, keyed by head.

    The store writes the copy a review is read back by, so the rule admits a
    name sharing the granted record's directory, stem and extension with a
    revision suffix. A different stem, a suffix too short to be a revision, and
    a different extension each remain stray, so the acceptance rests on the
    store's own naming rather than on the record's neighbourhood.
    """
    declared = _review_run(repository, tmp_path)
    candidate = _neighbour(declared["record"], kind)
    candidate.write_text("{}\n", encoding="utf-8")

    outside = _outside([str(candidate)])

    assert outside == (str(candidate),)


def test_a_committed_path_outside_every_declaration_is_still_refused(
    repository: Path, tmp_path: Path
) -> None:
    """The other direction: promotion still refuses what no declaration grants.

    A non-review run cites the commit it made, and that commit's own diff holds
    a path the node never declared. The scope test reads the committed paths at
    promotion and names the stray one, so the acceptance above is not a blanket
    admission of every path outside a declaration.
    """
    undeclared = "undeclared.txt"
    (repository / undeclared).write_text("work\n", encoding="utf-8")
    _git(repository, "add", "--", undeclared)
    _git(repository, "commit", "-q", "-m", "test: undeclared work")
    commit = _git(repository, "rev-parse", "HEAD")
    manifest = tmp_path / "manifest.md"
    manifest.write_text(_manifest_body([undeclared], commits=commit), encoding="utf-8")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-list", "--max-parents=0", "HEAD"),
            "launch": "in-harness",
            "backend": "native",
            "created_at": "2026-09-30T12:00:00Z",
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": "build-the-target",
                "plan": PLAN,
                "section": "s4",
                "time_budget": "25m",
                "role": "implement",
                "write_paths": [DECLARED_PATH],
            },
        },
    )

    with pytest.raises(crew.CrewError, match="outside its declared write scope"):
        crew.complete(RUN_ID, gate="passed", commits=[commit], root=repository)

    assert pointer_path(RUN_ID).is_file()
    assert ledger.runs(PROJECT, root=repository) == []
