"""A node's figures ride every launch path, or the launch refuses.

``TaskNode.images`` supplies figures to dispatch. SVG inputs are recorded as
run-local PNGs; each launch receives one ``-i <path>`` per recorded image. A
resume or lane change reads those paths from the run record.

Each property is asserted on the composed plan the launcher would run, never on
a helper returned by one:

* a node dispatched with two figures emits one ``-i`` per file on a codex lane,
  and its prompt file carries the brief with no listing beside the flag;
* ``crew dispatch --image`` threads the flag through the command to the same
  argv, so the option the delivery probe and the evaluation use is wired;
* a run whose record carries two figures resumes with both flags;
* moving such a run to another codex lane keeps both flags in the new argv;
* the same node on a clive lane, which declares image input and carries no
  image flag, is handed each figure's path in its prompt and nothing on argv;
* those paths are in the prompt file the worker reads, not only in the composed
  plan, because the file is what the worker is fed;
* a node carrying no figures writes a prompt file with no listing;
* an in-harness launch composes no plan and writes the uncomposed prompt;
* the same node on the claude lane, which shares clive's flag grammar and
  declares nothing, is refused, and the refusal names the lane.

Writing the file from the prompt as it stood before ``launch_plan`` composed
it -- which is what a fresh dispatch did -- leaves every plan-level arm here
green and fails only the clive case, because that case reads the file rather
than the plan. The discrimination sits on the file the worker is fed, not on
the composition the launcher would run.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, crew_dispatch_commands
from reckon.crew.runs import pointer_path
from tests import test_a_live_run_never_reads_dead as liveness

dispatch_module = importlib.import_module("reckon.crew.dispatch")
sessions_module = importlib.import_module("reckon.crew.dispatch_sessions")

PROJECT = "sample"
PLAN = "plan-a"
FIRST = "docs/figures/plan-a/node-a/first.svg"
SECOND = "docs/figures/plan-a/node-a/second.svg"

# The heading a prompt carries when its lane is named its figures. Spelled here
# rather than imported so the assertion reads the text the worker is handed,
# and a lane that names no figures is shown to leave it out.
FIGURE_LISTING_MARKER = "Figures attached to this node"

# Resolved before any fixture patches HOME, so it names the operator's own crew
# home however the tests redirect theirs.
REAL_CREW_HOME = Path(os.path.expanduser("~")) / ".config" / "reckon" / "crew"

# Every run id a case creates, so the operator's crew home can be shown not to
# carry any of them.
_CREATED_RUN_IDS: set[str] = set()


def _written_under_operator_home(run_id: str) -> list[Path]:
    """Where this run's durable artifacts would land under the operator's home."""
    return [
        candidate
        for candidate in (
            REAL_CREW_HOME / "runs" / run_id,
            REAL_CREW_HOME / "live" / f"{run_id}.json",
        )
        if candidate.exists()
    ]


@pytest.fixture(autouse=True)
def _real_crew_home_untouched():
    """Every run resolves under a temporary home; the operator's is read-only.

    A shared home is written by peer sessions, so the check is that none of the
    run ids this file creates appears under it — an equality of listings would
    measure the fleet rather than this file's hermeticity.
    """
    before = set(_CREATED_RUN_IDS)
    yield
    for run_id in _CREATED_RUN_IDS - before:
        landed = _written_under_operator_home(run_id)
        assert not landed, f"run {run_id!r} was written to {landed}"


@pytest.fixture()
def operator_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary operator home the fence composes against."""
    home = tmp_path / "operator-home"
    for name in (
        ".claude",
        ".codex",
        ".ssh",
        ".agents",
        ".config/reckon",
        ".config/git",
        "public",
        ".local/bin",
    ):
        (home / name).mkdir(parents=True, exist_ok=True)
    for name in (".claude.json", ".gitconfig"):
        (home / name).write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return home


