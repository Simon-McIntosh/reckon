"""The srun/sbatch/salloc shims refuse an implicit nested launch.

Every case drives the real shim executable, with the fixture facts injected by
a ``sitecustomize`` the test writes into a temporary directory, so no test
reads the real ``/proc``. The shim directory is put on the fixture ``PATH`` and
a fake real binary records the argv it was handed, which is how the exec paths
are told from the refusal path.

The declared mutation — remove the in-allocation check — is loaded here and run
by ``docs/figures/a-worker-knows-it-is-on-compute/`` against the refusal test.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path

import pytest

from reckon.nested_launch import REFUSAL_STATUS

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIM_DIR = REPO_ROOT / "reckon" / "host_shims"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
TOOLS = ("srun", "sbatch", "salloc")

# The fact injection: install the fixture facts before the shim calls main, and
# pop its own markers so the fake binary the shim execs into never sees them.
# ``NESTED_LAUNCH_TEST_MODULE`` swaps in the mutant for the mutation check.
_INJECTION = '''"""Test-only: install fixture host facts into reckon.nested_launch."""
import os
import sys

_marker = os.environ.pop("NESTED_LAUNCH_TEST_FACTS", None)
_module_path = os.environ.pop("NESTED_LAUNCH_TEST_MODULE", None)
if _marker is not None:
    if _module_path:
        import importlib.util

        _spec = importlib.util.spec_from_file_location(
            "reckon.nested_launch", _module_path
        )
        _module = importlib.util.module_from_spec(_spec)
        sys.modules["reckon.nested_launch"] = _module
        _spec.loader.exec_module(_module)
    else:
        import reckon.nested_launch as _module

    from reckon.host import HostFacts

    _inside = _marker == "inside"
    _module.host_facts = lambda **kwargs: HostFacts(
        in_allocation=_inside,
        job_id="4242" if _inside else None,
        step_id=None,
        node="98dci4-clu-2058" if _inside else None,
        tmp_filesystem="xfs",
        home_filesystem="gpfs",
        tmp_is_node_local=True,
        reason="" if _inside else "no-slurm-job-id-in-the-environment",
        sources={},
    )
'''

# A fake real scheduler tool: append its full path then its argv, one per line,
# each invocation terminated by ``---``. The full path is recorded so a case
# can prove the shim directory was dropped from the lookup, not re-exec'd.
_FAKE = """#!/bin/sh
{
  printf '%s\\n' "$0"
  printf '%s\\n' "$@"
  printf '%s\\n' "---"
} >> "$FAKE_LAUNCH_RECORD"
exit "${FAKE_LAUNCH_EXIT:-0}"
"""

# The mutation the runner under docs/figures/ loads. The mutation removes the
# refusal gate in ``main``, so a plain srun inside the allocation execs instead
# of being refused. The anchor must stay in step with reckon/nested_launch.py.
DECLARED_MUTATION = "remove the in-allocation check so plain srun always execs"
_MUTATION_ANCHOR = (
    "    if refuses_implicit_launch(tool, argv, facts, env):\n"
    "        print(refusal_message(tool, facts), file=sys.stderr)\n"
    "        return REFUSAL_STATUS\n"
)
_MUTATION_REPLACEMENT = ""


@dataclasses.dataclass
class Fixture:
    """A fake real scheduler tool, the fact injection, and the argv record."""

    fake_bin: Path
    injection: Path
    record: Path


def make_fixture(root: Path) -> Fixture:
    fake_bin = root / "fake-real"
    fake_bin.mkdir(parents=True, exist_ok=True)
    injection = root / "injection"
    injection.mkdir(parents=True, exist_ok=True)
    record = root / "argv-record.txt"
    for tool in TOOLS:
        path = fake_bin / tool
        path.write_text(_FAKE, encoding="utf-8")
        path.chmod(0o755)
    (injection / "sitecustomize.py").write_text(_INJECTION, encoding="utf-8")
    return Fixture(fake_bin=fake_bin, injection=injection, record=record)


def launch_env(
    fixture: Fixture,
    *,
    inside: bool,
    override: Path | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("RECKON_ALLOW_NESTED_LAUNCH", None)
    env["PATH"] = os.pathsep.join(
        [str(fixture.fake_bin), str(SHIM_DIR), env.get("PATH", "")]
    )
    env["PYTHONPATH"] = os.pathsep.join([str(fixture.injection), str(REPO_ROOT)])
    env["NESTED_LAUNCH_TEST_FACTS"] = "inside" if inside else "outside"
    env["FAKE_LAUNCH_RECORD"] = str(fixture.record)
    if override is not None:
        env["NESTED_LAUNCH_TEST_MODULE"] = str(override)
    if extra:
        env.update(extra)
    return env


def run_shim(
    tool: str, argv: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SHIM_DIR / tool), *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def recorded(record: Path) -> list[tuple[str, tuple[str, ...]]]:
    if not record.exists():
        return []
    invocations: list[tuple[str, tuple[str, ...]]] = []
    current: list[str] = []
    for line in record.read_text(encoding="utf-8").splitlines():
        if line == "---":
            if current:
                invocations.append((current[0], tuple(current[1:])))
            current = []
        else:
            current.append(line)
    return invocations


def load_declared_mutant(scratch: Path) -> Path:
    source = (REPO_ROOT / "reckon" / "nested_launch.py").read_text(encoding="utf-8")
    if _MUTATION_ANCHOR not in source:
        raise AssertionError(
            "the in-allocation check is not where the mutation expects it"
        )
    mutated = source.replace(_MUTATION_ANCHOR, _MUTATION_REPLACEMENT)
    if mutated == source:
        raise AssertionError("the mutation changed nothing")
    path = scratch / "nested_launch_mutant.py"
    path.write_text(mutated, encoding="utf-8")
    return path


# --- the shims are the executables the section names -------------------------


def test_the_three_shims_are_executable() -> None:
    for tool in TOOLS:
        path = SHIM_DIR / tool
        assert path.is_file(), f"{tool} shim is missing"
        assert os.access(path, os.X_OK), f"{tool} shim is not executable"


def test_the_fixture_install_is_live(tmp_path: Path) -> None:
    """The injection replaces host_facts, so the module reads no real /proc."""
    fixture = make_fixture(tmp_path)
    env = launch_env(fixture, inside=True)
    probe = (
        "import reckon.nested_launch as m; "
        "f = m.host_facts(); "
        "print(int(f.in_allocation), f.job_id, f.node)"
    )
    result = subprocess.run(
        [str(VENV_PYTHON), "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1 4242 98dci4-clu-2058"


# --- the decision, driven through the shim executable ------------------------


def test_plain_srun_inside_the_allocation_is_refused(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    result = run_shim("srun", ["true"], launch_env(fixture, inside=True))
    assert result.returncode == REFUSAL_STATUS, result.stderr
    assert "4242" in result.stderr
    assert "98dci4-clu-2058" in result.stderr
    assert "RECKON_ALLOW_NESTED_LAUNCH=1" in result.stderr
    assert recorded(fixture.record) == []


def test_srun_overlap_with_an_explicit_job_runs(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    result = run_shim(
        "srun", ["--overlap", "--jobid=4242", "true"], launch_env(fixture, inside=True)
    )
    assert result.returncode == 0, result.stderr
    assert recorded(fixture.record) == [
        (str(fixture.fake_bin / "srun"), ("--overlap", "--jobid=4242", "true"))
    ]


def test_srun_overlap_with_one_task_runs(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    result = run_shim(
        "srun", ["--overlap", "-n", "1", "true"], launch_env(fixture, inside=True)
    )
    assert result.returncode == 0, result.stderr
    assert recorded(fixture.record) == [
        (str(fixture.fake_bin / "srun"), ("--overlap", "-n", "1", "true"))
    ]


def test_srun_outside_the_allocation_runs_with_identical_argv(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    result = run_shim("srun", ["true"], launch_env(fixture, inside=False))
    assert result.returncode == 0, result.stderr
    invocations = recorded(fixture.record)
    assert invocations == [(str(fixture.fake_bin / "srun"), ("true",))]
    # The lookup dropped the shim directory, so the fake on PATH is what ran.
    assert invocations[0][0].startswith(str(fixture.fake_bin))


def test_the_override_discharges_the_refusal(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    env = launch_env(fixture, inside=True, extra={"RECKON_ALLOW_NESTED_LAUNCH": "1"})
    result = run_shim("srun", ["true"], env)
    assert result.returncode == 0, result.stderr
    assert recorded(fixture.record) == [(str(fixture.fake_bin / "srun"), ("true",))]


@pytest.mark.parametrize("tool", ["sbatch", "salloc"])
def test_sbatch_and_salloc_are_refused_inside(tmp_path: Path, tool: str) -> None:
    fixture = make_fixture(tmp_path)
    result = run_shim(tool, ["true"], launch_env(fixture, inside=True))
    assert result.returncode == REFUSAL_STATUS, result.stderr
    assert "RECKON_ALLOW_NESTED_LAUNCH=1" in result.stderr
    assert "4242" in result.stderr
    assert recorded(fixture.record) == []


def test_the_declared_mutation_makes_the_refusal_go_away(tmp_path: Path) -> None:
    """The mutant of the declared mutation execs where the real module refuses.

    This asserts the mutation's effect directly; the runner under docs/figures/
    turns it into the red log by running the refusal test against this mutant.
    """
    fixture = make_fixture(tmp_path)
    mutant = load_declared_mutant(tmp_path)
    result = run_shim(
        "srun", ["true"], launch_env(fixture, inside=True, override=mutant)
    )
    assert result.returncode == 0, result.stderr
    assert recorded(fixture.record) == [(str(fixture.fake_bin / "srun"), ("true",))]
