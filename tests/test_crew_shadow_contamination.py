"""Shadow streams that read their primary's answer are excluded from slices.

A shadow dispatched after its primary merged shares the object store with the
integration branch, so its stream can read the landed commit or the primary's
changed files from the main checkout without leaving a footprint. The ledger
row cannot say which shadow was void — only the stream can — so promotion
scans it and marks the row, and the mark excludes it from every calibration
slice.

The two fixture streams are the recorded runs this detection was written
against, reproduced from their own stream files so the test stays hermetic:
one known-contaminated arm whose stream ran ``git show`` of its primary's
landed sha for source and for tests, and one known-clean arm whose stream
entered the main checkout environment but read neither the sha nor the
changed files. The distinction is invisible in the ledger row, so asserting
both directions is what stops a false positive discarding sound evidence.
"""

from __future__ import annotations

import json
import subprocess

from reckon import calibration, ledger
from reckon.crew import promotion

MAIN_CHECKOUT = "/home/ITER/mcintos/Code/reckon"

# The known-contaminated arm and its primary, reconstructed from the recorded
# stream of a real calibration shadow: the stream ran ``git show`` of the
# primary's landed sha for the source files and then for the tests.
CONTAMINATED_PRIMARY_COMMIT = "7572d83c8f567b4751ab2fdafe9cbe0cef6eb0ab"
CONTAMINATED_PRIMARY_PATHS = (
    "reckon/calibration.py",
    "reckon/capabilities.py",
    "reckon/crew/routing.py",
    "tests/test_calibration.py",
    "tests/test_capabilities.py",
    "tests/test_routing_surface.py",
)
CONTAMINATED_WORKTREE = (
    "/home/ITER/mcintos/Code/.reckon-worktrees/reckon-c8f839407e49/"
    "shadow-r-20260909T061216201721-clw-calibration-identity-pools-cosmetic-"
    "fields-clive-fb812051aa0a/clw-calibration-identity-pools-cosmetic-fields"
)


def _recorded_bash_commands() -> list[str]:
    """The ``git show`` reads recorded from the contaminated arm's stream."""
    return [
        "cd " + MAIN_CHECKOUT + ' && git log --oneline -3 && echo "---" && '
        "git show 7572d83 --stat 2>&1 | head -30",
        "cd " + MAIN_CHECKOUT + " && git show 7572d83 -- reckon/calibration.py "
        "reckon/capabilities.py reckon/crew/routing.py",
        "cd " + MAIN_CHECKOUT + " && git show 7572d83 -- tests/test_calibration.py "
        "tests/test_capabilities.py tests/test_routing_surface.py",
    ]


def _stream_jsonl(lines: list[str], tmp_path) -> str:
    """Write one JSONL stream whose tool calls carry the given commands."""
    path = tmp_path / "stream.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for command in lines:
            event = {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": command},
                        }
                    ],
                },
            }
            handle.write(json.dumps(event) + "\n")
    return str(path)


def test_recorded_contaminated_stream_is_marked(tmp_path) -> None:
    """A shadow that ran ``git show`` of its primary's sha is contaminated."""
    stream = _stream_jsonl(_recorded_bash_commands(), tmp_path)
    calls = ledger.stream_tool_calls([stream])
    assert [kind for kind, _ in calls] == ["bash"] * 3

    reason = ledger.shadow_primary_read(
        calls,
        primary_commit=CONTAMINATED_PRIMARY_COMMIT,
        primary_paths=CONTAMINATED_PRIMARY_PATHS,
        repo_root=MAIN_CHECKOUT,
        worktree=CONTAMINATED_WORKTREE,
    )
    assert reason == "primary_commit_read"


def _shadow_record(run_id: str, *, contaminated: bool) -> dict:
    """One calibration-shaped shadow row, optionally marked contaminated."""
    record = ledger.build_record(
        run_id=run_id,
        plan="plan-a",
        gate="passed",
        node="shadow-node",
        completed_at_source="provided",
        worker_seconds=600,
        worker_seconds_source="provided",
        backend="clive",
        agent={"backend": "clive", "model": "candidate-model"},
        lineage={"kind": "shadow", "primary_run_id": "r-primary"},
        **({"shadow_contaminated": "primary_commit_read"} if contaminated else {}),
    )
    record.update({"attempt": 1, "attempt_kind": "dispatch"})
    return record


