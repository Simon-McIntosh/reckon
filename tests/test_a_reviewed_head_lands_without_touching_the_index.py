"""A reviewed head lands by object-building, never by writing the shared index.

A coordinator lands a reviewed worker head in the shared checkout while
promotions are writing commits there at once. ``git merge --no-ff`` writes the
merged files into the working tree and stages them before it commits, so a
concurrent writer that wins the ``.git/index.lock`` race leaves the merged files
staged and the checkout dirty until someone repairs it by hand. The landing here
is built out of objects instead: ``merge-tree`` computes the tree,
``commit-tree`` the commit, and the checkout is touched only by the closing
fast-forward, which either completes or leaves it as it was.

Nothing here is asserted by inspecting the argv alone. Every case builds a real
repository with a bare origin, a reviewed head on a side commit and an unrelated
tracked file modified in the working tree, then runs the landing and reads the
checkout, the index and the remote back.

The declared mutation is the conflict refusal removed; ``RECKON_MERGE_CONFLICT
REFUSAL=removed`` applies it under pytest, and the conflict case then fails its
byte-identity assertion, which is the red log the manifest names.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.crew import merge

DECLARED_MUTATION = (
    "with the conflict refusal removed, a conflicting head leaves conflict "
    "state in the checkout and the byte-identity assertion fails"
)

# ``GIT_OPTIONAL_LOCKS=0`` keeps a read of the index from rewriting it, so the
# byte-identity assertion measures the landing and not the reading of it.
_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}

# An ``ssh`` that models a host refusing GitHub on port 22: the default route
# fails the way a compute node does, and the port-443 route git builds from
# ``GIT_SSH_COMMAND`` serves the remote git command locally, so the retried push
# reaches the bare origin and can be read back.
_SSH_STUB = """\
#!/bin/sh
route443=0
for arg in "$@"; do
  if [ "$arg" = "Port=443" ]; then route443=1; fi
done
if [ "$route443" -eq 0 ]; then
  echo "ssh: connect to host github.com port 22: Connection refused" >&2
  exit 255
fi
cmd=""
for arg in "$@"; do cmd="$arg"; done
case "$cmd" in
  git-receive-pack*) cmd="git receive-pack${cmd#git-receive-pack}" ;;
  git-upload-pack*) cmd="git upload-pack${cmd#git-upload-pack}" ;;
