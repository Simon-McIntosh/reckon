"""The reach reader resolves entry points, imports and subprocess launches.

Each case builds a throwaway git repository holding a ``reckon/`` package and a
``pyproject.toml``, so the reader is exercised from the one production entry
point that repository declares, at a real revision. The module index reads the
tree with ``ast`` and never imports it, so a fixture module is never executed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from reckon.served_code import neighbourhood, package_modules, reached


def _build_repo(root: Path, files: dict[str, str]) -> Path:
    """Write ``files`` under ``root`` and commit them at ``HEAD``."""

    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    identity = ["-c", "user.email=t@example.invalid", "-c", "user.name=test"]
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), *identity, "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), *identity, "commit", "-qm", "fixture"], check=True
    )
    return root


def _base_files() -> dict[str, str]:
    return {
        "pyproject.toml": (
            "[project]\n"
            'name = "fixture"\n'
            'version = "0"\n'
            "\n"
            "[project.scripts]\n"
            'fixture = "reckon.a:main"\n'
        ),
        "reckon/__init__.py": "",
        "reckon/a.py": (
            "import reckon.b\n\n\ndef main():\n    return reckon.b.helper()\n"
        ),
        "reckon/b.py": "def helper():\n    return 1\n",
        "reckon/c.py": "def orphan():\n    return 2\n",
    }


def test_console_script_reaches_its_import_closure(tmp_path: Path):
    repo = _build_repo(tmp_path / "repo", _base_files())

    reach = reached(str(repo), "HEAD")

    assert "reckon.a" in reach.modules
    assert "reckon.b" in reach.modules
    assert "reckon.c" not in reach.modules


def test_unimported_module_is_reported_unreached(tmp_path: Path):
    repo = _build_repo(tmp_path / "repo", _base_files())

    reach = reached(str(repo), "HEAD")
    every = package_modules(str(repo), "HEAD")

    assert "reckon.c" in every
    assert "reckon.c" not in reach.modules


def test_neighbourhood_returns_the_importer(tmp_path: Path):
    repo = _build_repo(tmp_path / "repo", _base_files())

    adjacent = neighbourhood(str(repo), "HEAD", ["b"])

    assert "reckon.a" in adjacent
    assert "reckon.b" not in adjacent


def test_subprocess_launch_is_an_entry_point(tmp_path: Path):
    """A module named only in a ``python -m`` source string is reached."""

    files = _base_files()
    files["reckon/a.py"] = (
        '"""Launched by the harness as python -m reckon.d."""\n'
        "import reckon.b\n"
        "\n"
        "\n"
        "def main():\n"
        "    return reckon.b.helper()\n"
    )
    files["reckon/d.py"] = "def entry():\n    return 3\n"
    repo = _build_repo(tmp_path / "repo", files)

    reach = reached(str(repo), "HEAD")

    assert "reckon.d" in reach.modules
    assert "reckon.d" in reach.roots