def test_a_contaminated_row_is_excluded_from_the_calibration_sample() -> None:
    """The marking makes every slice's own observation reject the row.

    ``calibrate_agent_speeds`` builds its sample through each record's
    observation: a marked row resolves to its exclusion reason instead of a
    (plan, agent, duration) tuple, so the slice's learning loop never sees it.
    """
    contaminated = _shadow_record("r-shadow-contaminated", contaminated=True)
    clean = _shadow_record("r-shadow-clean", contaminated=False)

    assert ledger.measurement_exclusion_reason(contaminated) == "contaminated"
    assert calibration._observation(contaminated) == "contaminated"

    assert ledger.measurement_exclusion_reason(clean) is None
    plan, agent, hours = calibration._observation(clean)
    assert plan == "plan-a"
    assert agent == calibration.calibration_configuration_key(clean)
    assert hours == 600 / 3600.0


def test_effort_report_counts_a_contaminated_row_under_its_own_key(
    tmp_path,
) -> None:
    """The effort view tallies void shadow evidence separately from stalled."""
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "docs"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "docs" / "plans" / "plan-a.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="plan-a">
</head><body><h2 id="dispatch">Dispatch</h2></body></html>
"""
    )
    record = _shadow_record("r-shadow-contaminated", contaminated=True)
    ledger.append_run("proj", record, root=root)

    report = ledger.effort_report("proj", root=root, declared={"plan-a": "M"})
    plan_row = next(row for row in report["plans"] if row["plan"] == "plan-a")
    assert plan_row["excluded_contaminated"] == 1
    assert plan_row["runs"] == 0


# The known-clean arm: its stream entered the main checkout only through the
# shared environment's interpreter, ran the primary's own changed test from the
# shadow's worktree, and issued bare status/diff reads there. Reading neither
# the primary's sha nor its changed files from the main checkout, it keeps its
# evidence.
CLEAN_PRIMARY_COMMIT = "58a5b760b8a37a1703b3984e70298a28063f1f71"
CLEAN_PRIMARY_PATHS = ("tests/test_fleet_rollup_surfaces.py",)
CLEAN_WORKTREE = (
    "/home/ITER/mcintos/Code/.reckon-worktrees/reckon-c8f839407e49/"
    "shadow-r-20260908T111222273123-fleet-rollup-test-follows-the-published-"
    "names-clive/fleet-rollup-test-follows-the-published-names"
)


def _recorded_clean_commands() -> list[tuple[str, str]]:
    """The clean arm's recorded calls: bash commands and one worktree read."""
    return [
        ("bash", "cd " + CLEAN_WORKTREE),
        (
            "bash",
            'PYTHONPATH="$PWD" '
            + MAIN_CHECKOUT
            + "/.venv/bin/python -m pytest -p no:cacheprovider -q "
            "tests/test_fleet_rollup_surfaces.py > /tmp/fleet_base.log 2>&1; "
            'echo "EXIT=$?"',
        ),
        ("bash", "cd " + CLEAN_WORKTREE),
        (
            "bash",
            'PYTHONPATH="$PWD" '
            + MAIN_CHECKOUT
            + "/.venv/bin/python -m pytest -p no:cacheprovider -q "
            "tests/test_fleet_rollup_surfaces.py > /tmp/fleet_after.log 2>&1; "
            'echo "EXIT=$?"',
        ),
        ("bash", 'git status --short; echo "=== diff ==="; git diff'),
        ("bash", "pwd && git rev-parse HEAD && git status --short"),
        (
            "read",
            CLEAN_WORKTREE + "/tests/test_fleet_rollup_surfaces.py",
        ),
    ]


