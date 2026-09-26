"""A fenced launch resolves the backend behind the fence, not the fence itself.

The launcher composes a worker argv as ``<fence> <binds...> -- <harness> ...``
when the fence is on. Launch resolution read the composed argv's first element,
which is the fence binary, so it resolved the fence — present on every host able
to launch — and accepted a plan whose backend was absent, while a present
backend was never rewritten to an unfenced absolute path. The launch then died
behind the fence at exec with nothing recording why.

These cases compose the plan through the production dispatch path so the
composition under test is the one a worker is launched with, and they state the
fence binary as a positive control: it is what argv[0] names and what the
resolution must skip.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")
backends_module = importlib.import_module("reckon._backends")

MISSING_PATH = "/nonexistent-backend-bin"
FENCE_BINARY = backends_module.FENCE_BINARY


def _backend_environment(path: str) -> dict:
    return {"PATH": path}


def _config(environment: dict) -> dict:
    return {
        "default_backend": "alpha",
        "backends": {
            "alpha": {
                "launch": "cli",
                "command": "codex",
                "model": "some-model",
                "effort": "high",
                "sandbox": "worktree-full",
                "session_reuse": True,
                "time_budget": "25m",
                "environment": environment,
            }
        },
        "roles": {"implement": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A hermetic mount carrying a plan, a git checkout and a flight config."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / "fixture.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
</head><body><h2 id="guard">Dispatch guard</h2></body></html>
""",
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo


def _node(name: str, manifest_path: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=f"node-{name}",
        goal="resolve the backend before the launch writes anything",
        plan="fixture",
        section="guard",
        spec_level="guided",
        done_when="one launch resolution case passes",
        write_paths=["src/resolved.txt"],
        time_budget="20m",
        manifest_path=manifest_path,
    )


def _admit_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the seat only; delivery is read from the real registry."""
    watcher = {"arming_line": "reckon crew watch --project sample", "watcher": {}}

    def watch_state(_project: str, *, session: str | None = None) -> dict:
        delivery = (
            runs.follower_state(_project, session) if session is not None else None
        )
        return {
            **watcher,
            "attach_line": runs._watch_attach_line(_project, session=session),
            "watcher_live": True,
            "session": session,
            "session_attached": None if delivery is None else bool(delivery["live"]),
            "follower": {} if delivery is None else delivery["follower"],
        }

    monkeypatch.setattr(dispatch_module, "watch_state", watch_state)


def _dispatch(
    repo: Path,
    config: dict,
    monkeypatch: pytest.MonkeyPatch,
    *,
    launcher=None,
) -> dict:
    _admit_watcher(monkeypatch)
    session = "session-fenced-resolve"
    seen: dict = {}

    def capture(plan, **_kwargs):
        seen["plan"] = plan
        return launcher(plan) if launcher is not None else 4242

    with runs.follower_claim("sample", session, delivery="stream"):
        crew.dispatch(
            node=_node("resolve", str(repo.parent / "manifest.md")),
            project="sample",
            repo=repo,
            config=config,
            session=session,
            launcher=capture,
            watch_required=False,
        )
    return seen


def _fence_separator(argv: list) -> int:
    """The index of the fence's own ``--`` separator, after its first element."""
    assert Path(str(argv[0])).name == FENCE_BINARY
    return argv.index("--", 1)


def test_a_fenced_plan_refuses_an_absent_backend_naming_the_backend_not_the_fence(
    project: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The absent backend is refused by name; the fence is never taken for it."""
    _config_home, repo = project
    # A search path that carries the fence binary and *only* it: the fence this
    # composition leads with is present, the backend is not. A resolver that
    # read argv[0] would therefore resolve the fence and accept the plan, so
    # the refusal below is the absent backend rather than a PATH that had
    # nothing on it.
    fence_bin = tmp_path / "fence-bin"
    fence_bin.mkdir()
    real_fence = shutil.which(FENCE_BINARY)
    assert real_fence is not None, "the host carries the fence binary"
    (fence_bin / FENCE_BINARY).symlink_to(real_fence)

    environment = _backend_environment(str(fence_bin))
    searched = dispatch_module.launch_search_path(environment)
    assert shutil.which(FENCE_BINARY, path=searched) is not None
    assert shutil.which("codex", path=searched) is None

    with pytest.raises(crew.CrewError) as refusal:
        _dispatch(repo, _config(environment), monkeypatch)

    message = str(refusal.value)
    assert "codex" in message
    assert FENCE_BINARY not in message
    assert list(runs.live_dir().glob("*.json")) == []
    assert not runs.runs_dir().exists() or list(runs.runs_dir().iterdir()) == []


def test_a_fenced_plan_carries_the_backend_absolute_path_behind_the_separator(
    project: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present backend is rewritten to its absolute path, behind the fence."""
    _config_home, repo = project
    bin_dir = tmp_path / "backend-bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)

    seen = _dispatch(repo, _config(_backend_environment(str(bin_dir))), monkeypatch)

    argv = list(seen["plan"].argv)
    separator = _fence_separator(argv)
    harness = Path(str(argv[separator + 1]))
    assert harness.is_absolute()
    assert harness == fake.resolve()
    assert harness.name == "codex"
    # The fence element is still the composition's head, unmoved by resolution.
    assert Path(str(argv[0])).name == FENCE_BINARY


def test_an_unfenced_plan_resolves_its_backend_exactly_as_before(
    project: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """With no fence, argv[0] is the backend and is resolved as it always was."""
    _config_home, repo = project
    bin_dir = tmp_path / "backend-bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)

    plan = backends_module.launch_plan(
        backend_name="alpha",
        backend=_config(_backend_environment(str(bin_dir)))["backends"]["alpha"],
        prompt="do the node",
        worktree=repo,
        manifest_path=str(repo.parent / "manifest.md"),
        fence=False,
    )
    resolved = dispatch_module.resolve_launch_executable(plan)

    assert Path(str(resolved.argv[0])) == fake.resolve()
    assert resolved.argv[0] == str(fake)
    assert FENCE_BINARY not in resolved.argv
