"""The obligations hook speaks only for a session that is coordinating.

Every case here drives the hook as the harness does: a subprocess, hook JSON on
stdin, and the synthesised config home the payload's working directory resolves
through.  The two mapping rules are exercised separately, because a session
named verbatim after its crew session and a session whose follower was armed by
its own process resolve through different evidence -- and the second rule's
negative direction is what keeps one coordinator's list off another's screen.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew.obligations import obligations as obligations_view
from reckon.hooks.coordinator_obligations import (
    digest_path,
    duty_digest,
    format_checklist,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
AUTHORITY_LINE = "mirror these into your task list; reckon's list is the authority"
PROJECT = "hook-fixture"
SESSION = "coordinator-hook-fixture"
RUN_ID = "run-hook-fixture"
NODE_ID = "hook-fixture-node"
FIRST_RUN_ID = "run-hook-alpha"
FIRST_NODE_ID = "hook-alpha-node"
SECOND_RUN_ID = "run-hook-beta"
SECOND_NODE_ID = "hook-beta-node"
SCORING_RUN_ID = "run-hook-scoring"
SCORING_NODE_ID = "hook-scoring-node"
# The lane a run arrived on, and the lane this fixture's host declares as the
# local default. A composed review dispatch names the first; the injected
# command must follow the second.
FOREIGN_BACKEND = "codex"
LOCAL_BACKEND = "clive"
HELD_ITEMS = 25
# Held trees are staged with distinct retention ages, the first and oldest at
# HELD_OLDEST_AGE seconds and each later tree HELD_AGE_STEP seconds newer, so
# the collapsed line's oldest figure is one value the test can name.
HELD_OLDEST_AGE = 2_940
HELD_AGE_STEP = 60

# An age the hook renders from wall-clock distances, such as "2s" or "1h3m".
AGE = re.compile(r"\d+d\d+h|\d+h\d+m|\d+m\d+s|\d+s")


def _stamp(path: Path) -> tuple[int, int] | None:
    """A file's identity for an untouched check, or None when it is absent."""
    try:
        metadata = path.stat()
    except OSError:
        return None
    return (metadata.st_mtime_ns, metadata.st_size)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    return home


