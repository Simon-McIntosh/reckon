"""A gate submitted from inside a fenced worker starts on its partition's node.

Measured on 19 November in run
``r-20261118T063008830158-qwnw-red-file-every-path-meets-the-node-contract``:
every submission of the node's gate to ``all_debug`` ended before pytest
started, and the worker measured its head on the fleet node instead. A worker
that follows its brief then has no measurement taken on the partition its
done-when names.

A submitted job's task re-enters the worker's fence on whichever node the
scheduler chose, and the fence's mount recipe is composed on the node the
worker runs on. One source in it belongs to a node rather than to the run: the
run's own scratch directory, which the worker's fence mounts over its ``/tmp``.
A job on another node has none of it, and an empty temp directory of the job's
own is exactly what that mount should supply, so the recipe names it apart from
every other source as ``temp_overlay``.

What the recipe must not do is invent a directory for a source that is not the
task's temp. A mount the job node cannot supply is a fence the job cannot
enter, and running the task against a synthesised empty directory would measure
the sandbox instead of the tree the gate was told to measure. So every other
absent source refuses before the task starts, naming each path and the node it
could not be supplied on — which bubblewrap's own message cannot do, since it
prints only the first source it could not find and no node at all:

    bwrap: Can't find source path /tmp/.../probe-temp-dir: No such file or directory

The tests execute the task boundary an ``sbatch`` job runs (the carried recipe
is decoded and the fence started by the same bootstrap the scheduler launches),
so neither assertion reads a string for the refusal, and the second one proves
the task runs rather than reading the first one's absence.

The declared negative control restores the temp-directory bind to its base
form, where the recipe named no ``temp_overlay`` and every absent directory
source was simply supplied empty: the task's own temp directory then reads as
an unbindable source, and the test that starts a sandboxed command against it
fails with the bind-more refusal naming that directory. It is applied by
running with ``RECKON_TEST_GATE_BIND_NEGATIVE=1``.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from reckon import _worker_fence, nested_launch

NEGATIVE_CONTROL_MUTATION = (
    "with the temp-directory bind restored to its base form — the source check "
    "removed from the task bootstrap — the test that starts a sandboxed command "
    "against a node-local temp directory fails, bubblewrap refusing the absent "
    "source with its own bind-mount error"
)
NEGATIVE_CONTROL = os.environ.get("RECKON_TEST_GATE_BIND_NEGATIVE") == "1"

_TASK_OUTPUT = "RAN"


def test_the_composed_recipe_names_the_node_temps_bind(tmp_path, monkeypatch):
    """The production composition emits ``temp_overlay`` for the run's scratch.

    The other two tests hand the recipe in, so they guard the task's reading of
    it and say nothing about the fence that writes it. This one composes the
    worker's fence through :func:`reckon._worker_fence.fence_argv` with the
    run's own scratch directory granted, decodes the carried recipe from the
    argv the launch would run, and asserts the temp bind is named there — with
    the source the fence actually mounts over the node tmp and the destination
    it mounts it on. Delete the emission and this fails while the argv still
    composes.
    """
    root = tmp_path / "reckon-crew-scratch"
    own = root / "run-under-test"
    monkeypatch.setattr(_worker_fence, "NODE_TMP_ROOT", tmp_path)
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(root))

    argv = _worker_fence.fence_argv(["true"], writable_directories=[str(own)])

    recipe = _carried_recipe(argv)
    bound = Path(own).resolve()
    assert recipe["temp_overlay"] == {
        "source": str(bound),
        "destination": str(Path(tmp_path).resolve()),
    }
    assert f"--bind {bound} {Path(tmp_path).resolve()}" in " ".join(argv)
    assert str(bound) in recipe["directory_sources"]


def _carried_recipe(argv: list[str]) -> dict:
    """Decode the fence recipe a composed argv carries to its task."""
    for index, word in enumerate(argv):
        if word == "--setenv" and argv[index + 1] == "RECKON_WORKER_FENCE":
            return json.loads(base64.urlsafe_b64decode(argv[index + 2]))
    raise AssertionError("the composed fence carries no recipe")


def _bwrap() -> str:
    found = shutil.which("bwrap")
    if found is None:
        pytest.skip("no fence program on this node")
    return os.path.abspath(found)


def _recipe(
    tmp_path: Path, temp_source: Path, destination: Path, *, classified: bool = True
) -> dict:
    """A carried fence whose one directory source is ``temp_source``.

    The shape is the composed recipe's: the fence program is carried by absolute
    path, the bind triples are the fence's own, and ``directory_sources`` names
    the sources the composing host knows are directories — the ones the task may
    supply empty. A source the compose host did not classify is a mount the job
    node cannot hold, and is what the refusal is for.
    """
    recipe = {
        "argv": [
            _bwrap(),
            "--dev-bind",
            "/",
            "/",
            "--bind",
            str(temp_source),
            str(destination),
        ],
        "program": _bwrap(),
        "carried_program": None,
        "directories": [],
        "directory_sources": [str(temp_source)] if classified else [],
        "scratch": str(tmp_path),
        "limit": "test composition",
    }
    if classified:
        recipe["temp_overlay"] = {
            "source": str(temp_source),
            "destination": str(destination),
        }
    return recipe


def _bootstrap() -> str:
    """The task bootstrap, or its base form under the declared mutation.

    The base form is the bootstrap before the source check existed: an absent
    source reaches the fence and bubblewrap refuses it, printing the source it
    could not find and never a node.
    """
    text = nested_launch._TASK_REENTRY
    if not NEGATIVE_CONTROL:
        return text
    start = text.index("# Every source the inherited fence mounts")
    end = text.index("script = None", start)
    return text[:start] + text[end:]


def _run_task(
    tmp_path: Path, recipe: dict, command: list[str]
) -> subprocess.CompletedProcess:
    spec = {"fence": recipe, "command": command, "env": "RECKON_WORKER_FENCE"}
    encoded = base64.urlsafe_b64encode(json.dumps(spec).encode()).decode()
    bootstrap = _bootstrap()
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            bootstrap,
            bootstrap,
            encoded,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_a_temp_directory_the_job_node_cannot_supply_is_named(tmp_path):
    """A source that is not the task's temp refuses by path and by node.

    The directory exists nowhere on this host, as a worker's own temp does not
    exist on the node a submitted job lands on. Every other mount would be
    invented as an empty directory, so this one must refuse and name it.
    """
    absent = tmp_path / "worker-only-temp"
    destination = tmp_path / "destination"
    destination.mkdir()
    recipe = _recipe(tmp_path, absent, destination, classified=False)

    result = _run_task(tmp_path, recipe, ["/bin/sh", "-c", f"echo {_TASK_OUTPUT}"])

    assert result.returncode == nested_launch.FENCE_SOURCE_STATUS, result.stderr
    assert _TASK_OUTPUT not in result.stdout
    assert str(absent) in result.stderr
    assert socket.gethostname() in result.stderr


def test_the_tasks_own_temp_directory_stands_in_and_the_task_runs(tmp_path):
    """The temp overlay is supplied empty, and the task's command runs under it.

    An absent temp directory is the one source the job runs with a fresh empty
    one: the notice names the directory and this node, and the fence still
    enters, so a gate submission starts here rather than refusing.
    """
    absent = tmp_path / "worker-only-temp"
    destination = tmp_path / "destination"
    destination.mkdir()
    recipe = _recipe(tmp_path, absent, destination)

    result = _run_task(tmp_path, recipe, ["/bin/sh", "-c", f"echo {_TASK_OUTPUT}"])

    assert result.returncode == 0, result.stderr
    assert _TASK_OUTPUT in result.stdout
    assert str(absent) in result.stderr
    assert socket.gethostname() in result.stderr