esac
exec sh -c "$cmd"
"""


def _run(repo: Path, *args: str):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=_ENV,
    )


def _git(repo: Path, *args: str) -> str:
    result = _run(repo, *args)
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _init_checkout(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True, env=_ENV)
    subprocess.run(["git", "init", "-q", str(work)], check=True, env=_ENV)
    _git(work, "config", "user.email", "author@example.invalid")
    _git(work, "config", "user.name", "Author")
    _git(work, "config", "commit.gpgsign", "false")
    (work / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(work, "add", "tracked.txt")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "branch", "-M", "main")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "main")
    return origin, work


def _reviewed_head(work: Path, branch: str, filename: str, content: str) -> str:
    """Commit one file on a side branch, leaving main checked out, and return it."""
    _git(work, "checkout", "-q", "-b", branch)
    (work / filename).write_text(content, encoding="utf-8")
    _git(work, "add", filename)
    _git(work, "commit", "-q", "-m", f"add {filename}")
    head = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "main")
    return head


def _status(work: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(work), "status", "--porcelain"],
        capture_output=True,
        check=False,
        env=_ENV,
    ).stdout


def _index_bytes(work: Path) -> bytes:
    return (work / ".git" / "index").read_bytes()


def test_the_verb_lands_a_merge_of_the_base_and_the_reviewed_head(
    tmp_path: Path,
) -> None:
    """The landed commit has both parents, and the checkout and origin match it."""
    origin, work = _init_checkout(tmp_path)
    base = _git(work, "rev-parse", "HEAD")
    head = _reviewed_head(work, "reviewed-feature", "feature.txt", "feature\n")
    # An unrelated tracked file is modified in the working tree, as a run record
    # under docs/state would be; the landing must leave it untouched.
    (work / "tracked.txt").write_text("local edit\n", encoding="utf-8")

    report = merge.land_merge(
        work,
        head=head,
        message="Land the reviewed feature",
        body="the feature is reviewed and its suite passes",
    )

    landed = _git(work, "rev-parse", "HEAD")
    assert report["base"] == base
    assert report["head"] == head
    assert report["commit"] == landed
    assert report["remote_head"] == landed
    assert _git(work, "rev-parse", "HEAD^@").split() == [base, head]
    # The working tree carries the merge result: the reviewed file is present
    # and the unrelated local edit is still there, uncommitted.
    assert (work / "feature.txt").read_text(encoding="utf-8") == "feature\n"
    assert (work / "tracked.txt").read_text(encoding="utf-8") == "local edit\n"
    # The origin's main reads the same commit the checkout does.
    assert _git(origin, "rev-parse", "main") == landed


def test_the_cli_verb_lands_the_head_and_prints_the_commit(
    tmp_path: Path, monkeypatch
) -> None:
    """The registered verb drives the landing and answers with one JSON document."""
    origin, work = _init_checkout(tmp_path)
    head = _reviewed_head(work, "reviewed-feature", "feature.txt", "feature\n")
    monkeypatch.setattr(merge, "project_repository", lambda project: work)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "merge",
            "--project",
            "reckon",
            "--head",
            head,
            "--message",
            "Land the reviewed feature",
            "--body",
            "the feature is reviewed and its suite passes",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    landed = _git(work, "rev-parse", "HEAD")
    assert payload["commit"] == landed
    assert payload["remote_head"] == landed
    assert _git(origin, "rev-parse", "main") == landed


def test_a_conflicting_head_is_refused_and_the_index_is_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    """A conflict writes nothing: status, index bytes and MERGE_HEAD are unchanged."""
    if os.environ.get("RECKON_MERGE_CONFLICT_REFUSAL") == "removed":
        monkeypatch.setattr(merge, "refusing_conflicts", lambda conflicts: [])
    origin, work = _init_checkout(tmp_path)
    # A reviewed head and main both change tracked.txt, so the merge conflicts.
    _git(work, "checkout", "-q", "-b", "reviewed-conflict")
    (work / "tracked.txt").write_text("from the reviewed head\n", encoding="utf-8")
    _git(work, "commit", "-q", "-am", "the reviewed edit")
    head = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "main")
    (work / "tracked.txt").write_text("from main\n", encoding="utf-8")
    (work / "unrelated.txt").write_text("committed on main\n", encoding="utf-8")
    _git(work, "add", "unrelated.txt")
    _git(work, "commit", "-q", "-am", "the main edit")
    # An unrelated tracked file is modified in the working tree.
    (work / "unrelated.txt").write_text("local edit\n", encoding="utf-8")

    before_status, before_index = _status(work), _index_bytes(work)
    origin_before = _git(origin, "rev-parse", "main")
    try:
        merge.land_merge(
            work,
            head=head,
            message="Land the reviewed feature",
            body="the repository is reviewed and its suite passes",
        )
    except merge.MergeError as exc:
        refusal: merge.MergeError | None = exc
    else:
        refusal = None
    after_status, after_index = _status(work), _index_bytes(work)

    # The byte-identity assertion is deliberately first. With the conflict
    # refusal removed the landing writes the conflict markers into the checkout,
    # so this is the assertion the declared mutation fails; the refusal shape is
    # checked only once the checkout is known to be untouched.
    assert (after_status, after_index) == (before_status, before_index)
    assert refusal is not None, "the conflicting head was not refused"
    assert "tracked.txt" in str(refusal), str(refusal)
    assert not (work / ".git" / "MERGE_HEAD").exists()
    assert _git(origin, "rev-parse", "main") == origin_before
    assert origin_before != head


def test_a_held_index_lock_is_retried_and_the_head_still_lands(tmp_path: Path) -> None:
    """A first attempt that cannot take the index is retried until it can."""
    origin, work = _init_checkout(tmp_path)
    head = _reviewed_head(work, "reviewed-feature", "feature.txt", "feature\n")
    lock = work / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    released: list[int] = []

    def release(seconds: float) -> None:
        released.append(1)
        lock.unlink(missing_ok=True)

    report = merge.land_merge(
        work,
        head=head,
        message="Land the reviewed feature",
        body="the feature is reviewed and its suite passes",
        retry_seconds=0.0,
        sleep=release,
    )

    assert released, "the landing did not retry past the held lock"
    assert report["commit"] == _git(work, "rev-parse", "HEAD")
    assert _git(origin, "rev-parse", "main") == report["commit"]


def test_a_candidate_merge_commit_lands_exactly_as_built(tmp_path: Path) -> None:
    """--candidate lands the commit a population measured, not a rebuilt one."""
    origin, work = _init_checkout(tmp_path)
    base = _git(work, "rev-parse", "HEAD")
    head = _reviewed_head(work, "reviewed-feature", "feature.txt", "feature\n")
    candidate = merge.build_merge_commit(
        work,
        base,
        head,
        "Land the reviewed feature",
        "the feature is reviewed and its suite passes",
    )

    report = merge.land_merge(work, head=head, candidate=candidate)

    assert report["commit"] == candidate
    assert _git(work, "rev-parse", "HEAD") == candidate
    assert _git(origin, "rev-parse", "main") == candidate


def test_a_message_with_no_body_is_refused(tmp_path: Path) -> None:
    """A subject-only merge message is refused before anything is written."""
    _origin, work = _init_checkout(tmp_path)
    base = _git(work, "rev-parse", "HEAD")
    head = _reviewed_head(work, "reviewed-feature", "feature.txt", "feature\n")

    with pytest.raises(merge.MergeError):
        merge.land_merge(work, head=head, message="Land the reviewed feature", body="")

    assert _git(work, "rev-parse", "HEAD") == base


def test_an_attribution_trailer_is_refused(tmp_path: Path) -> None:
    """A message crediting a tool as co-author is refused before it is committed."""
    _origin, work = _init_checkout(tmp_path)
    head = _reviewed_head(work, "reviewed-feature", "feature.txt", "feature\n")

    with pytest.raises(merge.MergeError):
        merge.land_merge(
            work,
            head=head,
            message="Land the reviewed feature",
            body=(
                "reviewed and green\n\nCo-Authored-By: Some Tool <tool@example.invalid>"
            ),
        )


def test_a_push_failure_after_the_merge_reports_the_landed_commit(
    tmp_path: Path, monkeypatch
) -> None:
    """A push that fails after the fast-forward names the landed commit.

    Against the base tree the same failure is a bare error with no landed
    commit: the verb refuses as if nothing were written, which is the defect.
    """
    _origin, work = _init_checkout(tmp_path)
    base = _git(work, "rev-parse", "HEAD")
    head = _reviewed_head(work, "reviewed-feature", "feature.txt", "feature\n")
    # Point origin at a path that is not a repository, so the push fails after
    # the fast-forward has already moved the branch.
    _git(work, "remote", "set-url", "origin", str(tmp_path / "no-such-origin.git"))
    monkeypatch.setattr(merge, "project_repository", lambda project: work)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "merge",
            "--project",
            "reckon",
            "--head",
            head,
            "--message",
            "Land the reviewed feature",
            "--body",
            "the feature is reviewed and its suite passes",
        ],
    )

    try:
        payload = json.loads(result.output)
    except json.JSONDecodeError:
        raise AssertionError(
            "the verb reported a bare error with no landed commit: "
            f"exit={result.exit_code} output={result.output!r}"
        ) from None
    landed = _git(work, "rev-parse", "HEAD")
    # The merge landed even though the push failed, and the status is distinct
    # from a refusal that wrote nothing.
    assert result.exit_code == merge.PUSH_OUTSTANDING_EXIT
    assert result.exit_code != 1
    assert payload["commit"] == landed
    assert payload["base"] == base
    assert payload["head"] == head
    assert payload["pushed"] is False


def test_a_rerun_for_the_same_head_pushes_without_building_a_new_merge(
    tmp_path: Path,
) -> None:
    """A head already in HEAD goes straight to the push; no second merge is built."""
    origin, work = _init_checkout(tmp_path)
    head = _reviewed_head(work, "reviewed-feature", "feature.txt", "feature\n")
    first = merge.land_merge(
        work,
        head=head,
        message="Land the reviewed feature",
        body="the feature is reviewed and its suite passes",
    )
    landed = first["commit"]
    assert _git(work, "rev-parse", "HEAD") == landed
    commits_before = _git(work, "rev-list", "--count", "HEAD")

    rerun = merge.land_merge(work, head=head)

    assert rerun["commit"] == landed
    assert rerun["pushed"] is True
    # No second merge: HEAD is unchanged and no commit was added.
    assert _git(work, "rev-parse", "HEAD") == landed
    assert _git(work, "rev-list", "--count", "HEAD") == commits_before
    assert _git(origin, "rev-parse", "main") == landed


def test_a_refused_port_22_push_is_retried_on_the_port_443_route(
    tmp_path: Path, monkeypatch
) -> None:
    """With no GIT_SSH_COMMAND and a refused port 22, the push uses 443 and lands."""
    origin, work = _init_checkout(tmp_path)
    head = _reviewed_head(work, "reviewed-feature", "feature.txt", "feature\n")
    _git(work, "remote", "set-url", "origin", f"ssh://git@example.invalid{origin}")
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "ssh"
    stub.write_text(_SSH_STUB, encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(merge, "project_repository", lambda project: work)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "merge",
            "--project",
            "reckon",
            "--head",
            head,
            "--message",
            "Land the reviewed feature",
            "--body",
            "the feature is reviewed and its suite passes",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    landed = _git(work, "rev-parse", "HEAD")
    assert payload["pushed"] is True
    assert payload["commit"] == landed
    assert _git(origin, "rev-parse", "main") == landed
