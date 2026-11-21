"""A process running from a reload image names shims a worker's node can read.

A self-reloading service imports the package from a published reload image on
its own node's local disk. A worker it launches may be placed on another node,
which holds no such image. The fence binds each scheduler shim from the
directory :func:`reckon.nested_launch.shim_directory` returns, and the worker's
search path starts with the git shim directory
:func:`reckon.worker_git_shim.worker_shim_directory` returns; a source the
worker's node lacks refuses the launch before the worker starts, and a git shim
script on the launching node's disk cannot run on the worker's node at all. So
the directory named from inside an image is the one in the checkout the image
was published from, and a process running from a checkout keeps its own.

Both families ask the one rule, :func:`reckon.nested_launch.shims_beside`, and
each is held to the same four cases: an image naming the checkout from the root
its launcher carried, an image naming it from the interpreter's checkout, an
image keeping its own shims when no checkout is found, and a checkout keeping
its own whatever root is carried.

Each case builds its image and its checkout under the case's own directory and
imports a copy of the module from there, so nothing outside it is read.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from reckon import nested_launch, worker_git_shim

IMAGE_NAME = "a" * 40


def _tree(root: Path) -> Path:
    """A minimal package tree holding the module and a shim directory."""
    package = root / "reckon"
    (package / "host_shims").mkdir(parents=True)
    (package / "host_shims" / "sbatch").write_text("#!/bin/sh\n", encoding="utf-8")
    shutil.copy(nested_launch.__file__, package / "nested_launch.py")
    return root


def _git_tree(root: Path) -> Path:
    """A minimal package tree holding the git shim module and its shim dir."""
    package = root / "reckon"
    (package / "worker_shims").mkdir(parents=True)
    (package / "worker_shims" / "git").write_text("#!/bin/sh\n", encoding="utf-8")
    shutil.copy(worker_git_shim.__file__, package / "worker_git_shim.py")
    return root


def _published_image(tmp_path: Path) -> Path:
    image = _tree(tmp_path / "images" / IMAGE_NAME)
    (image / ".complete").write_text("", encoding="utf-8")
    return image


def _published_git_image(tmp_path: Path) -> Path:
    image = _git_tree(tmp_path / "images" / IMAGE_NAME)
    (image / ".complete").write_text("", encoding="utf-8")
    return image


def _module_in(root: Path, monkeypatch: pytest.MonkeyPatch):
    """The module as a process importing the package from ``root`` loads it."""
    name = f"nested_launch_under_{root.name}"
    spec = importlib.util.spec_from_file_location(
        name, root / "reckon" / "nested_launch.py"
    )
    module = importlib.util.module_from_spec(spec)
    # A dataclass resolves its own module by name while the class body runs, so
    # the copy is registered for the length of the case before it executes.
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _git_module_in(root: Path, monkeypatch: pytest.MonkeyPatch):
    """The git shim module as a process importing from ``root`` loads it.

    Loaded from the tree rather than from the install, because the directory
    is a function of the file the process is running from.
    """
    name = f"worker_git_shim_under_{root.name}"
    spec = importlib.util.spec_from_file_location(
        name, root / "reckon" / "worker_git_shim.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_an_image_names_the_shims_of_the_git_checkout_its_launcher_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = _published_git_image(tmp_path)
    checkout = _git_tree(tmp_path / "checkout")
    monkeypatch.setenv(nested_launch.SOURCE_ROOT_ENV, str(checkout))

    named = _git_module_in(image, monkeypatch).worker_shim_directory()

    assert named == (checkout / "reckon" / "worker_shims").resolve()
    assert (named / "git").is_file()
    assert image not in named.parents


def test_an_image_without_a_carried_root_names_the_git_checkout_of_the_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = _published_git_image(tmp_path)
    checkout = _git_tree(tmp_path / "checkout")
    monkeypatch.delenv(nested_launch.SOURCE_ROOT_ENV, raising=False)
    monkeypatch.setattr(sys, "prefix", str(checkout / ".venv"))

    named = _git_module_in(image, monkeypatch).worker_shim_directory()

    assert named == (checkout / "reckon" / "worker_shims").resolve()
    assert (named / "git").is_file()


def test_an_image_with_no_git_checkout_to_name_keeps_its_own_shims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = _published_git_image(tmp_path)
    monkeypatch.setenv(nested_launch.SOURCE_ROOT_ENV, str(tmp_path / "absent"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "elsewhere" / ".venv"))

    named = _git_module_in(image, monkeypatch).worker_shim_directory()

    assert named == (image / "reckon" / "worker_shims").resolve()
    assert (named / "git").is_file()


def test_a_checkout_keeps_its_own_git_shims_whatever_root_is_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _git_tree(tmp_path / "checkout")
    other = _git_tree(tmp_path / "other")
    monkeypatch.setenv(nested_launch.SOURCE_ROOT_ENV, str(other))

    named = _git_module_in(checkout, monkeypatch).worker_shim_directory()

    assert named == (checkout / "reckon" / "worker_shims").resolve()
    assert other not in named.parents
    assert (named / "git").is_file()


def test_an_image_names_the_shims_of_the_checkout_its_launcher_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = _published_image(tmp_path)
    checkout = _tree(tmp_path / "checkout")
    monkeypatch.setenv(nested_launch.SOURCE_ROOT_ENV, str(checkout))

    named = _module_in(image, monkeypatch).shim_directory()

    assert named == (checkout / "reckon" / "host_shims").resolve()
    assert image not in named.parents


def test_an_image_without_a_carried_root_names_the_interpreters_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = _published_image(tmp_path)
    checkout = _tree(tmp_path / "checkout")
    monkeypatch.delenv(nested_launch.SOURCE_ROOT_ENV, raising=False)
    monkeypatch.setattr(sys, "prefix", str(checkout / ".venv"))

    named = _module_in(image, monkeypatch).shim_directory()

    assert named == (checkout / "reckon" / "host_shims").resolve()


def test_an_image_with_no_checkout_to_name_keeps_its_own_shims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = _published_image(tmp_path)
    monkeypatch.setenv(nested_launch.SOURCE_ROOT_ENV, str(tmp_path / "absent"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "elsewhere" / ".venv"))

    named = _module_in(image, monkeypatch).shim_directory()

    assert named == (image / "reckon" / "host_shims").resolve()


def test_a_checkout_keeps_its_own_shims_whatever_root_is_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _tree(tmp_path / "checkout")
    other = _tree(tmp_path / "other")
    monkeypatch.setenv(nested_launch.SOURCE_ROOT_ENV, str(other))

    named = _module_in(checkout, monkeypatch).shim_directory()

    assert named == (checkout / "reckon" / "host_shims").resolve()
