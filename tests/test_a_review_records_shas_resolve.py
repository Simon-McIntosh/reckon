"""A review record's revisions resolve before its review run completes.

A review worker types the revision pair into its stored record by hand, and a
sha that lost a couple of characters mid-value still has the shape of one.
Measured on a re-review: the stored record carried a 38-character head that
resolves to no object, the review run checked and promoted clean, and only the
reviewed run's own promotion noticed — it found no review of the revision the
run had reached and refused, and the reflex then dispatched a whole second
review. So the pair is resolved against the reviewed run's own repository at
both points a record passes while its author still holds a turn: ``store_review``,
which is the write, and ``crew check-manifest`` on the review run.

Every refusal is paired with the true-head case the same rule must accept, so
the refusal is shown to rest on the resolution rather than on a blanket ban on
either field, and the reproduction opens the file: the probe that reports the
corrupt head unresolved is first shown resolving the head that is really there.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, crew_home, pointer_path

PROJECT = "sha-resolution-fixture"
REVIEWED_RUN_ID = "r-20261004T000000000000-reviewed-run"
REVIEW_RUN_ID = "r-20261004T000001000000-review-of-reviewed-run"

REVIEW_MANIFEST = """\
node: review-of-{reviewed}
status: complete
commits: none
changed_paths:
  - {record}