def _stream_jsonl_calls(calls: list[tuple[str, str]], tmp_path) -> str:
    """Write a JSONL stream from ``(kind, payload)`` tool calls."""
    path = tmp_path / "stream.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for kind, payload in calls:
            content = (
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": payload},
                }
                if kind == "bash"
                else {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": payload},
                }
            )
            event = {
                "type": "assistant",
                "message": {"role": "assistant", "content": [content]},
            }
            handle.write(json.dumps(event) + "\n")
    return str(path)


def test_recorded_clean_stream_is_not_marked(tmp_path) -> None:
    """A shadow that entered the main checkout but read nothing stays clean."""
    stream = _stream_jsonl_calls(_recorded_clean_commands(), tmp_path)
    calls = ledger.stream_tool_calls([stream])
    assert [kind for kind, _ in calls] == [
        "bash",
        "bash",
        "bash",
        "bash",
        "bash",
        "bash",
        "read",
    ]

    reason = ledger.shadow_primary_read(
        calls,
        primary_commit=CLEAN_PRIMARY_COMMIT,
        primary_paths=CLEAN_PRIMARY_PATHS,
        repo_root=MAIN_CHECKOUT,
        worktree=CLEAN_WORKTREE,
    )
    assert reason is None


def test_a_path_mention_without_a_git_read_is_clean(tmp_path) -> None:
    """Running the primary's own test on the shadow's copy is not a read."""
    commands = [
        ("bash", "cd " + CLEAN_WORKTREE),
        (
            "bash",
            'PYTHONPATH="$PWD" ' + MAIN_CHECKOUT + "/.venv/bin/python -m pytest "
            "-q tests/test_fleet_rollup_surfaces.py",
        ),
    ]
    calls = ledger.stream_tool_calls([_stream_jsonl_calls(commands, tmp_path)])
    reason = ledger.shadow_primary_read(
        calls,
        primary_commit=CLEAN_PRIMARY_COMMIT,
        primary_paths=CLEAN_PRIMARY_PATHS,
        repo_root=MAIN_CHECKOUT,
        worktree=CLEAN_WORKTREE,
    )
    assert reason is None


def test_reading_own_worktree_via_git_c_is_clean(tmp_path) -> None:
    """``git -C`` at the shadow's worktree reads the shadow's own base file."""
    commands = [
        (
            "bash",
            "W="
            + CLEAN_WORKTREE
            + "\ncd "
            + MAIN_CHECKOUT
            + '\ngit -C "$W" show "HEAD:tests/test_fleet_rollup_surfaces.py"',
        ),
    ]
    calls = ledger.stream_tool_calls([_stream_jsonl_calls(commands, tmp_path)])
    reason = ledger.shadow_primary_read(
        calls,
        primary_commit=CLEAN_PRIMARY_COMMIT,
        primary_paths=CLEAN_PRIMARY_PATHS,
        repo_root=MAIN_CHECKOUT,
        worktree=CLEAN_WORKTREE,
    )
    assert reason is None


def test_changed_path_read_from_main_checkout_is_marked(tmp_path) -> None:
    """A git content read of a changed path from the main checkout counts."""
    commands = [
        (
            "bash",
            "cd "
            + MAIN_CHECKOUT
            + ' && git show "HEAD:reckon/calibration.py" | head -40',
        ),
    ]
    calls = ledger.stream_tool_calls([_stream_jsonl_calls(commands, tmp_path)])
    reason = ledger.shadow_primary_read(
        calls,
        primary_commit=CONTAMINATED_PRIMARY_COMMIT,
        primary_paths=CONTAMINATED_PRIMARY_PATHS,
        repo_root=MAIN_CHECKOUT,
        worktree=CONTAMINATED_WORKTREE,
    )
    assert reason == "primary_changed_path_read"


