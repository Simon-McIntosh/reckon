"""A citation that fails to resolve refuses the promotion rather than being dropped.

A promotion chooses the revision it asserts by comparing the commits the run
cites and taking the one every other citation is an ancestor of. That choice is
only a fact about the run's work while every citation is in it, so a citation
the resolver could not answer for may not be left out: dropping it lets the
remaining citations agree on an earlier tip, and the promotion records that tip
as the revision the run reached.

Two failures shared one empty answer and therefore one treatment. A revision
that names no commit and a resolver that timed out or could not start git both
answered nothing, so both looked like a citation with nothing to contribute.
They are told apart here: the resolver's strict mode raises for the instrument
failing, and the caller that chooses the revision then refuses the promotion
naming the citation and the tree it was read in.

These assertions cover the choice of revision only. The repair of the land that
merged a mis-chosen revision is a different node.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import recovery_review_subject as review_subject
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import real_store_home, real_store_is_not_a_fixture_target
from tests.real_home_traces import traces_naming

PROJECT = "citation"
# The run must change runtime source, so a review of it is required at all: a
# run touching only text resolves to the tier that owes no review, skipping the
# review gate this selection feeds.
FILE = "reckon/region.py"


def _git(tree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    return completed.stdout.strip()


def _commit(tree: Path, text: str) -> str:
    target = tree / FILE
    target.write_text(f"{text}\n{target.read_text(encoding='utf-8')}", encoding="utf-8")
    _git(tree, "add", FILE)
    _git(tree, "commit", "-q", "-m", "test: " + text)
    return _git(tree, "rev-parse", "HEAD")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / FILE).write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    _git(root, "add", FILE)
    _git(root, "commit", "-q", "-m", "chore: seed")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _run_tree(repository: Path, tmp_path: Path, run_id: str) -> Path:
    path = tmp_path / f"{run_id}-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


def _linear_run(
    repository: Path, tmp_path: Path, run_id: str
) -> tuple[Path, str, list[str]]:
    """A run worktree on a linear chain of four commits over the base."""
    base = _git(repository, "rev-parse", "HEAD")
    tree = _run_tree(repository, tmp_path, run_id)
    chain = [_commit(tree, f"step {number}") for number in range(1, 5)]
    return tree, base, chain


def _manifest(tmp_path: Path, run_id: str, commits: list[str]) -> Path:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    log_name = f"{run_id}-focus.log"
    (manifest.parent / log_name).write_text(
        "focused check passed\nEXIT=0\n", encoding="utf-8"
    )
    manifest.write_text(
        f"node: {PROJECT}-node\n"
        "status: complete\n"
        f"commits: [{', '.join(commits)}]\n"
        f"changed_paths: [{FILE}]\n"
        "tests: pytest -q tests/test_a_commit_that_fails_to_resolve_refuses_the_promotion.py\n"
        f"test_logs: [{log_name}]\n",
        encoding="utf-8",
    )
    return manifest


def _pointer(
    repository: Path, tree: Path, run_id: str, base: str, manifest: Path
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(tree),
            "base_sha": base,
            "process_alive": False,
            "launch": "in-harness",
            "role": "implement",
            "backend": "local",
            "created_at": "2026-01-01T00:00:01Z",
            "manifest_path": str(manifest),
            "node": {
                "id": f"{PROJECT}-node",
                "plan": "fixture",
                "section": "citation",
                "time_budget": "25m",
                "write_paths": [FILE],
            },
        },
    )


def _store_review(run_id: str, *, base: str, head: str) -> Path:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
        }
    )
    record["reviewed_base_sha"] = base
    record["reviewed_head_sha"] = head
    return review_module.store_review(record)


def _run(
    repository: Path, tmp_path: Path, run_id: str, commits: list[str] | None = None
) -> tuple[Path, str, list[str]]:
    """A promotable run whose record cites the commits its tree carries."""
    tree, base, chain = _linear_run(repository, tmp_path, run_id)
    _pointer(repository, tree, run_id, base, _manifest(tmp_path, run_id, chain))
    _store_review(run_id, base=base, head=chain[-1])
    return tree, base, list(commits if commits is not None else chain)


def _choosing_revision() -> bool:
    """Whether the call being made is the choice of the revision, or a check.

    The landing check and the choice both read the same citations through the
    same resolver, so a fault keyed on a count would fire in whichever ran
    second and the case would prove the wrong thing. The choosing frame is the
    one fact that separates them, and it is what these cases are about: the
    citation resolved when the landing check read it and did not when the
    revision was chosen.
    """
    frame = sys._getframe(1)
    while frame is not None:
        if frame.f_code.co_name == "_descendant_commit":
            return True
        frame = frame.f_back
    return False


class _ResolverFault:
    """The module's subprocess, failing one citation while the revision is chosen.

    Every other git invocation, the landing check's included, reaches the real
    process, so a case built on this fixture states exactly the disagreement the
    repair is about.
    """

    TimeoutExpired = subprocess.TimeoutExpired
    SubprocessError = subprocess.SubprocessError

    def __init__(self, target: str, *, mode: str) -> None:
        self.target = target
        self.mode = mode
        self.seen = 0

    def run(self, argv, *args, **kwargs):
        wanted = f"{self.target}^{{commit}}"
        if isinstance(argv, (list, tuple)) and wanted in argv:
            self.seen += 1
            if _choosing_revision():
                if self.mode == "timeout":
                    raise subprocess.TimeoutExpired(argv, 10)
                return subprocess.CompletedProcess(argv, 1, "", "no such commit")
        return subprocess.run(argv, *args, check=kwargs.pop("check", False), **kwargs)


def _promote(repository: Path, run_id: str, commits: list[str]) -> dict:
    return crew.complete(run_id, gate="passed", commits=commits, root=repository)


# (1) Every citation resolves: the run's tip is promoted.


def test_the_fourth_commit_is_promoted_when_every_citation_resolves(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-resolves"
    _tree, _base, chain = _run(repository, tmp_path, run_id)

    result = _promote(repository, run_id, chain)

    assert result["record"]["promoted_revision"] == chain[-1]


# (2) The resolver times out for the fourth commit: refused, as an instrument.


def test_a_timeout_on_the_fourth_commit_refuses_and_names_the_instrument(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-timeout"
    _tree, _base, chain = _run(repository, tmp_path, run_id)
    fault = _ResolverFault(chain[-1], mode="timeout")
    monkeypatch.setattr(review_subject, "subprocess", fault)

    with pytest.raises(crew.CrewError) as refusal:
        _promote(repository, run_id, chain)

    message = str(refusal.value)
    assert chain[-1] in message or chain[-1][:12] in message
    assert "timed out" in message
    assert "instrument fault" in message
    # The landing check read this citation first and answered; the choice is
    # where the resolver stopped answering, which is the disagreement asserted.
    assert fault.seen >= 2
    # Nothing landed: no promoted revision is recorded.
    assert ledger.runs(PROJECT, root=repository) == []


def test_a_timeout_refusal_is_worded_differently_from_an_unresolvable_citation(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unresolvable_run = "r-unresolvable-word"
    _tree, _base, chain = _run(repository, tmp_path, unresolvable_run)
    monkeypatch.setattr(
        review_subject,
        "subprocess",
        _ResolverFault(chain[-1], mode="unresolvable"),
    )
    with pytest.raises(crew.CrewError) as unresolvable:
        _promote(repository, unresolvable_run, chain)

    timeout_run = "r-timeout-word"
    slow_tree, slow_base, slow_chain = _linear_run(repository, tmp_path, timeout_run)
    _pointer(
        repository,
        slow_tree,
        timeout_run,
        slow_base,
        _manifest(tmp_path, timeout_run, slow_chain),
    )
    _store_review(timeout_run, base=slow_base, head=slow_chain[-1])
    monkeypatch.setattr(
        review_subject, "subprocess", _ResolverFault(slow_chain[-1], mode="timeout")
    )
    with pytest.raises(crew.CrewError) as timeout:
        _promote(repository, timeout_run, slow_chain)

    assert str(unresolvable.value) != str(timeout.value)
    assert "names no commit" in str(unresolvable.value)
    assert "timed out" not in str(unresolvable.value)


# (3) The strict resolver still reports a revision that names no commit as such.


def test_a_revision_naming_no_commit_is_unresolvable_not_an_instrument_fault(
    repository: Path, tmp_path: Path
) -> None:
    tree, _base, _chain = _linear_run(repository, tmp_path, "r-names-nothing")
    missing = "0" * 40

    assert review_subject._resolve_commit(tree, missing) == ""
    assert review_subject._resolve_commit(tree, missing, strict=True) == ""
    assert review_subject._resolve_commit(tree, "HEAD", strict=True) != ""


# (4) A citation that stopped resolving at the choice is refused, not left out.


def test_a_citation_that_stops_resolving_at_the_choice_refuses(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-stops-resolving"
    _tree, _base, chain = _run(repository, tmp_path, run_id)
    fault = _ResolverFault(chain[-1], mode="unresolvable")
    monkeypatch.setattr(review_subject, "subprocess", fault)

    with pytest.raises(crew.CrewError) as refusal:
        _promote(repository, run_id, chain)

    message = str(refusal.value)
    # The omission would have left the first three, which agree on the third.
    assert chain[-1][:12] in message
    assert "names no commit" in message
    assert str(_tree) in message
    assert "no single descendant" not in message
    assert fault.seen >= 2

    assert ledger.runs(PROJECT, root=repository) == []


# (5) No branch returns a citation that failed to resolve.


def test_a_single_citation_that_fails_at_the_choice_is_refused(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-single-citation"
    _tree, _base, chain = _run(repository, tmp_path, run_id)
    monkeypatch.setattr(
        review_subject, "subprocess", _ResolverFault(chain[-1], mode="unresolvable")
    )

    with pytest.raises(crew.CrewError) as refusal:
        _promote(repository, run_id, [chain[-1]])

    assert chain[-1][:12] in str(refusal.value)
    assert ledger.runs(PROJECT, root=repository) == []


def test_a_citation_list_none_of_which_resolves_is_refused(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-none-resolve"
    _tree, _base, chain = _run(repository, tmp_path, run_id)
    monkeypatch.setattr(
        review_subject,
        "subprocess",
        _FailingEveryResolution(first=chain[2], second=chain[3]),
    )

    with pytest.raises(crew.CrewError) as refusal:
        _promote(repository, run_id, [chain[2], chain[3]])

    assert chain[2][:12] in str(refusal.value) or chain[3][:12] in str(refusal.value)
    assert ledger.runs(PROJECT, root=repository) == []


class _FailingEveryResolution:
    """No citation resolves while the revision is chosen, though each did before."""

    TimeoutExpired = subprocess.TimeoutExpired
    SubprocessError = subprocess.SubprocessError

    def __init__(self, *, first: str, second: str) -> None:
        self.targets = {f"{first}^{{commit}}": 0, f"{second}^{{commit}}": 0}

    def run(self, argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and _choosing_revision():
            for wanted in self.targets:
                if wanted in argv:
                    self.targets[wanted] += 1
                    return subprocess.CompletedProcess(argv, 1, "", "no such commit")
        return subprocess.run(argv, *args, check=kwargs.pop("check", False), **kwargs)


# (6) The fixture's own isolation, asserted rather than assumed.

# Every run id these cases mint. A leaked pointer or registration lands under
# the shared home naming one of them, so a peer's own entry is never a hit and
# a write that escaped isolation always is.
NEEDLE_RUN_IDS = (
    "r-resolves",
    "r-timeout",
    "r-unresolvable-word",
    "r-names-nothing",
    "r-stops-resolving",
    "r-single-citation",
    "r-none-resolve",
)


@pytest.fixture(autouse=True)
def real_crew_home_untouched() -> Iterator[None]:
    """Search the real crew home for these run ids before and after every case.

    The temporary home is what the cases write; this is the receipt for that,
    taken on the operator's own home so a case that escaped isolation fails
    here rather than colliding with a live fleet.
    """
    home = real_store_home()
    before = traces_naming(home, ("crew/live", "crew/watch"), NEEDLE_RUN_IDS)
    yield
    assert traces_naming(home, ("crew/live", "crew/watch"), NEEDLE_RUN_IDS) == before


def test_the_real_crew_home_is_untouched(home: Path) -> None:
    real_store_is_not_a_fixture_target(home)