tests: record parsed, stored and parse-checked
"""


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _names_a_commit(repository: Path, revision: str) -> bool:
    """Whether ``revision`` resolves to a commit in ``repository``."""
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def _commit(repository: Path, name: str, body: str, message: str) -> str:
    (repository / name).write_text(body, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _lost_characters(sha: str) -> str:
    """The measured corruption: two characters dropped from the middle."""
    return sha[:10] + sha[12:]


def _record_path() -> Path:
    return review_module.review_path(PROJECT, REVIEWED_RUN_ID)


def _record(*, base: str, head: str) -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN_ID,
        "reviewed_base_sha": base,
        "reviewed_head_sha": head,
    }


def _check(run_id: str = REVIEW_RUN_ID):
    return CliRunner().invoke(cli_main, ["crew", "check-manifest", "--run", run_id])


def _findings(run_id: str = REVIEW_RUN_ID) -> tuple[int, list[str]]:
    result = _check(run_id)
    return result.exit_code, json.loads(result.output)["findings"]


@pytest.fixture()
def reviewed_run(tmp_path: Path, isolated_reckon_home: Path) -> dict:
    """A reviewed run whose worktree carries a real head, and its review run.

    Every directory this case resolves is named by the crew home the suite put
    in force, asserted here rather than assumed: a home resolution that stopped
    honouring the substitution would otherwise write pointers into the
    production tree this case has no business touching.
    """
    assert crew_home().is_relative_to(isolated_reckon_home)
    assert review_module.review_store_root().is_relative_to(isolated_reckon_home)

    repository = tmp_path / "repo"
    repository.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(repository, *arguments)
    base = _commit(repository, "seed.txt", "seed\n", "chore: seed")
    head = _commit(repository, "work.txt", "delivered\n", "feat: the reviewed change")

    record = _record_path()
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        REVIEW_MANIFEST.format(reviewed=REVIEWED_RUN_ID, record=record),
        encoding="utf-8",
    )

    _write_json(
        pointer_path(REVIEWED_RUN_ID),
        {
            "run_id": REVIEWED_RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": base,
            "launch": "cli",
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": "reviewed-node",
                "plan": "fixture",
                "section": "s1",
                "role": "implement",
                "write_paths": ["work.txt"],
            },
        },
    )
    _write_json(
        pointer_path(REVIEW_RUN_ID),
        {
            "run_id": REVIEW_RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": base,
            "launch": "cli",
            "role": "review",
            "manifest_path": str(manifest),
            "node": {
                "id": f"review-of-{REVIEWED_RUN_ID}",
                "plan": "fixture",
                "section": "s1",
                "role": "review",
                # The dispatch grants both spellings, as the reflex composes it.
                "write_paths": [
                    str(record),
                    str(
                        review_module.review_path(
                            PROJECT, REVIEWED_RUN_ID, reviewed_head_sha="0" * 40
                        )
                    ),
                ],
            },
        },
    )
    return {"repo": repository, "base": base, "head": head, "record": record}


def test_the_unresolved_head_reproduces(reviewed_run: dict) -> None:
    """The probe sees the head that is there before it reports the one that is not."""
    real = reviewed_run["head"]
    corrupted = _lost_characters(real)

    assert len(real) == 40, real
    assert len(corrupted) == 38, corrupted
    assert corrupted != real
    assert _names_a_commit(reviewed_run["repo"], real) is True
    assert _names_a_commit(reviewed_run["repo"], corrupted) is False


def test_store_review_refuses_a_head_that_names_no_commit(reviewed_run: dict) -> None:
    corrupted = _lost_characters(reviewed_run["head"])

    with pytest.raises(ValueError, match="reviewed_head_sha") as refused:
        review_module.store_review(_record(base=reviewed_run["base"], head=corrupted))

    message = str(refused.value)
    assert "reviewed_head_sha" in message, message
    assert corrupted in message, message
    assert reviewed_run["head"] in message, message
    assert not reviewed_run["record"].exists()


def test_store_review_refuses_a_base_that_names_no_commit(reviewed_run: dict) -> None:
    corrupted = _lost_characters(reviewed_run["base"])

    with pytest.raises(ValueError, match="reviewed_base_sha") as refused:
        review_module.store_review(_record(base=corrupted, head=reviewed_run["head"]))

    message = str(refused.value)
    assert "reviewed_base_sha" in message, message
    assert corrupted in message, message
    assert reviewed_run["head"] in message, message
    assert not reviewed_run["record"].exists()


def test_a_record_with_the_true_revision_pair_is_stored(reviewed_run: dict) -> None:
    """The positive control: the same write the corrupt pair is refused."""
    written = review_module.store_review(
        _record(base=reviewed_run["base"], head=reviewed_run["head"])
    )

    assert written.is_file()
    assert reviewed_run["head"] in written.name


def test_check_manifest_refuses_a_record_whose_head_names_no_commit(
    reviewed_run: dict,
) -> None:
    """A record written by hand, as a reviewer writes it, is refused at the check."""
    corrupted = _lost_characters(reviewed_run["head"])
    reviewed_run["record"].parent.mkdir(parents=True, exist_ok=True)
    reviewed_run["record"].write_text(
        json.dumps(_record(base=reviewed_run["base"], head=corrupted)),
        encoding="utf-8",
    )

    exit_code, findings = _findings()

    assert exit_code != 0, findings
    assert len(findings) == 1, findings
    assert "reviewed_head_sha" in findings[0], findings
    assert corrupted in findings[0], findings
    assert reviewed_run["head"] in findings[0], findings


def test_check_manifest_refuses_a_record_whose_base_names_no_commit(
    reviewed_run: dict,
) -> None:
    corrupted = _lost_characters(reviewed_run["base"])
    reviewed_run["record"].parent.mkdir(parents=True, exist_ok=True)
    reviewed_run["record"].write_text(
        json.dumps(_record(base=corrupted, head=reviewed_run["head"])),
        encoding="utf-8",
    )

    exit_code, findings = _findings()

    assert exit_code != 0, findings
    assert len(findings) == 1, findings
    assert "reviewed_base_sha" in findings[0], findings
    assert corrupted in findings[0], findings
    assert reviewed_run["head"] in findings[0], findings


def test_a_record_with_the_true_revision_pair_passes_the_check(
    reviewed_run: dict,
) -> None:
    review_module.store_review(
        _record(base=reviewed_run["base"], head=reviewed_run["head"])
    )

    exit_code, findings = _findings()

    assert exit_code == 0, findings
    assert findings == []
