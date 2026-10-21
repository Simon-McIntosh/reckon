"""A landing judges a changed path by blob identity, so a binary file can land.

``merge.land`` reads every changed path at the merge commit and refuses one
whose change did not survive. It read each path as text — ``git show
<revision>:<path>`` decoded as UTF-8 — so a head that added a binary file raised
``UnicodeDecodeError`` before any comparison and the run could not be landed at
all. The check now reads a path's blob id first: ``git rev-parse
<revision>:<path>`` names the object without decoding it, so a path whose merged
blob equals the head's survived whether it is text or binary, and content is
read only for a path whose blob differs, decoded only when it is text.

The declared mutation reads the binary path through the text ``git show`` again,
and the first case raises ``UnicodeDecodeError``, which is the red log the
manifest names.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from reckon.crew import merge
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import _write_origin_and_checkout

BINARY = b"\x00\x01\x02\x87binary payload\n"
ALTERED = b"\x00\x01\x02\x88a different binary payload\n"
TEXT_MARKER = "the reviewed change survives the merge\n"


def _head_adding_a_binary_file(repository: Path) -> str:
    """Commit a head that adds a binary file and edits a text file; return its sha."""
    _git(repository, "checkout", "-q", "-b", "reviewed")
    (repository / "asset.bin").write_bytes(BINARY)
    (repository / "reviewed.txt").write_text(TEXT_MARKER, encoding="utf-8")
    _git(repository, "add", "asset.bin", "reviewed.txt")
    _git(repository, "commit", "-q", "-m", "add a binary asset and a text change")
    head = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", "main")
    return head


def _blob_bytes(repository: Path, revision: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repository), "cat-file", "blob", f"{revision}:{path}"],
        check=True,
        capture_output=True,
    ).stdout


def _candidate_merge(
    repository: Path,
    base: str,
    head: str,
    *,
    drop: str | None = None,
    alter: tuple[str, bytes] | None = None,
) -> str:
    """Return a candidate merge commit whose tree drops or alters one path.

    The tree starts as the merge-tree result a correct landing would commit, so
    the only difference the landing sees is the one named here.
    """
    tree = merge._run(repository, "merge-tree", "--write-tree", base, head)
    merged_tree = tree.stdout.splitlines()[0].strip()
    index = repository.parent / f"candidate-index-{head[:12]}"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    merge._run(repository, "read-tree", merged_tree, env=env)
    if drop is not None:
        merge._run(repository, "update-index", "--force-remove", drop, env=env)
    if alter is not None:
        path, data = alter
        blob = subprocess.run(
            ["git", "-C", str(repository), "hash-object", "-w", "--stdin"],
            input=data,
            check=True,
            capture_output=True,
        ).stdout.decode()
        merge._run(
            repository,
            "update-index",
            "--cacheinfo",
            f"100644,{blob.strip()},{path}",
            env=env,
        )
    candidate_tree = merge._run(repository, "write-tree", env=env).stdout.strip()
    index.unlink(missing_ok=True)
    return merge._commit_tree(
        repository, candidate_tree, [base, head], "land\n\na candidate merge tree\n"
    )


def test_a_head_adding_a_binary_file_lands_and_is_judged_by_blob_identity(
    tmp_path: Path,
) -> None:
    """The landing carries the binary path, reports it by blob, and stays readable."""
    _origin, repository = _write_origin_and_checkout(tmp_path)
    head = _head_adding_a_binary_file(repository)

    report = merge.land(repository, head=head, message="land", body="the change lands")

    assert report["pushed"] is True
    merged = report["commit"]
    head_blob = _git(repository, "rev-parse", f"{head}:asset.bin")
    # The merged tree carries the head's blob for the binary path, byte for byte.
    assert _git(repository, "rev-parse", f"{merged}:asset.bin") == head_blob
    assert _blob_bytes(repository, merged, "asset.bin") == BINARY
    # The report names the changed paths, and the binary one is judged by blob
    # identity rather than by markers it has none of.
    assert report["survived"]["paths"] == ["asset.bin", "reviewed.txt"]
    assert report["survived"]["judgements"]["asset.bin"] == "blob_identity"
    # The text file's marker landed as it does at base.
    assert TEXT_MARKER.strip() in _git(repository, "show", f"{merged}:reviewed.txt")
    assert report["survived"]["judgements"]["reviewed.txt"] == ("blob_identity")


def test_a_candidate_that_drops_the_binary_file_is_refused(tmp_path: Path) -> None:
    """A merge tree without the binary path is refused, naming it, nothing pushed."""
    origin, repository = _write_origin_and_checkout(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")
    head = _head_adding_a_binary_file(repository)
    candidate = _candidate_merge(repository, base, head, drop="asset.bin")
    origin_before = _git(origin, "rev-parse", "main")

    with pytest.raises(merge.MergeError) as refused:
        merge.land(repository, head=head, candidate=candidate)

    assert "asset.bin" in str(refused.value)
    assert "dropped it" in str(refused.value)
    assert _git(origin, "rev-parse", "main") == origin_before


def test_a_candidate_that_alters_the_binary_file_is_refused(tmp_path: Path) -> None:
    """A merge tree with different bytes for the binary path is refused, naming it."""
    origin, repository = _write_origin_and_checkout(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")
    head = _head_adding_a_binary_file(repository)
    candidate = _candidate_merge(repository, base, head, alter=("asset.bin", ALTERED))
    origin_before = _git(origin, "rev-parse", "main")

    with pytest.raises(merge.MergeError) as refused:
        merge.land(repository, head=head, candidate=candidate)

    assert "asset.bin" in str(refused.value)
    assert "not the head's" in str(refused.value)
    assert _git(origin, "rev-parse", "main") == origin_before


def test_a_text_path_that_lost_its_line_is_refused_by_the_marker_check(
    tmp_path: Path,
) -> None:
    """A present text path missing the introduced line reaches the marker check.

    The merged copy of the text file still exists, so this cannot be refused by
    the absent-path branch: it is refused only because the line the change
    introduced is not in it, which is the comparison the marker check makes.
    """
    origin, repository = _write_origin_and_checkout(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")
    head = _head_adding_a_binary_file(repository)
    candidate = _candidate_merge(
        repository, base, head, alter=("reviewed.txt", b"a different line\n")
    )
    origin_before = _git(origin, "rev-parse", "main")

    with pytest.raises(merge.MergeError) as refused:
        merge.land(repository, head=head, candidate=candidate)

    assert "reviewed.txt" in str(refused.value)
    assert TEXT_MARKER.strip() in str(refused.value)
    assert "did not survive the merge" in str(refused.value)
    assert _git(origin, "rev-parse", "main") == origin_before