def test_an_abbreviated_sha_is_marked_but_a_short_fragment_is_not(tmp_path) -> None:
    """Seven hex digits name the commit; six are too short to read as one."""
    abbreviated = [
        ("bash", "cd " + MAIN_CHECKOUT + " && git show 7572d83 --stat | head"),
    ]
    calls = ledger.stream_tool_calls([_stream_jsonl_calls(abbreviated, tmp_path)])
    assert (
        ledger.shadow_primary_read(
            calls,
            primary_commit=CONTAMINATED_PRIMARY_COMMIT,
            primary_paths=CONTAMINATED_PRIMARY_PATHS,
            repo_root=MAIN_CHECKOUT,
            worktree=CONTAMINATED_WORKTREE,
        )
        == "primary_commit_read"
    )

    fragment = [
        ("bash", 'echo "a 7572d8 fragment" && git status --short'),
    ]
    calls = ledger.stream_tool_calls([_stream_jsonl_calls(fragment, tmp_path)])
    assert (
        ledger.shadow_primary_read(
            calls,
            primary_commit=CONTAMINATED_PRIMARY_COMMIT,
            primary_paths=CONTAMINATED_PRIMARY_PATHS,
            repo_root=MAIN_CHECKOUT,
            worktree=CONTAMINATED_WORKTREE,
        )
        is None
    )


def test_primary_sha_in_a_non_git_command_is_clean(tmp_path) -> None:
    """Naming the sha without a git read resolves nothing and stays clean."""
    commands = [
        (
            "bash",
            "cd "
            + CONTAMINATED_WORKTREE
            + ' && echo "trace 7572d83 seen" > manifest-notes.txt',
        ),
    ]
    calls = ledger.stream_tool_calls([_stream_jsonl_calls(commands, tmp_path)])
    reason = ledger.shadow_primary_read(
        calls,
        primary_commit=CONTAMINATED_PRIMARY_COMMIT,
        primary_paths=CONTAMINATED_PRIMARY_PATHS,
        repo_root=MAIN_CHECKOUT,
        worktree=CONTAMINATED_WORKTREE,
    )
    assert reason is None


def test_file_read_of_changed_path_in_main_is_marked_worktree_is_clean(
    tmp_path,
) -> None:
    """File-path reads discriminate by which checkout they target."""
    from_main = [("read", MAIN_CHECKOUT + "/reckon/calibration.py")]
    assert (
        ledger.shadow_primary_read(
            ledger.stream_tool_calls([_stream_jsonl_calls(from_main, tmp_path)]),
            primary_commit=CONTAMINATED_PRIMARY_COMMIT,
            primary_paths=CONTAMINATED_PRIMARY_PATHS,
            repo_root=MAIN_CHECKOUT,
            worktree=CONTAMINATED_WORKTREE,
        )
        == "primary_changed_path_read"
    )

    from_worktree = [
        ("read", CONTAMINATED_WORKTREE + "/reckon/calibration.py"),
    ]
    assert (
        ledger.shadow_primary_read(
            ledger.stream_tool_calls([_stream_jsonl_calls(from_worktree, tmp_path)]),
            primary_commit=CONTAMINATED_PRIMARY_COMMIT,
            primary_paths=CONTAMINATED_PRIMARY_PATHS,
            repo_root=MAIN_CHECKOUT,
            worktree=CONTAMINATED_WORKTREE,
        )
        is None
    )


def test_primary_read_targets_are_resolved_from_the_primarys_own_commit(
    tmp_path,
) -> None:
    """Changed paths come from the primary's cited commit, never the fixture."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "reckon").mkdir()
    (repo / "reckon" / "calibration.py").write_text("value = 1\n")
    (repo / "reckon" / "capabilities.py").write_text("value = 2\n")
    for args in (
        ["add", "reckon/calibration.py", "reckon/capabilities.py"],
        ["commit", "-q", "-m", "chore: land two files"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    commits = promotion._resolve_commits(
        cwd=repo, revisions=["HEAD"], run_id="r-resolution"
    )
    primary = {"run_id": "r-primary", "commits": list(commits)}
    commit, paths = promotion._primary_read_targets(primary, repo)
    assert commit == commits[0]
    assert paths == ("reckon/calibration.py", "reckon/capabilities.py")
    assert all((repo / path).exists() for path in paths)
