"""The picker's credential file is found from a linked worktree.

The follower, the admission service and the MCP server all start with ``sys.path``
rooted at the checkout that supplied the import, and that checkout may be a
linked worktree. Only the main checkout carries ``.env``, so a lookup that stops
at the worktree's own root reports the router disabled where the key is present.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from reckon.crew.picker import client as picker_client

KEY_NAME = "OPENROUTER_API_KEY_RECKON"
KEY_VALUE = "sk-resolution-probe-not-a-live-key"


def _install_package(root: Path) -> Path:
    """Place the picker module under ``root`` where a follower would import it."""
    source = Path(picker_client.__file__).read_text()
    package = root / "reckon"
    picker = package / "crew" / "picker"
    picker.mkdir(parents=True, exist_ok=True)
    for directory in (package, package / "crew", picker):
        (directory / "__init__.py").write_text("")
    module = picker / "client.py"
    module.write_text(source)
    return module


def _linked_checkout(tmp_path: Path, *, key: bool = True) -> tuple[Path, Path]:
    """Build a main checkout and a linked worktree of it, laid out as git writes them."""
    main = tmp_path / "main"
    gitdir = main / ".git" / "worktrees" / "linked"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    if key:
        (main / ".env").write_text(f"export {KEY_NAME}={KEY_VALUE}\n")
    worktree = tmp_path / "linked"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
    _install_package(worktree)
    return main, worktree


def _plain_checkout(tmp_path: Path) -> Path:
    """Build a checkout that is not a worktree and carries no credential file."""
    root = tmp_path / "exported"
    root.mkdir()
    _install_package(root)
    return root


def _lookup(root: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Ask for the key from a process started the way the follower starts."""
    script = "\n".join(
        [
            "import json, sys",
            f"sys.path.insert(0, {str(root)!r})",
            "from reckon.crew.picker import client",
            "path = client.credential_path()",
            "print(json.dumps({'module': client.__file__, 'path': str(path)}))",
            "try:",
            "    print(json.dumps({'key': client.load_key(path)}))",
            "except Exception as error:",
            "    print('ERROR', type(error).__name__, str(error))",
        ]
    )
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    environ = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": "",
    }
    assert KEY_NAME not in environ
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        env=environ,
        capture_output=True,
        text=True,
        check=False,
    )


def _report(result: subprocess.CompletedProcess[str]) -> tuple[dict, str]:
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 2, result.stdout
    return json.loads(lines[0]), lines[1]


def test_a_linked_worktree_finds_the_key_in_the_main_checkout(tmp_path: Path) -> None:
    main, worktree = _linked_checkout(tmp_path)

    report, answer = _report(_lookup(worktree, tmp_path))

    # The instrument first: the module under test was imported from the worktree,
    # which holds no credential file of its own.
    assert Path(report["module"]).resolve() == (
        worktree / "reckon/crew/picker/client.py"
    )
    assert not (worktree / ".env").exists()
    assert Path(report["path"]) == (main / ".env").resolve()
    assert json.loads(answer) == {"key": KEY_VALUE}


def test_no_file_and_no_variable_names_both_places_it_looked(tmp_path: Path) -> None:
    main, worktree = _linked_checkout(tmp_path, key=False)

    report, answer = _report(_lookup(worktree, tmp_path))

    assert Path(report["module"]).resolve() == (
        worktree / "reckon/crew/picker/client.py"
    )
    assert answer.startswith("ERROR LiveJevDisabledError ")
    assert "environment" in answer
    assert str((main / ".env").resolve()) in answer


def test_a_checkout_that_is_not_a_worktree_still_resolves_its_own_file(
    tmp_path: Path,
) -> None:
    root = _plain_checkout(tmp_path)
    (root / ".env").write_text(f"{KEY_NAME}={KEY_VALUE}\n")

    report, answer = _report(_lookup(root, tmp_path))

    assert Path(report["path"]) == (root / ".env").resolve()
    assert json.loads(answer) == {"key": KEY_VALUE}