# The mutation the node declares, repeated here so a reader of the file meets it.
NEGATIVE_CONTROL_MUTATION = (
    "make the fresh dispatch write the uncomposed prompt to its prompt file "
    "again: the clive case must then fail on the file it reads, because the "
    "composed naming never reaches it"
)


def _backend(bindir: Path, command: str, modalities: list[str] | None = None) -> dict:
    settings = {
        "launch": "cli",
        "command": str(bindir / command),
        "sandbox": "worktree-full",
        "time_budget": "25m",
        "session_reuse": True,
        "usable_input_window": 512_000,
    }
    if modalities is not None:
        settings["input_modalities"] = modalities
    return settings


@pytest.fixture()
def harness_bin(tmp_path: Path) -> Path:
    """Stub harness executables so dialect resolution never reads the host PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("codex", "claude", "clive"):
        stub = bindir / name
        stub.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
        stub.chmod(0o755)
    return bindir


@pytest.fixture()
def config(harness_bin: Path) -> dict:
    return {
        "default_backend": "alpha",
        "local_backend": "alpha",
        "backends": {
            "alpha": _backend(harness_bin, "codex"),
            "beta": _backend(harness_bin, "codex"),
            # clive speaks claude's flags and declares image input, so a figure
            # reaches it in the prompt its worker reads. claude shares that flag
            # grammar and declares nothing, so it keeps refusing by name.
            "clive": _backend(harness_bin, "clive", ["text", "image"]),
            "claude": _backend(harness_bin, "claude"),
        },
        "roles": {"implement": {}},
        "budget": {
            "utilisation_ceiling_pct": 100,
            "resume_reserve_pct": 5,
            "exhausted_statuses": [],
        },
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def repo(
    tmp_path: Path,
    crew_home: Path,
    operator_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """A throwaway mount carrying a plan and a worktree prepared without git.

    The worktree is prepared inline so the launch under measurement is the only
    thing this file exercises; the fleet script the real dispatch shells out to
    is stubbed at its seam.
    """
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / f"{PLAN}.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN}">'
        '<h2 id="s6">A dispatch carries a node\'s figures</h2>',
        encoding="utf-8",
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt", f"docs/plans/{PLAN}.html"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *args)
    (crew_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    base_sha = _git(root, "rev-parse", "HEAD")

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "--detach", "--force", str(path), base_sha],
            cwd=root,
            check=True,
            capture_output=True,
        )
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    return root


class _Recorder:
    """Stands in for the spawn, recording the plan the launcher would run."""

    def __init__(self) -> None:
        self.argv: list[list[str]] = []
        self.stdin_text: list[str] = []

    def __call__(self, plan, *, log_path, stderr_path, prompt_path) -> int:
        self.argv.append([str(token) for token in plan.argv])
        self.stdin_text.append(str(plan.stdin_text))
        return os.getpid()


def _node(crew_home: Path, images: list[str], *, backend_role: str = "implement"):
    manifest = crew_home / "manifests" / "node-a.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    return crew.TaskNode(
        id="node-a",
        goal="carry a node's figures through every launch path",
        plan=PLAN,
        section="§6",
        done_when="tests/test_dispatch_attaches_the_figures.py passes",
        write_paths=["allowed.txt"],
        time_budget="25m",
        manifest_path=str(manifest),
        spec_level="guided",
        role=backend_role,
        images=images,
    )


def _image_pairs(argv: list[str]) -> list[tuple[str, str]]:
    return [
        (flag, argv[index + 1])
        for index, flag in enumerate(argv)
        if flag == "-i" and index + 1 < len(argv)
    ]


def _worker_prompt(run_id: str) -> str:
    """The prompt the worker is handed: the run's own prompt file."""
    return (crew.run_dir(run_id) / "prompt.txt").read_text(encoding="utf-8")


def _write_figure_sources(repo: Path) -> None:
    for name in (FIRST, SECOND):
        source = repo / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 70 35">'
            '<rect width="70" height="35"/></svg>',
            encoding="utf-8",
        )