@pytest.fixture()
def repository(tmp_path: Path, config_home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed hook fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _live_run(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str,
    node_id: str,
    status: str,
    role: str,
) -> Path:
    """Record one live run whose manifest reports ``status`` under its own role."""
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {node_id}\nstatus: {status}\n", encoding="utf-8")
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "process_alive": False,
            "role": role,
            "manifest_path": str(manifest),
            "node": {
                "id": node_id,
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )
    return manifest


def _blocked_run(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str = RUN_ID,
    node_id: str = NODE_ID,
) -> Path:
    """Record one live run whose manifest holds its own turn open."""
    return _live_run(
        repository,
        tmp_path,
        run_id=run_id,
        node_id=node_id,
        status="blocked",
        role="implement",
    )


def _review_ready_run(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str,
    node_id: str,
) -> Path:
    """Record one completed review run, whose duty action names no run.

    A review run's deliverable is the review it wrote, so a complete manifest
    under the review role is promotable rather than scoring.
    """
    return _live_run(
        repository,
        tmp_path,
        run_id=run_id,
        node_id=node_id,
        status="complete",
        role="review",
    )


def _scoring_run(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str,
    node_id: str,
    backend: str,
) -> Path:
    """Record one completed run whose head no review has read yet.

    The manifest names the head commit, which is what makes the run scoring
    rather than promotable: completion with no review of that revision. The
    recorded backend is the lane the run was carried on, and the composed
    review dispatch for it names that lane.
    """
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    head = _git(repository, "rev-parse", "HEAD")
    manifest.write_text(
        f"node: {node_id}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
    )
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": head,
            "process_alive": False,
            "role": "implement",
            "backend": backend,
            "manifest_path": str(manifest),
            "node": {
                "id": node_id,
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )
    return manifest


@pytest.fixture()
def local_lane_config(config_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A host flight config declaring a local lane beside a metered backend.

    The hook reads the local lane default from the resolved flight config, so
    the fixture plants the host layer of the synthetic home -- never the
    operator's own file -- and names it through the environment override, so an
    operator variable cannot reach into the case.
    """
    path = config_home / "flight.yaml"
    path.write_text(
        "\n".join(
            [
                "version: 1",
                f"default_backend: {LOCAL_BACKEND}",
                f"local_backend: {LOCAL_BACKEND}",
                "backends:",
                f"  {LOCAL_BACKEND}:",
                "    launch: in-harness",
                "    sandbox: worktree-full",
                "    session_reuse: false",
                f"  {FOREIGN_BACKEND}:",
                "    launch: cli",
                f"    command: {FOREIGN_BACKEND}",
                "    model: fixture-model",
                "    sandbox: worktree-full",
                "    session_reuse: false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(path))
    return path


def _retained_worktrees(repository: Path, tmp_path: Path, count: int) -> None:
    """Promote runs whose retained worktrees are still registered.

    Every row names its own tree and its own retention instant, so the derived
    items are distinct runs while the remedy they are answered by is one shared
    command.
    """
    now = datetime.now(tz=UTC)
    trees = tmp_path / "managed-worktrees" / SESSION
    trees.mkdir(parents=True)
    rows: list[dict[str, object]] = []
    for index in range(count):
        node_id = f"held-node-{index:02d}"
        tree = trees / node_id
        _git(repository, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
        retained_at = (
            now - timedelta(seconds=HELD_OLDEST_AGE - index * HELD_AGE_STEP)
        ).isoformat()
        rows.append(
            {
                "run_id": f"held-run-{index:02d}",
                "plan": "fixture-plan",
                "node": node_id,
                "completed_at": retained_at,
                "worktree_retention": {
                    "worktree": str(tree),
                    "retained_at": retained_at,
                },
            }
        )
    ledger = repository / "docs" / "state" / PROJECT / "crew.json"
    ledger.write_text(
        json.dumps(
            {
                "updated": now.isoformat(),
                "project": PROJECT,
                "doc": "crew",
                "data": {"members": [], "runs": rows, "holds": [], "_version": 1},
            }
        ),
        encoding="utf-8",
    )


def _hook(
    mode: str,
    payload: dict[str, object],
    *,
    claude_pid: int | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    if claude_pid is not None:
        environment["CLAUDE_PID"] = str(claude_pid)
    return subprocess.run(
        [sys.executable, str(HOOK), "--hook", mode],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def _prompt_payload(directory: Path, session_id: str) -> dict[str, object]:
    return {
        "session_id": session_id,
        "cwd": str(directory),
        "hook_event_name": "UserPromptSubmit",
    }


def _stop_payload(
    directory: Path, session_id: str, **extra: object
) -> dict[str, object]:
    return {"session_id": session_id, "cwd": str(directory), **extra}


def _expected_checklist(*, unreconciled: int) -> str:
    header = (
        f"reckon obligations for session {SESSION} (project {PROJECT}): "
        "1 outstanding, oldest <age>"
    )
    blocked = (
        f"- [blocked] {RUN_ID} ({NODE_ID}, <age> old): "
        "read <manifest>; resolve the blocker before resuming the run"
    )
    closure = (
        f"unreconciled runs: {unreconciled}; work the list to empty "
        "before ending the turn."
    )
    return f"{header}\n{blocked}\n{closure}\n{AUTHORITY_LINE}"


def _normalised(checklist: str, *, manifest: Path) -> str:
    """Replace the two wall-clock ages so the rest of the text compares exactly."""
    return AGE.sub("<age>", checklist).replace(str(manifest), "<manifest>")


def _age_seconds(rendered: str) -> int:
    """The seconds an age the hook rendered stands for, read back."""
    return sum(
        int(value) * {"d": 86_400, "h": 3_600, "m": 60, "s": 1}[unit]
        for value, unit in re.findall(r"(\d+)([dhms])", rendered)
    )


def _registered_parent_pid() -> int:
    """The pid the follower registration names as the process that armed it."""
    for row in runs.list_followers(PROJECT):
        if row.get("session") == SESSION:
            record = row.get("follower") or {}
            return int(record["parent_pid"])
    raise AssertionError("the fixture registration is missing")


def test_prompt_mode_injects_the_checklist_for_a_coordinating_session(
    repository: Path, tmp_path: Path
) -> None:
    _blocked_run(repository, tmp_path)
    with runs.follower_claim(PROJECT, SESSION):
        completed = _hook("prompt", _prompt_payload(repository, "harness-session"))

    assert completed.returncode == 0
    assert completed.stderr == ""
    emitted = json.loads(completed.stdout)
    checklist = emitted["hookSpecificOutput"]["additionalContext"]
    assert checklist.strip(), "an obligations payload must inject a non-empty checklist"
    assert set(emitted["hookSpecificOutput"]) == {
        "hookEventName",
        "additionalContext",
    }
    assert emitted["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert _normalised(checklist, manifest=tmp_path / "manifests" / f"{RUN_ID}.md") == (
        _expected_checklist(unreconciled=1)
    )
    assert checklist.endswith(AUTHORITY_LINE)


def test_stop_mode_blocks_while_obligations_remain_and_your_own_continuation_stops_it(
    repository: Path, tmp_path: Path
) -> None:
    _blocked_run(repository, tmp_path)
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    with runs.follower_claim(PROJECT, SESSION):
        first = _hook("stop", _stop_payload(repository, "harness-session"))
        continued = _hook(
            "stop",
            _stop_payload(repository, "harness-session", stop_hook_active=True),
        )

    assert first.returncode == 0
    decision = json.loads(first.stdout)
    assert set(decision) == {"decision", "reason"}
    assert decision["decision"] == "block"
    assert _normalised(decision["reason"], manifest=manifest) == (
        _expected_checklist(unreconciled=1)
    )

    assert continued.returncode == 0
    assert continued.stdout == ""
    assert continued.stderr == ""


def test_a_session_with_no_obligations_is_silent_in_both_modes(
    repository: Path,
) -> None:
    with runs.follower_claim(PROJECT, SESSION):
        prompting = _hook("prompt", _prompt_payload(repository, "harness-session"))
        stopping = _hook("stop", _stop_payload(repository, "harness-session"))

    assert prompting.returncode == 0
    assert prompting.stdout == ""
    assert stopping.returncode == 0
    assert stopping.stdout == ""


def test_a_directory_outside_the_mounts_is_silent(
    tmp_path: Path, config_home: Path
) -> None:
    outside = tmp_path / "not-a-checkout"
    outside.mkdir()

    prompting = _hook("prompt", _prompt_payload(outside, "harness-session"))
    stopping = _hook("stop", _stop_payload(outside, "harness-session"))

    assert prompting.returncode == 0
    assert prompting.stdout == ""
    assert stopping.returncode == 0
    assert stopping.stdout == ""


def test_a_follower_armed_by_this_session_names_it_and_another_sessions_does_not(
    repository: Path, tmp_path: Path
) -> None:
    _blocked_run(repository, tmp_path)
    with runs.follower_claim(PROJECT, SESSION):
        owner = _registered_parent_pid()
        ours = _hook(
            "prompt",
            _prompt_payload(repository, "harness-not-the-crew-session-name"),
            claude_pid=owner,
        )
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            theirs = _hook(
                "prompt",
                _prompt_payload(repository, "harness-not-the-crew-session-name"),
                claude_pid=unrelated.pid,
            )
        finally:
            unrelated.kill()
            unrelated.wait()

    assert owner > 1
    assert ours.returncode == 0
    assert json.loads(ours.stdout)["hookSpecificOutput"]["additionalContext"].endswith(
        AUTHORITY_LINE
    )
    assert theirs.returncode == 0
    assert theirs.stdout == ""


def test_the_real_settings_home_is_never_read_or_written(
    repository: Path, tmp_path: Path
) -> None:
    watched = [
        Path.home() / ".config" / "reckon",
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
    ]
    before = {path: _stamp(path) for path in watched}

    _blocked_run(repository, tmp_path)
    with runs.follower_claim(PROJECT, SESSION):
        _hook("prompt", _prompt_payload(repository, "harness-session"))
        _hook("stop", _stop_payload(repository, "harness-session"))

    after = {path: _stamp(path) for path in watched}
    assert after == before


def test_no_follower_leaves_the_hook_silent(repository: Path, tmp_path: Path) -> None:
    """A session that armed no follower is not coordinating, so both modes are quiet.

    The recorded run is live and blocked, so the silence is the missing
    registration rather than an empty fleet: with a follower the same payload
    blocks in stop mode.
    """
    _blocked_run(repository, tmp_path)
    prompting = _hook("prompt", _prompt_payload(repository, "harness-session"))
    stopping = _hook("stop", _stop_payload(repository, "harness-session"))

    assert prompting.returncode == 0
    assert prompting.stdout == ""
    assert prompting.stderr == ""
    assert stopping.returncode == 0
    assert stopping.stdout == ""
    assert stopping.stderr == ""


def test_items_sharing_a_command_collapse_to_one_counted_line(
    repository: Path, tmp_path: Path
) -> None:
    """A fleet's worth of identical remedies takes one line, and one only.

    Twenty-five promoted runs still hold their worktrees and are all answered
    by the same gc command; two blocked runs are answered by a command naming
    each one's own manifest. The collapsed line carries the count, the oldest
    age and the command once; each blocked item keeps its own line and its run
    id; and the checklist stays inside the size a coordinator reads at the open
    of a turn. The retention ages are staggered newest-last, so the collapsed
    line's age is compared against the oldest member by value.
    """
    _retained_worktrees(repository, tmp_path, HELD_ITEMS)
    first_manifest = _blocked_run(
        repository, tmp_path, run_id=FIRST_RUN_ID, node_id=FIRST_NODE_ID
    )
    second_manifest = _blocked_run(
        repository, tmp_path, run_id=SECOND_RUN_ID, node_id=SECOND_NODE_ID
    )

    with runs.follower_claim(PROJECT, SESSION):
        completed = _hook("prompt", _prompt_payload(repository, "harness-session"))

    assert completed.returncode == 0
    assert completed.stderr == ""
    checklist = json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"]
    lines = checklist.splitlines()

    collapsed = [line for line in lines if line.startswith("- [worktree-held]")]
    assert len(collapsed) == 1
    assert AGE.sub("<age>", collapsed[0]) == (
        f"- [worktree-held] {HELD_ITEMS} items (oldest <age> old): "
        f"reckon crew gc --repo {repository} --project {PROJECT} --apply"
    )

    blocked = [line for line in lines if line.startswith("- [blocked]")]
    assert sorted(AGE.sub("<age>", line) for line in blocked) == sorted(
        [
            (
                f"- [blocked] {FIRST_RUN_ID} ({FIRST_NODE_ID}, <age> old): "
                f"read {first_manifest}; resolve the blocker before resuming the run"
            ),
            (
                f"- [blocked] {SECOND_RUN_ID} ({SECOND_NODE_ID}, <age> old): "
                f"read {second_manifest}; resolve the blocker before resuming the run"
            ),
        ]
    )

    assert lines[0].startswith(
        f"reckon obligations for session {SESSION} (project {PROJECT}): "
        f"{HELD_ITEMS + 2} outstanding, oldest "
    )
    rendered_oldest = _age_seconds(
        re.search(r"oldest (\S+) old", collapsed[0]).group(1)
    )
    newest_member_age = HELD_OLDEST_AGE - HELD_AGE_STEP * (HELD_ITEMS - 1)
    assert rendered_oldest >= HELD_OLDEST_AGE
    assert rendered_oldest > newest_member_age
    assert checklist.endswith(AUTHORITY_LINE)
    assert len(lines) <= 8
    assert len(checklist) < 2_000


def test_two_review_ready_items_sharing_a_run_less_command_keep_their_own_lines(
    repository: Path, tmp_path: Path
) -> None:
    """Two completed review runs share one promotion sentence and keep two lines.

    A review run's duty action names no run, so the two duties carry identical
    command text while being two different reviews to read. Collapsing them
    would hide one run's review behind a count, so each keeps its own line
    carrying its own run id.
    """
    _review_ready_run(repository, tmp_path, run_id=FIRST_RUN_ID, node_id=FIRST_NODE_ID)
    _review_ready_run(
        repository, tmp_path, run_id=SECOND_RUN_ID, node_id=SECOND_NODE_ID
    )

    with runs.follower_claim(PROJECT, SESSION):
        completed = _hook("prompt", _prompt_payload(repository, "harness-session"))

    assert completed.returncode == 0
    assert completed.stderr == ""
    checklist = json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"]
    lines = checklist.splitlines()

    review = [line for line in lines if line.startswith("- [review-ready]")]
    assert len(review) == 2, checklist
    commands = {line.split("): ", 1)[1] for line in review}
    assert len(commands) == 1, (
        "the two duties must share one command for this case to bite"
    )
    shared = commands.pop()
    assert FIRST_RUN_ID not in shared and SECOND_RUN_ID not in shared, (
        "the shared command must name no run, so only the renderer separates the lines"
    )
    assert sorted(line.split()[2] for line in review) == sorted(
        [FIRST_RUN_ID, SECOND_RUN_ID]
    )
    assert lines[0].startswith(
        f"reckon obligations for session {SESSION} (project {PROJECT}): "
        "2 outstanding, oldest "
    )
    assert lines[-1] == AUTHORITY_LINE


def test_the_collapsed_line_reports_the_oldest_age_of_its_members() -> None:
    """The counted line carries the oldest member's age by value, not the newest.

    The two fixture ages render differently -- 3_000 seconds is ``50m0s`` and
    600 is ``10m0s`` -- so a line matching the oldest age can only have read
    the oldest member.
    """
    command = "reckon crew gc --repo /tmp/held-repo --project hook-fixture --apply"
    payload = {
        "project": PROJECT,
        "session": SESSION,
        "obligations": [
            {
                "kind": "worktree-held",
                "run_id": "held-oldest",
                "node": "held-oldest-node",
                "age_seconds": 3_000,
                "next_command": command,
            },
            {
                "kind": "worktree-held",
                "run_id": "held-newest",
                "node": "held-newest-node",
                "age_seconds": 600,
                "next_command": command,
            },
        ],
        "summary": {"count": 2, "oldest_age_seconds": 3_000, "unreconciled_runs": 0},
    }

    collapsed = [
        line
        for line in format_checklist(payload).splitlines()
        if line.startswith("- [worktree-held]")
    ]

    assert collapsed == [f"- [worktree-held] 2 items (oldest 50m0s old): {command}"]


def test_prompt_mode_speaks_on_a_changed_duty_set_and_is_quiet_on_a_repeated_one(
    repository: Path, tmp_path: Path, config_home: Path
) -> None:
    """The third drive is what makes the second drive's silence mean anything.

    A silent second drive on its own proves nothing, because a session the hook
    cannot resolve is silent too. The same fixture gains one more duty before
    the third drive, and the hook speaks again over the same registration -- so
    the second drive was quiet because the set had not moved, not because
    nothing was there to say.
    """
    _blocked_run(repository, tmp_path)
    digest_file = digest_path(PROJECT, SESSION)

    with runs.follower_claim(PROJECT, SESSION):
        first = _hook("prompt", _prompt_payload(repository, "harness-session"))
        assert digest_file.is_file(), "the prompt drive must record its digest"
        first_digest = digest_file.read_text(encoding="utf-8").strip()
        first_stamp = _stamp(digest_file)
        repeated = _hook("prompt", _prompt_payload(repository, "harness-session"))
        repeated_stamp = _stamp(digest_file)
        _blocked_run(repository, tmp_path, run_id=SECOND_RUN_ID, node_id=SECOND_NODE_ID)
        changed = _hook("prompt", _prompt_payload(repository, "harness-session"))

    assert first.returncode == 0
    assert first.stderr == ""
    injected = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
    assert RUN_ID in injected
    assert first_digest, "the digest is the record of what was injected"
    # Where the record lives is part of the contract: under the config home,
    # beside the registration of the session it describes.
    assert digest_file.parent == runs.follower_lock_path(PROJECT, SESSION).parent
    assert config_home in digest_file.parents

    assert repeated.returncode == 0
    assert repeated.stdout == ""
    assert repeated.stderr == ""
    assert repeated_stamp == first_stamp, (
        "a repeated duty set must not rewrite the record it matched"
    )

    assert changed.returncode == 0
    assert changed.stderr == ""
    checklist = json.loads(changed.stdout)["hookSpecificOutput"]["additionalContext"]
    assert RUN_ID in checklist
    assert SECOND_RUN_ID in checklist
    assert digest_file.read_text(encoding="utf-8").strip() != first_digest


def test_an_age_that_moved_alone_does_not_re_inject(
    repository: Path, tmp_path: Path, config_home: Path
) -> None:
    """A duty whose age grew is the same duty, so it is not read out twice.

    The age is shown to have moved rather than assumed to have: the derivation
    the hook itself reads is asked again after the manifest is backdated, and
    it reports the older figure while the hook stays quiet.
    """
    manifest = _blocked_run(repository, tmp_path)

    with runs.follower_claim(PROJECT, SESSION):
        first = _hook("prompt", _prompt_payload(repository, "harness-session"))
        aged = time.time() - 900
        os.utime(manifest, (aged, aged))
        moved = obligations_view(PROJECT, SESSION)
        again = _hook("prompt", _prompt_payload(repository, "harness-session"))

    assert first.stdout, "the first drive must inject for this case to bite"
    assert moved["obligations"][0]["age_seconds"] >= 900
    assert again.returncode == 0
    assert again.stdout == ""
    assert again.stderr == ""


def test_the_digest_does_not_silence_the_stop_mode(
    repository: Path, tmp_path: Path, config_home: Path
) -> None:
    """A stop is a verdict on the turn, not a repeat of a checklist.

    The prompt drive records the digest for the very duty set the stop that
    follows it reads. The block must still fire: a stop allowed over
    outstanding work is the forgetting this hook exists to prevent.
    """
    _blocked_run(repository, tmp_path)

    with runs.follower_claim(PROJECT, SESSION):
        prompting = _hook("prompt", _prompt_payload(repository, "harness-session"))
        stopping = _hook("stop", _stop_payload(repository, "harness-session"))

    assert prompting.stdout, "the prompt drive must inject for this case to bite"
    assert stopping.returncode == 0
    assert json.loads(stopping.stdout)["decision"] == "block"


def test_a_duty_that_empties_and_returns_is_injected_again(
    repository: Path, tmp_path: Path, config_home: Path
) -> None:
    """A returned duty must speak, so an emptied list must clear the record.

    Three drives over one registration and one session: the first injects a
    blocked duty and records its digest, the second is silent because the run's
    pointer is gone and the session owes nothing, and the third must inject the
    same duty again. The reappearance is the same duty in the digest's own
    terms -- the digest of the restored set is compared against the recorded
    one before the third drive -- because the defect is precisely that a
    returning duty matches the record the emptied list left behind and is
    never spoken again.
    """
    _blocked_run(repository, tmp_path)
    digest_file = digest_path(PROJECT, SESSION)
    pointer = runs.pointer_path(RUN_ID)

    with runs.follower_claim(PROJECT, SESSION):
        first = _hook("prompt", _prompt_payload(repository, "harness-session"))
        assert first.stdout, "the first drive must inject for this case to bite"
        recorded = digest_file.read_text(encoding="utf-8").strip()
        assert recorded == duty_digest(
            obligations_view(PROJECT, SESSION)["obligations"]
        ), "the record must be the digest of the set that was injected"

        pointer.unlink()
        assert obligations_view(PROJECT, SESSION)["obligations"] == [], (
            "the removed run must leave the session owing nothing, or the "
            "second drive's silence proves nothing"
        )
        emptied = _hook("prompt", _prompt_payload(repository, "harness-session"))

        assert emptied.returncode == 0
        assert emptied.stdout == ""
        assert emptied.stderr == ""

        _blocked_run(repository, tmp_path)
        assert (
            duty_digest(obligations_view(PROJECT, SESSION)["obligations"]) == recorded
        ), (
            "the reappearing duty must be digest-identical to the injected one, "
            "or this case cannot show a stale record swallowing it"
        )
        returned = _hook("prompt", _prompt_payload(repository, "harness-session"))

    assert returned.returncode == 0
    assert returned.stderr == ""
    assert returned.stdout, "the returned duty must be injected again"
    checklist = json.loads(returned.stdout)["hookSpecificOutput"]["additionalContext"]
    assert RUN_ID in checklist


def test_an_emptied_list_seen_first_by_the_stop_mode_still_frees_the_injection(
    repository: Path, tmp_path: Path, config_home: Path
) -> None:
    """The event that sees the list empty is whichever fires first, so both clear.

    The prompt is not guaranteed to be the mode that observes the emptying: a
    duty can be drained, the turn ended over an empty list, and the same duty
    return before the next prompt. The record is cleared on the empty list in
    either mode, so the stop drive's silence over nothing does not become the
    prompt drive's silence over something.
    """
    _blocked_run(repository, tmp_path)
    digest_file = digest_path(PROJECT, SESSION)
    pointer = runs.pointer_path(RUN_ID)

    with runs.follower_claim(PROJECT, SESSION):
        first = _hook("prompt", _prompt_payload(repository, "harness-session"))
        assert first.stdout, "the first drive must inject for this case to bite"

        pointer.unlink()
        stopping = _hook("stop", _stop_payload(repository, "harness-session"))
        assert stopping.returncode == 0
        assert stopping.stdout == "", "a stop over no duties is not a verdict"
        assert digest_file.read_text(encoding="utf-8").strip() == "", (
            "the stop that sees the emptied list must clear the record too"
        )

        _blocked_run(repository, tmp_path)
        returned = _hook("prompt", _prompt_payload(repository, "harness-session"))

    assert returned.returncode == 0
    assert returned.stderr == ""
    assert returned.stdout, "the returned duty must be injected again"
    checklist = json.loads(returned.stdout)["hookSpecificOutput"]["additionalContext"]
    assert RUN_ID in checklist


def test_a_review_command_in_the_checklist_follows_the_local_lane(
    repository: Path,
    tmp_path: Path,
    config_home: Path,
    local_lane_config: Path,
) -> None:
    """A dispatch printed for a coordinator names the local lane, not the run's.

    The composed command the obligations reader derives names the backend the
    run was carried on; that composition is asserted first, so a rewrite cannot
    pass by having nothing to rewrite. What the hook injects is a command a
    coordinator may type, so it follows the host's declared local lane and the
    rest of the command is left byte for byte as composed.
    """
    _scoring_run(
        repository,
        tmp_path,
        run_id=SCORING_RUN_ID,
        node_id=SCORING_NODE_ID,
        backend=FOREIGN_BACKEND,
    )
    composed = recovery.classify_pointer(runs.read_pointer(SCORING_RUN_ID))[
        "next_action"
    ]
    assert f"--backend {FOREIGN_BACKEND}" in composed, (
        "the composed review dispatch must name the run's own backend for this "
        "case to bite"
    )

    with runs.follower_claim(PROJECT, SESSION):
        completed = _hook("prompt", _prompt_payload(repository, "harness-session"))

    assert completed.returncode == 0
    assert completed.stderr == ""
    checklist = json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"]
    review = [
        line for line in checklist.splitlines() if line.startswith("- [review-missing]")
    ]
    assert len(review) == 1, checklist
    injected = review[0].split("): ", 1)[1]
    assert FOREIGN_BACKEND not in injected
    assert injected == composed.replace(f" --backend {FOREIGN_BACKEND}", " --local")
