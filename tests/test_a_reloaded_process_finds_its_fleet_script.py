"""A reload image carries the script used by its own dispatch code."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from reckon import checkout_trust


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(root: Path, relative: str, content: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(root, "add", relative)
    _git(root, "commit", "-qm", "Change source content")


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(
        source / "reckon",
        root / "reckon",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    script = root / "skills/reckon-build/scripts/worktree_fleet.py"
    script.parent.mkdir(parents=True)
    shutil.copy2(source / "skills/reckon-build/scripts/worktree_fleet.py", script)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Reload Test")
    _git(root, "config", "user.email", "reload@example.invalid")
    _git(root, "add", "reckon", "skills")
    _git(root, "commit", "-qm", "Seed source")
    return root


def _resolve_in_image(image: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(image)
    code = """
from reckon.crew.routing import _fleet_script
from reckon import checkout_trust
print(checkout_trust.__file__)
print(_fleet_script())
"""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_reloaded_package_finds_its_own_fleet_script(
    tmp_path: Path, monkeypatch
) -> None:
    root = _source(tmp_path)
    monkeypatch.setattr(
        checkout_trust, "_committed_tree_cache", lambda: tmp_path / "images"
    )
    if os.environ.get("RECKON_RELOAD_EXTRACT_RECKON_ONLY") == "1":
        monkeypatch.setattr(
            checkout_trust,
            "_archive_source",
            lambda source_root, revision: (
                checkout_trust._reckon_subtree(source_root, revision),
                "reckon/",
            ),
        )
    identity = checkout_trust.source_code_identity(root / "reckon", source_root=root)
    old_identity = _git(root, "rev-parse", "HEAD:reckon")
    assert identity != old_identity
    old_image, _, _, old_pin = checkout_trust.materialize_committed_tree(
        root, old_identity
    )
    os.close(old_pin)
    assert (old_image / "reckon/__init__.py").is_file()
    assert not (old_image / "skills").exists()
    monkeypatch.delenv("RECKON_FOLLOWER_IMAGE_COMMIT", raising=False)
    assert checkout_trust.running_code_identity(old_image / "reckon") == old_identity
    image, _, _, pin = checkout_trust.materialize_committed_tree(root, identity)
    os.close(pin)

    result = _resolve_in_image(image, tmp_path)
    assert result.returncode == 0, result.stderr
    module_path, script_path = result.stdout.splitlines()
    assert Path(module_path).resolve() == (image / "reckon/checkout_trust.py").resolve()
    assert (
        Path(script_path).resolve()
        == (image / "skills/reckon-build/scripts/worktree_fleet.py").resolve()
    )

    _commit(root, "skills/reckon-build/scripts/worktree_fleet.py", "# changed script\n")
    changed = checkout_trust.source_code_identity(root / "reckon", source_root=root)
    assert changed != identity
    _commit(root, "NOTES.txt", "outside both code trees\n")
    assert (
        checkout_trust.source_code_identity(root / "reckon", source_root=root)
        == changed
    )


def test_a_reckon_only_reload_image_cannot_resolve_the_script(
    tmp_path: Path, monkeypatch
) -> None:
    root = _source(tmp_path)
    monkeypatch.setattr(
        checkout_trust, "_committed_tree_cache", lambda: tmp_path / "images"
    )
    monkeypatch.setattr(
        checkout_trust,
        "_archive_source",
        lambda source_root, revision: (
            checkout_trust._reckon_subtree(source_root, revision),
            "reckon/",
        ),
    )
    identity = checkout_trust.source_code_identity(root / "reckon", source_root=root)
    image, _, _, pin = checkout_trust.materialize_committed_tree(root, identity)
    os.close(pin)
    result = _resolve_in_image(image, tmp_path)
    assert result.returncode != 0
    assert "missing its worktree fleet script" in result.stderr
