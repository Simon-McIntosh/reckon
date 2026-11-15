"""Manifest commit citations retain their meaning across worker formats."""

import json
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew.reports import parse_manifest
from reckon.crew.runs import _write_json, pointer_path
from tests import test_crew_promotion
from tests.helpers.git import git
from tests.test_crew_promotion import (
    PROJECT,
    _repository_with_candidate,
    _write_complete_manifest_pointer,
)

repository = test_crew_promotion.repository


@pytest.mark.parametrize("form", ["list", "json", "none", "commas"])
def test_each_commit_form_promotes_without_editing(
    repository: Path, tmp_path: Path, form: str
) -> None:
    base, first = _repository_with_candidate(repository)
    (repository / "candidate.txt").write_text("seed\ndelivered\nverified\n")
    git(repository, "add", "candidate.txt")
    git(repository, "commit", "-q", "-m", "test: verify candidate")
    second = git(repository, "rev-parse", "HEAD")
    first_short = git(repository, "rev-parse", "--short", first)
    expected = [first, second]
    entries = [
        f"{first_short} test: record candidate, with details",
        f"{second} test: verify candidate",
    ]
    values = {
        "list": "\n" + "\n".join(f"  - {entry}" for entry in entries),
        "json": json.dumps(entries),
        "none": "\n- none — this node writes a review record outside the repository",
        "commas": f"{first_short}, {second}",
    }
    run_id = f"r-manifest-commit-form-{form}"
    if form == "none":
        base = second
        expected = []
    manifest = _write_complete_manifest_pointer(
        repository,
        tmp_path,
        run_id,
        base=base,
        changed_paths="none" if form == "none" else "candidate.txt",
        commits=values[form],
    )
    if form == "none":
        record = json.loads(pointer_path(run_id).read_text())
        record["role"] = "review"
        _write_json(pointer_path(run_id), record)
    delivered = manifest.read_text()

    promoted = crew.complete(run_id, gate="passed", root=repository)

    assert promoted["record"]["commits"] == expected
    assert ledger.runs(PROJECT, root=repository)[0]["commits"] == expected
    assert not pointer_path(run_id).exists()
    assert manifest.read_text() == delivered
    assert len(parse_manifest(delivered)["commits"]) == (1 if form == "none" else 2)


def test_an_unresolvable_list_commit_refuses_promotion(
    repository: Path, tmp_path: Path
) -> None:
    base, commit = _repository_with_candidate(repository)
    missing = "0123456789abcdef0123456789abcdef01234567"
    assert git(repository, "rev-parse", "--verify", f"{commit}^{{commit}}") == commit
    assert missing not in git(repository, "rev-list", "--all").splitlines()
    run_id = "r-manifest-unresolvable-list-commit"
    _write_complete_manifest_pointer(
        repository,
        tmp_path,
        run_id,
        base=base,
        changed_paths="candidate.txt",
        commits=f"\n  - {missing} test: missing",
    )

    with pytest.raises(crew.CrewError, match=missing) as refusal:
        crew.complete(run_id, gate="passed", root=repository)

    assert "does not resolve to a commit object" in str(refusal.value)
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).exists()