def _stopped_pointer(tmp_path: Path, repo: Path, run_id: str, *, backend: str) -> dict:
    """A cli run as a resume or a lane change finds it: gone, on this host."""
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    tree = tmp_path / f"{run_id}-tree"
    tree.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(f"node: {run_id}\nstatus: waiting\n", encoding="utf-8")
    prompt_path = directory / "prompt.txt"
    prompt_path.write_text("the original dispatch prompt\n", encoding="utf-8")
    liveness._write_exit_record(run_id)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(tree),
        "launch": "cli",
        "backend": backend,
        "dialect": "codex",
        "role": "implement",
        "pid": liveness._absent_pid(),
        "pid_start_time": None,
        "process_alive": None,
        "session_id": f"sess-{run_id}",
        "session_harness": "codex",
        "attempt": 1,
        "base_sha": "HEAD",
        "created_at": "2026-10-14T09:59:00+00:00",
        "log_path": str(directory / "stream.jsonl"),
        "manifest_path": str(manifest),
        "prompt_path": str(prompt_path),
        "phase": "working",
        "node": {
            "id": "node-a",
            "plan": PLAN,
            "section": "§6",
            "time_budget": "25m",
            "write_paths": ["allowed.txt"],
            "images": [FIRST, SECOND],
        },
    }
    crew._write_json(pointer_path(run_id), record)
    return record


def test_a_dispatch_of_two_figures_emits_two_flags(
    config: dict, repo: Path, tmp_path: Path
) -> None:
    _write_figure_sources(repo)
    recorder = _Recorder()
    record = dispatch_module.dispatch(
        node=_node(tmp_path, [FIRST, SECOND]),
        project=PROJECT,
        repo=repo,
        config=config,
        session="fig-direct",
        check_budget=False,
        launcher=recorder,
    )

    argv = [str(token) for token in record["argv"]]
    _CREATED_RUN_IDS.add(str(record["run_id"]))
    directory = crew.run_dir(record["run_id"])
    expected = [("-i", str(directory / f"image-{index}.png")) for index in range(2)]
    assert _image_pairs(argv) == expected, argv
    assert _image_pairs(recorder.argv[0]) == expected
    assert record["node"]["images"] == [path for _, path in expected]


def test_a_clive_lane_is_handed_the_figures_in_the_prompt_its_worker_reads(
    config: dict, repo: Path, tmp_path: Path
) -> None:
    """clive carries no image flag, so the figure travels in the prompt the
    worker is handed -- and argv stays free of it. Both halves matter: a path
    that never reaches the worker leaves the figure unopened, and a path on
    argv would be handed to a harness that rejects it."""
    _write_figure_sources(repo)
    recorder = _Recorder()
    record = dispatch_module.dispatch(
        node=_node(tmp_path, [FIRST, SECOND]),
        project=PROJECT,
        repo=repo,
        config=config,
        session="fig-clive",
        check_budget=False,
        backend_override="clive",
        launcher=recorder,
    )
    _CREATED_RUN_IDS.add(str(record["run_id"]))
    directory = crew.run_dir(record["run_id"])
    recorded = [str(directory / f"image-{index}.png") for index in range(2)]
    handed = _worker_prompt(str(record["run_id"]))

    for path in recorded:
        assert path in recorder.stdin_text[0]
    assert "Read" in recorder.stdin_text[0]
    assert _image_pairs([str(token) for token in record["argv"]]) == []
    # The worker is fed this file, not the plan, so the naming has to be in the
    # file itself: a path that only ever existed in the composed plan is handed
    # to a worker that never reads the plan.
    for path in recorded:
        assert path in handed, handed
    assert "Read" in handed


