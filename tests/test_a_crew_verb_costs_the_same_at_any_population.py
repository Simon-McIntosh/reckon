"""A crew verb costs the same at any run population.

Every slow stage found in a promotion was found one at a time, by profiling a
live run on the fleet node: the held-worktree fallback scan, the per-row
catalogue resolve, the rollout-home walk and the ledger snapshot walk. Each was
a read whose cost grew with the number of runs the crew home holds. A repair
landed for each, and nothing noticed the next one until a promotion again took
minutes.

This module guards the verbs rather than the functions. It drives a whole
``crew complete --gate not-run`` of a review run, a plan-review compose, and a
``crew complete --land`` of an implement run, and counts the filesystem calls
each one makes over a crew home and ledger holding 20 and then 200 other runs.
A verb whose cost grows with the population makes more calls at 200 than at 20
and fails here, whichever function inside it grew.

The count is of calls, not of entries: one ``scandir`` over a directory holding
two hundred children is one call, while a stat of each child is two hundred.
That is the distinction the guard exists to draw — a walk that visits every run
costs a call per run, and a walk that visits one run costs one.

Each measurement runs in its own interpreter, so a module-level cache warmed by
one arm cannot make the next arm look cheaper. Within an arm the verb runs once
before the counter is armed, which removes the one-time costs a live verb pays
once per home — the run store's first migration and the ledger index's first
build — leaving the count comparing the verb's steady-state cost. The compose
case is a dry-run preview: a preview writes nothing, so the two arms compose
the same work rather than the first changing the second's.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

POPULATIONS = (20, 200)
PROJECT = "proj"
PLAN = "plan-a"

# The three verbs this guard covers. A fourth case is one entry here and one arm
# in the driver, not a fourth copy of the measurement machinery.
CASES = {
    "review-promotion": "crew complete --gate not-run of a review run",
    "plan-review-compose": "a plan-review compose",
    "implement-landing": "crew complete --land of an implement run",
}


# ── The counting wrapper the module owns ───────────────────────────────────


def _count_filesystem_calls(action):
    """Run ``action`` and return the filesystem calls it made, by kind.

    ``stat`` also counts ``lstat``, ``Path.stat`` and every ``Path`` predicate
    built on it, because they are one syscall and the question is how many
    filesystem entries the verb touched. ``glob`` counts the call, not the
    matches.
    """
    import glob as glob_module
    import pathlib

    counts = {"stat": 0, "scandir": 0, "listdir": 0, "glob": 0}

    def wrap(name, original):
        def counted(*args, **kwargs):
            counts[name] += 1
            return original(*args, **kwargs)

        return counted

    attributes = {
        (os, "stat"): ("stat", os.stat),
        (os, "lstat"): ("stat", os.lstat),
        (os, "scandir"): ("scandir", os.scandir),
        (os, "listdir"): ("listdir", os.listdir),
    }
    saved = [
        (holder, name, original)
        for (holder, name), (_k, original) in attributes.items()
    ]
    saved.append((glob_module, "glob", glob_module.glob))
    saved.append((pathlib.Path, "glob", pathlib.Path.glob))
    try:
        for (holder, name), (kind, original) in attributes.items():
            setattr(holder, name, wrap(kind, original))
        glob_module.glob = wrap("glob", glob_module.glob)
        pathlib.Path.glob = wrap("glob", pathlib.Path.glob)
        action()
    finally:
        for holder, name, original in saved:
            setattr(holder, name, original)
    return counts


# ── The fixture the driver builds ──────────────────────────────────────────


def _synthetic_row(position: int) -> dict:
    """One completed run row, shaped like a promoted implement run."""
    return {
        "run_id": f"r-20260101T{position // 3600 % 24:02d}{position // 60 % 60:02d}"
        f"{position % 60:02d}000000-fixture-{position:04d}",
        "project": PROJECT,
        "node": f"fixture-node-{position:04d}",
        "role": "implement",
        "plan": PLAN,
        "gate": "passed",
        "status": "complete",
        "session_id": f"session-{position:04d}",
        "dispatched_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
    }


def _write_plan(path: Path) -> None:
    from reckon import _plan_html

    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Plan A",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )


def _seed_population(root: Path, population: int) -> None:
    """Write ``population`` completed runs everywhere a run is kept.

    Each other run gets all three places a stage grew over: a row in the
    ledger's aggregate, its own document under the ledger's ``runs/`` (the
    snapshot walk), and a crew run directory with a client harness home inside
    it (the rollout-home walk). A guard that seeds only one of them leaves the
    other two populations invisible to it.
    """
    from reckon import ledger
    from reckon.crew import rollout
    from reckon.crew import runs as crew_runs

    rows = [_synthetic_row(position) for position in range(population)]
    ledger.write(
        PROJECT,
        {"members": [], "runs": rows, "holds": []},
        0,
        root=root,
    )
    for row in rows:
        ledger.run_path(PROJECT, row["run_id"], root).parent.mkdir(
            parents=True, exist_ok=True
        )
        ledger.write_run_file(PROJECT, row, root=root)
    runs_root = crew_runs.runs_dir()
    runs_root.mkdir(parents=True, exist_ok=True)
    for position in range(population):
        run_home = rollout.run_home_sessions_root(runs_root / f"r-peer-{position:04d}")
        (run_home / "2026" / "11" / "10").mkdir(parents=True, exist_ok=True)


def _build_checkout(root: Path, origin: Path | None = None) -> Path:
    """A committed checkout, optionally pushed to a bare origin."""
    from tests.helpers.git import git as _git

    _write_plan(root / "docs" / "plans" / f"{PLAN}.html")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    if not (root / ".git").exists():
        for arguments in (
            ("init", "-q", "-b", "main"),
            ("config", "user.email", "worker@example.invalid"),
            ("config", "user.name", "Worker"),
            ("config", "commit.gpgsign", "false"),
        ):
            _git(root, *arguments)
    _git(root, "add", "docs", "seed.txt")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    if origin is not None:
        _git(root, "remote", "add", "origin", str(origin))
        _git(root, "push", "-q", "-u", "origin", "main")
    return root


def _head(path: Path) -> str:
    from tests.helpers.git import git as _git

    return _git(path, "rev-parse", "HEAD")


def _write_review_run(root: Path, home: Path, run_id: str) -> dict:
    """A completed report-only review run, as the live pointer promotion reads."""
    from reckon.crew.runs import _write_json, pointer_path
    from reckon.crew.runs import run_dir as _run_dir

    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        "changed_paths: none (the sole deliverable is the report)\n"
        "tests: true\n",
        encoding="utf-8",
    )
    Path(_run_dir(run_id)).mkdir(parents=True, exist_ok=True)
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(root),
            "worktree": str(root),
            "base_sha": _head(root),
            "launch": "in-harness",
            "role": "review",
            "backend": "native",
            "created_at": "2026-11-11T06:59:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s2",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )
    return {"run_id": run_id}


def _write_implement_run(tmp_path: Path, root: Path, home: Path, run_id: str) -> dict:
    """An implement run whose worktree holds one reviewed commit to land.

    The worktree is cut from the repository the project's mount names, because
    a landing refuses a checkout that is not the project's registered one.
    """
    from reckon.crew.runs import _write_json, pointer_path
    from tests.helpers.git import git as _git

    origin = tmp_path / "origin.git"
    repository = root
    seed = _head(repository)
    worktree = tmp_path / "worktrees" / run_id
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", str(worktree), seed)
    (worktree / f"{run_id}.txt").write_text("reviewed change\n", encoding="utf-8")
    _git(worktree, "add", f"{run_id}.txt")
    _git(worktree, "commit", "-q", "-m", f"add {run_id}")
    head = _git(worktree, "rev-parse", "HEAD")
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("node: node-a\nstatus: complete\n", encoding="utf-8")
    gate_log = home / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n", encoding="utf-8")
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "base_sha": seed,
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-11-08T00:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s6",
                "time_budget": "35m",
                "write_paths": [f"{run_id}.txt"],
            },
        },
    )
    return {
        "repository": repository,
        "origin": origin,
        "run_id": run_id,
        "head": head,
        "gate_log": gate_log,
    }


# ── Driving one verb ───────────────────────────────────────────────────────


def _invoke(arguments: list[str]):
    from click.testing import CliRunner

    from reckon import cli as cli_module

    result = CliRunner().invoke(cli_module.main, arguments)
    return result.exit_code, result.output


def _drive(case: str, fixture: dict, run_index: int) -> dict:
    """Run one arm of one verb, returning its exit status and a marker."""
    if case == "review-promotion":
        run = fixture["review_runs"][run_index]
        exit_code, output = _invoke(
            [
                "crew",
                "complete",
                "--run",
                run["run_id"],
                "--gate",
                "not-run",
                "--outcome",
                "the fixture exercises the filesystem calls a promotion makes",
                "--no-commit",
                "the sole deliverable is the report",
                "--checkout-path",
                str(fixture["root"]),
            ]
        )
        return {
            "exit_code": exit_code,
            "output": output[-2000:],
            "marker": run["run_id"],
        }
    if case == "implement-landing":
        run = fixture["implement_runs"][run_index]
        exit_code, output = _invoke(
            [
                "crew",
                "complete",
                "--run",
                run["run_id"],
                "--gate",
                "passed",
                "--commit",
                run["head"],
                "--outcome",
                "one call promoted, merged, pushed and released the run",
                "--checkout-path",
                str(run["repository"]),
                "--no-impl-change",
                "the fixture lands one file and moves no plan",
                "--gate-command",
                "true",
                "--gate-exit-status",
                "0",
                "--gate-log-path",
                str(run["gate_log"]),
                "--land",
            ]
        )
        return {
            "exit_code": exit_code,
            "output": output[-2000:],
            "marker": run["head"],
        }
    if case == "plan-review-compose":
        from reckon.crew import plan_review

        report = plan_review.compose_owed_plan_reviews(
            PROJECT, dry_run=True, base_dir=fixture["root"]
        )
        return {
            "exit_code": 0 if not report.get("failed") else 1,
            "output": json.dumps(report)[:2000],
            "marker": report.get("read", []),
        }
    raise AssertionError(f"unknown case {case!r}")


def _build_fixture(case: str, workspace: Path, population: int) -> dict:
    from reckon.crew import rollout

    home = workspace / "config"
    home.mkdir(parents=True, exist_ok=True)
    (home / "client-sessions").mkdir(exist_ok=True)
    rollout.CLIENT_SESSIONS_DIR = home / "client-sessions"
    root = _build_checkout(workspace / "repo")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    _seed_population(root, population)
    # An edit inside the authoring burst but outside the quiet period, so the
    # compose weighs the plan rather than skipping it as still being written.
    plan_path = root / "docs" / "plans" / f"{PLAN}.html"
    stamp = plan_path.stat().st_mtime - 3600
    os.utime(plan_path, (stamp, stamp))
    fixture = {"root": root, "home": home}
    if case == "review-promotion":
        from reckon.crew import ledger

        ledger.load(PROJECT, root=root)  # build the index outside the count
        fixture["review_runs"] = [
            _write_review_run(root, home, f"r-20261111T07000000000{n}-node-a")
            for n in (1, 2)
        ]
    elif case == "implement-landing":
        from reckon.crew import ledger
        from tests.helpers.git import git as _git

        origin = workspace / "origin.git"
        _git(workspace, "init", "-q", "--bare", str(origin))
        _git(root, "remote", "add", "origin", str(origin))
        _git(root, "push", "-q", "-u", "origin", "main")
        ledger.load(PROJECT, root=root)
        fixture["implement_runs"] = [
            _write_implement_run(
                workspace, root, home, f"r-20261111T08000000000{n}-node-a"
            )
            for n in (1, 2)
        ]
    else:
        from reckon.crew import ledger

        ledger.load(PROJECT, root=root)
    return fixture


def _measure(case: str, workspace: Path, population: int) -> dict:
    """Build the fixture, run the verb once to warm, then count a second run."""
    fixture = _build_fixture(case, workspace, population)
    _drive(case, fixture, 0)
    result: dict = {}

    def counted():
        result.update(_drive(case, fixture, 1))

    counts = _count_filesystem_calls(counted)
    return {
        "counts": counts,
        "exit_code": result.get("exit_code"),
        "output": result.get("output"),
        "marker": result.get("marker"),
        "population": population,
    }


def main(argv: list[str]) -> int:
    """The subprocess driver: one arm of one case, on a workspace of its own."""
    case, population, workspace = argv[0], int(argv[1]), Path(argv[2])
    config_home = workspace / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    os.environ["RECKON_HOME"] = str(config_home)
    os.environ.pop("RECKON_RUN_ID", None)
    try:
        measured = _measure(case, workspace, population)
    except Exception as exc:  # noqa: BLE001 — the driver reports its own failure
        measured = {"error": f"{type(exc).__name__}: {exc}", "population": population}
    print(json.dumps(measured))
    return 0


def _run_arm(case: str, population: int, workspace: Path) -> dict:
    """Run one arm in its own interpreter and read back what it measured."""
    repository_root = Path(__file__).resolve().parent.parent
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repository_root)
    environment.pop("RECKON_RUN_ID", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            case,
            str(population),
            str(workspace),
        ],
        cwd=str(repository_root),
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    line = completed.stdout.strip().splitlines()[-1]
    measured = json.loads(line)
    assert "error" not in measured, measured["error"]
    return measured


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_crew_verb_costs_the_same_at_any_population(
    case: str, tmp_path: Path
) -> None:
    """The verb makes the same number of filesystem calls at 200 runs as at 20."""
    measured = {
        population: _run_arm(case, population, tmp_path / f"{case}-{population}")
        for population in POPULATIONS
    }

    narrow, wide = measured[POPULATIONS[0]], measured[POPULATIONS[1]]

    # The arms are shown to have reached the work before an equality is read
    # as an absence: a verb that refused before its first read would make the
    # same count at both sizes for the wrong reason.
    for population, arm in measured.items():
        assert arm["exit_code"] == 0, (population, arm["exit_code"], arm["output"])
    assert narrow["marker"], (POPULATIONS[0], narrow["output"])

    assert wide["counts"] == narrow["counts"], (
        f"{CASES[case]} made more filesystem calls over {POPULATIONS[1]} other "
        f"runs than over {POPULATIONS[0]}: {narrow['counts']} at 20, "
        f"{wide['counts']} at {POPULATIONS[1]}"
    )


def test_the_counter_sees_the_run_population(tmp_path: Path) -> None:
    """A lookup shaped to walk every run home makes the counter grow.

    Without this, a counter that observed nothing would report the equal counts
    the guard above reads as independence. The lookup is the rollout receipt
    read the promotion path no longer makes: it names no run, so it visits
    every run directory under the crew home.
    """
    from reckon.crew import rollout

    session = "1a2b3c4d-0001-4000-8000-000000000001"

    def arms():
        counts = {}
        for population in POPULATIONS:
            fixture = _build_fixture(
                "review-promotion", tmp_path / str(population), population
            )
            rollout.CLIENT_SESSIONS_DIR = Path(fixture["home"]) / "client-sessions"
            counts[population] = _count_filesystem_calls(
                lambda: rollout.read_rollout_receipt(session)
            )
        return counts

    counts = arms()

    assert (
        counts[POPULATIONS[1]]["scandir"] > counts[POPULATIONS[0]]["scandir"]
        or counts[POPULATIONS[1]]["stat"] > counts[POPULATIONS[0]]["stat"]
    ), (
        "the counter read the same over 200 other runs as over 20, so it "
        f"cannot see the population: {counts}"
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
