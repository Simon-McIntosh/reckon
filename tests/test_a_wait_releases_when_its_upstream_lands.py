"""A section wait releases when its upstream lands, not when it is declared done.

A coordinator marks a section done in the plan and commits that immediately
before landing its run, so for a while the plan says done while the revision is
still off the registered checkout's HEAD. A request waiting on that section is
released only once the upstream's revision is an ancestor of HEAD -- the landed
test the queue already applies to a run ref, extended to cover a run that has
delivered and is not yet promoted. A discarded run will never land, so it holds
nothing, and a section completed with no run at all releases its waiter at
once.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import promotion, queue_admission, runs
from reckon.crew.node import TaskNode

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN = "plan-a"
UPSTREAM = "s1"
WAITER = "s2"
UPSTREAM_RUN = "r-upstream"
WAITER_RUN = "r-waiter"
DELIVERY_BRANCH = "run-branch"

CONFIG = {
    "default_backend": "clive",
    "local_backend": "clive",
    "backends": {
        "clive": {
            "launch": "cli",
            "command": "codex",
            "model": "local-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _plan_html(declarations: dict[str, str], waits: dict[str, list[str]]) -> str:
    quote = '"'
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{PLAN}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations{quote} content={quote}"
        f"{json.dumps(declarations).replace(quote, '&quot;')}{quote}>\n"
        f"<meta name={quote}plan-section-depends-on{quote} content={quote}"
        f"{json.dumps(waits).replace(quote, '&quot;')}{quote}>\n"
        f"<h2 id={quote}{UPSTREAM}{quote}>§1</h2>\n"
        f"<h2 id={quote}{WAITER}{quote}>§2</h2>\n"
    )


def _write_plan(repository: Path, declarations: dict[str, str]) -> None:
    path = repository / "docs" / "plans" / f"{PLAN}.html"
    path.write_text(
        _plan_html(declarations, {WAITER: [f"{PLAN}#{UPSTREAM}"]}),
        encoding="utf-8",
    )


def _rewrite_declarations(repository: Path, change: dict[str, str]) -> None:
    path = repository / "docs" / "plans" / f"{PLAN}.html"
    text = path.read_text(encoding="utf-8")
    match = re.search(r'name="plan-section-declarations" content="([^"]*)"', text)
    assert match is not None, "the fixture always writes a declarations meta"
    raw = match.group(1)
    declarations = json.loads(raw.replace("&quot;", '"').replace("&amp;", "&"))
    declarations.update(change)
    path.write_text(
        text.replace(raw, json.dumps(declarations).replace('"', "&quot;"), 1),
        encoding="utf-8",
    )
    # A landing commits the plan it ticks: a dispatch refuses to run against a
    # plan that differs from the base it reads.
    _git(repository, "add", "--", str(path.relative_to(repository)))
    _git(repository, "commit", "-q", "-m", "chore: declare the section done")


@pytest.fixture()
def queue_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repository = tmp_path / "checkout"
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    (repository / "docs" / "plans").mkdir(parents=True)
    _write_plan(
        repository,
        {UPSTREAM: "implementable", WAITER: "implementable"},
    )
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    # The mounts registry wins over the config home wherever both are consulted,
    # and a host that exports it would otherwise leave this fixture's checkout
    # unnamed: the guard would read the host repository's HEAD instead of the
    # one built here.
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(config_home / "mounts.json"))
    from reckon.crew.dispatch_sections import project_mount_repository

    resolved = project_mount_repository(PROJECT)
    assert resolved is not None, "the fixture registers its own checkout"
    assert Path(resolved).resolve() == repository.resolve(), (
        "the guard resolves the fixture's checkout, not the host's"
    )
    return repository


def _config_for():
    config = json.loads(json.dumps(CONFIG))

    def resolve(pointer):
        return config

    return resolve


def _no_launch(recorded: list):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(Path(str(prompt_path)).name)
        return os.getpid()

    return launch


def _phase_of(section: str) -> str:
    for pointer in runs.list_live(project=PROJECT):
        node = dict(pointer.get("node") or {})
        if str(node.get("section")) == section:
            return str(pointer.get("phase") or "")
    return ""


def _declare(repository: Path, section: str) -> None:
    _rewrite_declarations(repository, {section: "done"})


def _queue_waiter(repository: Path) -> dict:
    """Queue one request on ``s2``, which declares a wait on ``s1``."""
    return queue_admission.queue_requests(
        [
            TaskNode(
                id="n-waiter",
                goal=f"build {WAITER}",
                plan=PLAN,
                section=WAITER,
                done_when=f"pytest reports passed for {WAITER}",
                write_paths=[f"{WAITER}.txt"],
                manifest_path=str(repository / "manifest.md"),
                time_budget="20m",
                spec_level="exact",
            )
        ],
        project=PROJECT,
        session=SESSION,
        repo_root=repository,
        config=CONFIG,
        run_ids={"n-waiter": WAITER_RUN},
    )


def _deliver(repository: Path) -> str:
    """Record an upstream run that has delivered and whose head is off HEAD."""
    _git(repository, "checkout", "-q", "-b", DELIVERY_BRANCH)
    (repository / "upstream.txt").write_text("upstream\n", encoding="utf-8")
    _git(repository, "add", "--", "upstream.txt")
    _git(repository, "commit", "-q", "-m", "feat: the upstream section")
    delivered = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", "main")
    assert delivered != _git(repository, "rev-parse", "HEAD")

    manifest = runs.run_dir(UPSTREAM_RUN) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"status: complete\ncommits: {delivered}\nmanifest-version: 1\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(UPSTREAM_RUN),
        {
            "run_id": UPSTREAM_RUN,
            "project": PROJECT,
            "session": SESSION,
            "phase": "working",
            "repo": str(repository),
            "node": {"id": "n-upstream", "plan": PLAN, "section": UPSTREAM},
        },
    )
    return delivered


def _promote(delivered: str) -> None:
    """Write the ledger row a promotion records, without merging its head."""
    ledger.run_path(PROJECT, UPSTREAM_RUN).parent.mkdir(parents=True, exist_ok=True)
    ledger.write_run_file(
        PROJECT,
        {
            "run_id": UPSTREAM_RUN,
            "project": PROJECT,
            "plan": PLAN,
            "node": {"id": "n-upstream", "plan": PLAN, "section": UPSTREAM},
            "promoted_revision": delivered,
            "commits": [delivered],
        },
    )


def _release() -> dict:
    """Run the release a promotion of ``s1`` performs for its waiters."""
    return queue_admission._release_waiters([f"{PLAN}#{UPSTREAM}"], project=PROJECT)


def _admit(recorded: list) -> None:
    queue_admission.admit_once(config_for=_config_for(), launcher=_no_launch(recorded))


def test_a_delivered_unlanded_run_holds_the_wait_until_merged_into_head(
    queue_repo: Path,
) -> None:
    """The declaration alone does not release the waiter; the landing does."""
    stored = _queue_waiter(queue_repo)
    assert stored[WAITER_RUN]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE

    # The coordinator marks the section done, as a landing does, and the run has
    # delivered -- but its head is not in the registered checkout's HEAD.
    _declare(queue_repo, UPSTREAM)
    delivered = _deliver(queue_repo)

    launched: list[str] = []
    _admit(launched)
    assert launched == [], "a done declaration alone launches nothing"
    assert _phase_of(WAITER) == queue_admission.AWAITING_UPSTREAM_PHASE, (
        "the waiter stays waiting while its upstream code is off HEAD"
    )

    # Promotion records the revision, and the promotion's own release re-derives
    # the wait: the head is still unmerged, so the waiter keeps waiting.
    _promote(delivered)
    _release()
    assert _phase_of(WAITER) == queue_admission.AWAITING_UPSTREAM_PHASE, (
        "a promotion without the merge leaves the waiter waiting"
    )

    # Merging the delivered head into HEAD lands the section and releases it.
    _git(
        queue_repo,
        "merge",
        "--no-ff",
        "-q",
        "-m",
        "merge the upstream run",
        "run-branch",
    )
    assert _git(queue_repo, "merge-base", "--is-ancestor", delivered, "HEAD") == ""
    _release()
    assert _phase_of(WAITER) == queue_admission.QUEUED_PHASE, (
        "the waiter is released once the upstream's head is in HEAD"
    )


def test_a_discarded_delivered_run_releases_the_wait(queue_repo: Path) -> None:
    """A run that will never land is not a landing that is still to come."""
    _queue_waiter(queue_repo)
    _declare(queue_repo, UPSTREAM)
    _deliver(queue_repo)
    _release()
    assert _phase_of(WAITER) == queue_admission.AWAITING_UPSTREAM_PHASE

    promotion.discard(UPSTREAM_RUN)

    _release()
    assert _phase_of(WAITER) == queue_admission.QUEUED_PHASE, (
        "a discarded run no longer holds the section's waiters"
    )


def test_a_run_with_no_registered_checkout_holds_the_wait(
    queue_repo: Path,
) -> None:
    """A landing that cannot be read is unknown, not absent."""
    _queue_waiter(queue_repo)
    _declare(queue_repo, UPSTREAM)
    _deliver(queue_repo)

    mounts = Path(os.environ["RECKON_MOUNTS_PATH"])
    mounts.write_text("{}", encoding="utf-8")

    _release()
    assert _phase_of(WAITER) == queue_admission.AWAITING_UPSTREAM_PHASE, (
        "an unreadable landing holds the waiter rather than releasing it"
    )


def test_a_section_declared_done_with_no_run_releases_at_once(queue_repo: Path) -> None:
    """A section completed by hand has no run to wait for."""
    _queue_waiter(queue_repo)
    _declare(queue_repo, UPSTREAM)

    _release()
    assert _phase_of(WAITER) == queue_admission.QUEUED_PHASE