def test_an_undeclared_lane_refuses_the_figures_and_names_the_lane(
    config: dict, repo: Path, tmp_path: Path
) -> None:
    """The claude lane shares clive's flag grammar and declares no image input,
    so it refuses by name rather than composing an un-named launch beside it:
    the declaration is the lane's, and a change keyed on the dialect would move
    both at once."""
    _write_figure_sources(repo)
    with pytest.raises(crew.CrewError) as excinfo:
        dispatch_module.dispatch(
            node=_node(tmp_path, [FIRST]),
            project=PROJECT,
            repo=repo,
            config=config,
            session="fig-claude",
            check_budget=False,
            backend_override="claude",
            launcher=_Recorder(),
        )

    assert "claude" in str(excinfo.value)


def test_crew_dispatch_image_reaches_the_argv(
    config: dict,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_figure_sources(repo)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *args, **kwargs: config
    )
    recorder = _Recorder()
    real_dispatch = crew.dispatch

    def dispatch_with_launcher(**kwargs):
        kwargs.setdefault("launcher", recorder)
        return real_dispatch(**kwargs)

    monkeypatch.setattr(crew, "dispatch", dispatch_with_launcher)

    runner = CliRunner()
    result = runner.invoke(
        cli_module.main,
        [
            "crew",
            "dispatch",
            "--project",
            PROJECT,
            "--plan",
            PLAN,
            "--section",
            "s6",
            "--role",
            "implement",
            "--spec-level",
            "exact",
            "--node",
            "node-a",
            "--goal",
            "carry a node's figures through the command",
            "--done-when",
            "tests/test_dispatch_attaches_the_figures.py passes",
            "--write-path",
            "allowed.txt",
            "--image",
            FIRST,
            "--image",
            SECOND,
            "--session",
            "fig-cli",
            "--repo",
            str(repo),
            "--no-watch",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    _CREATED_RUN_IDS.add(str(payload["run_id"]))
    argv = [str(token) for token in payload["argv"]]
    directory = crew.run_dir(payload["run_id"])
    expected = [("-i", str(directory / f"image-{index}.png")) for index in range(2)]
    assert _image_pairs(argv) == expected, argv
    assert payload["node"]["images"] == [path for _, path in expected]


def test_a_resumed_run_keeps_both_flags(
    config: dict, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions_module, "FENCE_WORKERS", True)
    run_id = "r-fig-resume"
    _CREATED_RUN_IDS.add(run_id)
    _stopped_pointer(tmp_path, repo, run_id, backend="alpha")

    plan = crew.resume_plan(run_id, "continue", config=config)

    argv = [str(token) for token in plan.argv]
    assert _image_pairs(argv) == [("-i", FIRST), ("-i", SECOND)], argv


def test_a_resumed_clive_run_is_named_its_figures(
    config: dict, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resumed turn reads the composing done here, and the resumed worker
    reads the file that composition writes, so this pair is the end-to-end
    delivery a fresh dispatch reaches through the same two steps."""
    monkeypatch.setattr(sessions_module, "FENCE_WORKERS", True)
    run_id = "r-fig-resume-clive"
    _CREATED_RUN_IDS.add(run_id)
    _stopped_pointer(tmp_path, repo, run_id, backend="clive")

    plan = crew.resume_plan(run_id, "continue", config=config)

    assert FIRST in plan.stdin_text
    assert SECOND in plan.stdin_text
    assert _image_pairs([str(token) for token in plan.argv]) == []


def test_a_lane_change_keeps_both_flags(
    config: dict, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions_module, "FENCE_WORKERS", True)
    run_id = "r-fig-lane"
    _CREATED_RUN_IDS.add(run_id)
    _stopped_pointer(tmp_path, repo, run_id, backend="alpha")

    resolution = SimpleNamespace(
        backend="beta",
        launch="cli",
        backend_settings=dict(config["backends"]["beta"]),
        lane_gate={
            "state": "open",
            "gate_path": "fixture-gate.json",
            "paused": False,
            "reason": None,
            "detail": "",
            "path_check": "skipped",
            "path_check_detail": "backend declares no lane document to publish a config_path",
        },
        validation=SimpleNamespace(ok=True, findings=[]),
        competence={"allowed": True},
        authority="a-ledger-authority",
        sandbox_write_roots=None,
    )
    monkeypatch.setattr(sessions_module, "plan_dispatch", lambda **kwargs: resolution)
    monkeypatch.setattr(
        sessions_module, "_budget_verdict", lambda **kwargs: {"held": False}
    )
    monkeypatch.setattr(
        sessions_module,
        "resolve_dispatch_ledger_root",
        lambda authority: authority,
    )

    moved = dispatch_module.change_lane(
        run_id,
        "beta",
        "the lane is spent",
        config=config,
        launch=True,
        launcher=_Recorder(),
    )

    argv = [str(token) for token in moved["argv"]]
    assert _image_pairs(argv) == [("-i", FIRST), ("-i", SECOND)], argv


def test_a_codex_prompt_file_carries_the_brief_and_no_figure_listing(
    config: dict, repo: Path, tmp_path: Path
) -> None:
    """The codex lane hands each figure over on ``-i``, so its prompt file must
    not name them: a listing beside the flag would hand the worker the figure a
    second time, and a worker that opens a file the harness was already given
    is exactly the delivery a lane that names its figures does not need."""
    _write_figure_sources(repo)
    record = dispatch_module.dispatch(
        node=_node(tmp_path, [FIRST, SECOND]),
        project=PROJECT,
        repo=repo,
        config=config,
        session="fig-codex-prompt",
        check_budget=False,
        launcher=_Recorder(),
    )
    _CREATED_RUN_IDS.add(str(record["run_id"]))
    directory = crew.run_dir(record["run_id"])
    handed = _worker_prompt(str(record["run_id"]))

    assert _image_pairs([str(token) for token in record["argv"]]) == [
        ("-i", str(directory / "image-0.png")),
        ("-i", str(directory / "image-1.png")),
    ]
    assert FIGURE_LISTING_MARKER not in handed
    assert "carry a node's figures through every launch path" in handed


def test_a_figureless_dispatch_writes_a_prompt_with_no_figure_listing(
    config: dict, repo: Path, tmp_path: Path
) -> None:
    """A node carrying no figures composes no listing, so the marker is absent
    because this dispatch had nothing to name rather than because its lane is
    one that names no figures."""
    record = dispatch_module.dispatch(
        node=_node(tmp_path, []),
        project=PROJECT,
        repo=repo,
        config=config,
        session="fig-none",
        check_budget=False,
        backend_override="clive",
        launcher=_Recorder(),
    )
    _CREATED_RUN_IDS.add(str(record["run_id"]))
    handed = _worker_prompt(str(record["run_id"]))

    assert handed.strip()
    assert FIGURE_LISTING_MARKER not in handed
    assert not (crew.run_dir(record["run_id"]) / "image-0.png").exists()


def test_an_in_harness_dispatch_writes_the_uncomposed_prompt_file(
    config: dict, repo: Path, tmp_path: Path
) -> None:
    """An in-harness launch composes no plan — reckon cannot spawn the calling
    harness's own delegation primitive — so there is no composition for this
    file to be written from, and it keeps the prompt dispatch composed itself.
    A node carrying figures must not raise here: those paths reach a plan this
    path never builds."""
    _write_figure_sources(repo)
    embedded = {
        **config,
        "backends": {
            **config["backends"],
            "embedded": {
                "launch": "in-harness",
                "model": "embedded-model",
                "effort": "high",
                "sandbox": "worktree-full",
                "time_budget": "25m",
            },
        },
    }
    record = dispatch_module.dispatch(
        node=_node(tmp_path, [FIRST, SECOND]),
        project=PROJECT,
        repo=repo,
        config=embedded,
        session="fig-native",
        check_budget=False,
        backend_override="embedded",
        route="deterministic",
    )
    _CREATED_RUN_IDS.add(str(record["run_id"]))
    handed = _worker_prompt(str(record["run_id"]))

    assert record["launch"] == "in-harness"
    assert handed.strip()
    assert FIGURE_LISTING_MARKER not in handed
    assert "carry a node's figures through every launch path" in handed
