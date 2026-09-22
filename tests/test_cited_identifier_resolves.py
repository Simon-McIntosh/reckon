"""An identifier a manifest cites must resolve in the repository that owns it.

A commit sha reaches the ledger row as text, and a value assembled rather than
copied passes every shape check there is: forty hexadecimal characters, the
right eight-character prefix, and nothing behind it. The only instrument that
separates a real citation from a fabricated one is the store itself, so
promotion asks it about the value as cited and refuses a citation that resolves
to no object — naming the citation and the run — rather than storing a row that
reads as evidence and points at nothing.

The check reads the citation as the producer honestly wrote it. A width or
character-class requirement is the trap this avoids: it demands a form the
producer's tooling may not hand it, so compliance means guessing, and the guess
is exactly the fabricated value the check was meant to catch.

Every case enters through ``crew complete``, the path that records a run's
commits into the ledger, and every case starts from a run whose gate passed and
whose citations are the only thing under test.

The check reads the citations of a run that presents none of its own. A
promotion that presents at least one commit takes the branch above and resolves
only the presented list, so an unresolvable value declared beside a presented
one is still dropped from the shortfall comparison rather than refused; that
branch's documented silence is pinned by another test file, outside this node's
granted paths, and is reported as a follow-on rather than changed here. The
commitless branch is where the defect was measured: a citation the coordinator
never copied into ``--commit`` reaches the record as manifest text, and nothing
resolved it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import ledger
from reckon.cli import main as cli_main
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "citation"

# The measured fabricated value: the right shape, the right prefix, no object.
_FABRICATED = "7892cb696b2fbb52ac9b3e2ded0f5a5a26a3b2d1"
# A forty-character hexadecimal value that resolves to nothing in any store.
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


def _commit_of(repository: Path, name: str) -> str:
    """Create one commit touching ``<name>.txt`` and return its canonical sha."""
    path = f"{name}.txt"
    (repository / path).write_text(f"{name}\n", encoding="utf-8")
    _git(repository, "add", path)
    _git(repository, "commit", "-q", "-m", f"feat: add {name}")
    return _git(repository, "rev-parse", "HEAD")


def _pointer(repository: Path, run_id: str, manifest: Path, *, base: str) -> Path:
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    path = pointer_path(run_id)
    _write_json(
        path,
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
                "id": "node-citation",
                "plan": "missing-plan",
                "section": "citation",
                "time_budget": "20m",
                "write_paths": ["landed.txt"],
            },
        },
    )
    return path


def _promote(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str,
    declared: str,
    presented: tuple[str, ...] = (),
    no_commit: str = "",
):
    """Promote one run through the CLI, with ``declared`` as its commits line."""
    manifest = tmp_path / f"{run_id}.manifest.md"
    manifest.write_text(
        f"node: fixture\nstatus: complete\ncommits: {declared}\ntests: done\n",
        encoding="utf-8",
    )
    pointer = _pointer(
        repository, run_id, manifest, base=_git(repository, "rev-parse", "HEAD^")
    )
    arguments = [
        "crew",
        "complete",
        "--run",
        run_id,
        "--gate",
        "passed",
        "--checkout-path",
        str(repository),
        "--gate-command",
        "pytest -q",
        "--gate-exit-status",
        "0",
        "--gate-log-path",
        "/durable/citation.log",
        "--waive-unreviewed-promotion",
        "the fixture measures the citation check, not the review gate",
    ]
    arguments += ["--commit", *presented] if presented else []
    if no_commit:
        arguments += ["--no-commit", no_commit]
    result = CliRunner().invoke(cli_main, arguments)
    return result, pointer, manifest


def _rows(repository: Path) -> list[dict]:
    return ledger.runs(PROJECT, root=repository)


# ── A citation that resolves is admitted ────────────────────────────────────


def test_a_full_commit_sha_that_resolves_is_admitted(
    repository: Path, tmp_path: Path
) -> None:
    landed = _commit_of(repository, "landed")

    result, pointer, _ = _promote(
        repository,
        tmp_path,
        run_id="r-full",
        declared=landed,
        presented=(landed,),
    )

    assert result.exit_code == 0, result.output
    assert not pointer.exists()
    assert [row["commits"] for row in _rows(repository)] == [[landed]]


def test_an_abbreviated_commit_sha_that_resolves_is_admitted(
    repository: Path, tmp_path: Path
) -> None:
    landed = _commit_of(repository, "landed")

    result, pointer, _ = _promote(
        repository,
        tmp_path,
        run_id="r-abbrev",
        declared=landed[:9],
        presented=(landed[:9],),
    )

    assert result.exit_code == 0, result.output
    assert not pointer.exists()
    # The row records the canonical id, so an honest abbreviation stays useful
    # without the ledger carrying a spelling nothing can be resolved from.
    assert [row["commits"] for row in _rows(repository)] == [[landed]]


def test_a_citation_naming_a_tag_is_admitted(repository: Path, tmp_path: Path) -> None:
    landed = _commit_of(repository, "landed")
    _git(repository, "tag", "landed-tag", landed)

    result, pointer, _ = _promote(
        repository,
        tmp_path,
        run_id="r-tag",
        declared="landed-tag",
        presented=("landed-tag",),
    )

    assert result.exit_code == 0, result.output
    assert not pointer.exists()
    assert [row["commits"] for row in _rows(repository)] == [[landed]]


# ── A citation that resolves to nothing is refused ──────────────────────────


def test_the_measured_fabricated_sha_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    _commit_of(repository, "landed")

    result, _, _ = _promote(
        repository,
        tmp_path,
        run_id="r-fabricated",
        declared=_FABRICATED,
    )

    assert result.exit_code != 0, result.output
    assert _FABRICATED in result.output
    assert "r-fabricated" in result.output
    # Nothing was recorded, so the refusal is not a report that lands beside a
    # stored row: the pointer survives for the coordinator to fix; no ledger.
    assert _rows(repository) == []


def test_a_well_formed_sha_with_no_object_behind_it_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    _commit_of(repository, "landed")

    result, _, _ = _promote(
        repository,
        tmp_path,
        run_id="r-gone40",
        declared=_GONE_40,
    )

    assert result.exit_code != 0, result.output
    assert _GONE_40 in result.output
    assert "does not resolve" in result.output
    assert _rows(repository) == []


def test_a_citation_that_is_not_hexadecimal_at_all_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    _commit_of(repository, "landed")

    result, _, _ = _promote(
        repository,
        tmp_path,
        run_id="r-nothex",
        declared="the-commit-i-made-earlier",
    )

    assert result.exit_code != 0, result.output
    assert "the-commit-i-made-earlier" in result.output
    assert "r-nothex" in result.output
    assert _rows(repository) == []


def test_the_refusal_turns_on_resolvability_and_not_on_the_spelling(
    repository: Path, tmp_path: Path
) -> None:
    """The same value is admitted the moment the store holds it.

    The fabricated value and a value the store has are the same shape; only one
    is refused, which is the assertion that the check is asking the store rather
    than judging the text.
    """
    landed = _commit_of(repository, "landed")
    assert len(landed) == len(_FABRICATED)

    refused, _, _ = _promote(
        repository, tmp_path, run_id="r-turned-off", declared=_FABRICATED
    )
    admitted, _, _ = _promote(
        repository, tmp_path, run_id="r-turned-on", declared=landed, presented=(landed,)
    )

    assert refused.exit_code != 0
    assert admitted.exit_code == 0, admitted.output


# ── A manifest that names no commit is unaffected ───────────────────────────


def test_a_declared_absence_is_not_read_as_a_citation(
    repository: Path, tmp_path: Path
) -> None:
    """``commits: none (…)`` is the record stating it has no commit."""
    _commit_of(repository, "landed")

    result, pointer, _ = _promote(
        repository,
        tmp_path,
        run_id="r-none",
        declared="none (repository worktree remained clean)",
        no_commit="report-only node: its deliverable is the manifest, not a commit",
    )

    assert result.exit_code == 0, result.output
    assert not pointer.exists()
    rows = _rows(repository)
    assert rows and rows[0]["no_commit"]


def test_a_manifest_with_no_commits_line_is_unaffected(
    repository: Path, tmp_path: Path
) -> None:
    _commit_of(repository, "landed")
    manifest = tmp_path / "r-silent.manifest.md"
    manifest.write_text(
        "node: fixture\nstatus: complete\ntests: done\n", encoding="utf-8"
    )
    pointer = _pointer(
        repository, "r-silent", manifest, base=_git(repository, "rev-parse", "HEAD^")
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--run",
            "r-silent",
            "--gate",
            "passed",
            "--checkout-path",
            str(repository),
            "--gate-command",
            "pytest -q",
            "--gate-exit-status",
            "0",
            "--gate-log-path",
            "/durable/citation.log",
            "--no-commit",
            "report-only node: nothing to register",
            "--waive-unreviewed-promotion",
            "the fixture measures the citation check, not the review gate",
        ],
    )

    assert result.exit_code == 0, result.output
    assert not pointer.exists()


# ── The refusal is a refusal, not a report ─────────────────────────────────


def test_the_refusal_leaves_no_row_behind_it(repository: Path, tmp_path: Path) -> None:
    """The row is not written, so the coordinator reads a refusal, not a record.

    A report beside a stored row would read as a promotion that happened; this
    exits non-zero and appends nothing, which is the difference between a
    defective citation being caught and being filed.
    """
    _commit_of(repository, "landed")

    result, pointer, _ = _promote(
        repository,
        tmp_path,
        run_id="r-unwritten",
        declared=_FABRICATED,
    )

    assert result.exit_code != 0, result.output
    assert "Traceback" not in result.output
    assert _rows(repository) == []
    # The pointer survives the refusal, so the coordinator can fix the citation
    # and promote the run rather than rediscovering it as a lost session.
    assert pointer.exists()
