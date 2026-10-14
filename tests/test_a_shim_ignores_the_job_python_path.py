"""A shim imports reckon's packages whatever Python path its job loaded.

A SLURM job that ran ``module load`` puts its own site-packages on
``PYTHONPATH``. The shims used to append that caller path to reckon's import
root, so a 3.11 copy of a dependency shadowed the one in reckon's own
environment and the import failed before git or the scheduler was reached.
Each shim now starts reckon's interpreter with only reckon's import root on its
path and hands the caller's environment back to the program it runs.

Every case drives the real shim executable with ``PYTHONPATH`` and
``PYTHONHOME`` naming a temporary directory whose ``typing_extensions.py``
raises on import — the shape of the job that broke — and a fake real tool on
``PATH`` that records the environment it was handed. A git command through the
worker shim succeeds, each host shim reaches its main, and the program the shim
runs still sees the caller's ``PYTHONPATH`` and ``PYTHONHOME`` unchanged.

The negative control the file declares restores the appended caller
``PYTHONPATH`` in the shims; the cases below then fail on the import error the
hostile directory raises. ``mutate_shim_source`` applies it, and the control is
logged under the run directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
WORKER_SHIM_DIR = REPO_ROOT / "reckon" / "worker_shims"
HOST_SHIM_DIR = REPO_ROOT / "reckon" / "host_shims"
GIT_SHIM = WORKER_SHIM_DIR / "git"
HOST_TOOLS = ("salloc", "sbatch", "srun")

RUN_ID = "r-a-shim-ignores-the-job-python-path"

# The mutation this file declares. It restores the append the shims used to
# write — the caller's PYTHONPATH placed after reckon's import root — so the
# hostile directory comes back onto the interpreter's path and the import
# error returns.
DECLARED_MUTATION = (
    "with the appended caller PYTHONPATH restored in the shims, the new test "
    "must fail with the import error"
)

# The fixed and buggy forms of the two lines the mutation rewrites, kept in
# step with the four shims. The exec line is the one every shim carries; the
# unset line is what keeps the caller's PYTHONPATH out of the interpreter.
_FIXED_EXEC = 'PYTHONPATH="$repo_root" \\'
_BUGGY_EXEC = 'PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}" \\'
_FIXED_UNSET = "unset PYTHONPATH PYTHONHOME PYTHONSTARTUP"
_BUGGY_UNSET = "unset PYTHONHOME PYTHONSTARTUP"


def mutation_names() -> tuple[str, ...]:
    """The mutations this file declares, in the order a runner reads them."""
    return (DECLARED_MUTATION,)


def mutate_shim_source(text: str) -> str:
    """Return ``text`` with the appended caller ``PYTHONPATH`` restored.

    The mutation leaves the caller's ``PYTHONPATH`` in the environment and puts
    it back on the interpreter's path, which is the defect the shims now
    avoid. The anchors must stay in step with the four shim files.
    """
    if _FIXED_EXEC not in text or _FIXED_UNSET not in text:
        raise AssertionError("the code the mutation targets is not where it expects it")
    mutated = text.replace(_FIXED_UNSET, _BUGGY_UNSET).replace(_FIXED_EXEC, _BUGGY_EXEC)
    if mutated == text:
        raise AssertionError("the mutation changed nothing")
    return mutated


# The hostile dependency: any interpreter that imports it fails, which is the
# failure the job's Python-3.11 copy of typing_extensions produced.
_HOSTILE = 'raise ImportError("hostile typing_extensions on the job PYTHONPATH")\n'

# A fake real tool that records the environment the shim handed it. ``%s`` is
# its own name so one body serves git and each scheduler tool.
_FAKE = """#!/bin/sh
printf 'TOOL=%s\\n' "$(basename "$0")"
printf 'PYTHONPATH=%s\\n' "${PYTHONPATH-<unset>}"
printf 'PYTHONHOME=%s\\n' "${PYTHONHOME-<unset>}"
"""


def _real_git() -> str:
    """The real git on ``PATH`` that is not a reckon shim."""
    from reckon.shim_lookup import real_executable

    found = real_executable("git", os.environ.get("PATH", ""))
    assert found, "no real git on PATH that is not a reckon shim"
    return found


def make_hostile(root: Path) -> Path:
    """A directory whose ``typing_extensions`` raises on import."""
    hostile = root / "hostile"
    hostile.mkdir(parents=True, exist_ok=True)
    (hostile / "typing_extensions.py").write_text(_HOSTILE, encoding="utf-8")
    return hostile


def make_fake_bin(root: Path) -> Path:
    """A directory of fake real tools that print the environment they receive."""
    fake = root / "fake-bin"
    fake.mkdir(parents=True, exist_ok=True)
    for name in ("git", *HOST_TOOLS):
        path = fake / name
        path.write_text(_FAKE, encoding="utf-8")
        path.chmod(0o755)
    return fake


def base_env(root: Path, hostile: Path) -> dict[str, str]:
    """A minimal environment whose interpreter path is hostile to reckon.

    ``PYTHONPATH`` and ``PYTHONHOME`` both name the raising directory, so a
    shim that lets either reach its interpreter fails before reaching its main.
    """
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.defpath,
        "HOME": str(home),
        "RECKON_HOME": str(home),
        "RECKON_SHIM_PYTHON": str(PYTHON),
        "PYTHONPATH": str(hostile),
        "PYTHONHOME": str(hostile),
        "LC_ALL": "C",
    }


def run_shim(
    shim: Path, argv: list[str], env: dict[str, str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(shim), *argv],
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _recorded(stdout: str) -> dict[str, str]:
    """The environment the fake tool printed, as a mapping."""
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key] = value
    return fields


# ── the four shims carry the fixed form the mutation targets ────────────────


def test_every_shim_carries_the_isolated_exec_line() -> None:
    shims = [GIT_SHIM, *(HOST_SHIM_DIR / tool for tool in HOST_TOOLS)]
    for shim in shims:
        text = shim.read_text(encoding="utf-8")
        assert _FIXED_EXEC in text, f"{shim} does not start reckon with only its root"
        assert _FIXED_UNSET in text, f"{shim} does not keep the caller path aside"
        assert _BUGGY_EXEC not in text, f"{shim} still appends the caller PYTHONPATH"
        # The mutation the control applies must actually change each shim.
        assert mutate_shim_source(text) != text


# ── the worker shim: a git command succeeds and the child env is the caller's ─


def test_a_git_command_succeeds_under_a_hostile_python_path(tmp_path: Path) -> None:
    hostile = make_hostile(tmp_path)
    fake = make_fake_bin(tmp_path)
    env = base_env(tmp_path, hostile)
    env["PATH"] = os.pathsep.join([str(fake), str(WORKER_SHIM_DIR), os.defpath])

    result = run_shim(GIT_SHIM, ["status", "--short"], env)

    assert result.returncode == 0, result.stderr
    fields = _recorded(result.stdout)
    assert fields.get("TOOL") == "git", result.stdout
    assert fields.get("PYTHONPATH") == str(hostile), result.stdout
    assert fields.get("PYTHONHOME") == str(hostile), result.stdout


def test_git_reaches_its_main_and_forwards_with_reckon_imported(
    tmp_path: Path,
) -> None:
    """A run-scoped mutating git command imports reckon.crew and still succeeds.

    The shim resolves the run's worktree from the live pointer before it
    forwards, which is where an import of ``reckon.crew`` happens; a hostile
    interpreter path would break the import there rather than at startup.
    """
    hostile = make_hostile(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    real_git = _real_git()
    subprocess.run([real_git, "init", "-q", str(worktree)], check=True)

    home = tmp_path / "home"
    live = home / "crew" / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / f"{RUN_ID}.json").write_text(
        json.dumps({"worktree": str(worktree)}), encoding="utf-8"
    )

    env = base_env(tmp_path, hostile)
    env["RECKON_RUN_ID"] = RUN_ID
    env["RECKON_HOME"] = str(home)
    env["PATH"] = os.pathsep.join(
        [os.path.dirname(real_git), str(WORKER_SHIM_DIR), os.defpath]
    )

    result = run_shim(GIT_SHIM, ["add", "-A"], env, cwd=worktree)

    assert result.returncode == 0, result.stderr
    assert "ImportError" not in result.stderr, result.stderr


# ── each host shim reaches its main and the child env is the caller's ────────


def test_each_host_shim_reaches_its_main_under_a_hostile_python_path(
    tmp_path: Path,
) -> None:
    hostile = make_hostile(tmp_path)
    fake = make_fake_bin(tmp_path)
    for tool in HOST_TOOLS:
        env = base_env(tmp_path, hostile)
        env["PATH"] = os.pathsep.join([str(fake), str(HOST_SHIM_DIR), os.defpath])
        # The discharge the shim names: forward rather than refuse, so the fake
        # real tool runs and its recorded environment can be read back.
        env["RECKON_ALLOW_NESTED_LAUNCH"] = "1"

        result = run_shim(HOST_SHIM_DIR / tool, ["--version"], env)

        assert result.returncode == 0, f"{tool}: {result.stderr}"
        fields = _recorded(result.stdout)
        assert fields.get("TOOL") == tool, f"{tool}: {result.stdout}"
        assert fields.get("PYTHONPATH") == str(hostile), f"{tool}: {result.stdout}"
        assert fields.get("PYTHONHOME") == str(hostile), f"{tool}: {result.stdout}"
