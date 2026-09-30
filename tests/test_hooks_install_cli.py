"""Tests for the ``reckon hooks install`` command.

The command wraps :func:`reckon.hooks.install.install_hook_settings`: the dry
run prints the fragment and opens no file, ``--write`` merges it into the target
settings file, and a target that already carries the whole fragment is refused
rather than silently reported as a no-op.

Every case points the settings path at a file under the pytest temporary
directory, and every case compares the real user-scope settings file before and
after, so a command that reached for the default path instead of the named one
fails here rather than installing onto the machine running the suite.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from pwd import getpwuid

from click.testing import CliRunner

from reckon.cli import main
from reckon.hooks import install as installer

# A settings document carrying hooks and keys of its own, so the merge can be
# shown to keep them and the dry run shown not to print them.
EXISTING_SETTINGS: dict = {
    "model": "opus",
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Agent",
                "hooks": [{"type": "command", "command": "/opt/guard.py"}],
            }
        ]
    },
}


def _real_settings() -> Path:
    return Path(getpwuid(os.getuid()).pw_dir) / ".claude" / "settings.json"


def _fingerprint(path: Path) -> tuple[int, bytes] | None:
    try:
        status = path.stat()
    except FileNotFoundError:
        return None
    return status.st_mtime_ns, path.read_bytes()


def _write(path: Path, payload: dict) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2).encode()
    path.write_bytes(encoded)
    return encoded


def _invoke(settings: Path, *extra: str):
    return CliRunner().invoke(
        main,
        ["hooks", "install", "--scope", "user", "--settings", str(settings), *extra],
    )


def test_install_help_lists_the_command() -> None:
    """The verb exists and its help names the required options."""
    result = CliRunner().invoke(main, ["hooks", "install", "--help"])

    assert result.exit_code == 0, result.output
    assert "--scope" in result.output
    assert "--write" in result.output


def test_dry_run_prints_the_fragment_and_leaves_the_target_byte_identical(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "settings.json"
    original = _write(settings, EXISTING_SETTINGS)
    real_before = _fingerprint(_real_settings())

    result = _invoke(settings)

    assert result.exit_code == 0, result.output
    printed = json.loads(result.output)
    assert printed == installer.build_hook_snippet()
    # The fragment, not the merged document: the file's own hook is not printed.
    assert "/opt/guard.py" not in result.output
    assert settings.read_bytes() == original
    assert _fingerprint(_real_settings()) == real_before


def test_write_merges_into_the_named_settings_file(tmp_path: Path) -> None:
    settings = tmp_path / "claude" / "settings.json"
    _write(settings, EXISTING_SETTINGS)
    real_before = _fingerprint(_real_settings())

    result = _invoke(settings, "--write")

    assert result.exit_code == 0, result.output
    merged = json.loads(settings.read_text())
    # The file's own key, its own hook group and the fragment all survive.
    assert merged["model"] == "opus"
    assert merged["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert "/opt/guard.py" in json.dumps(merged)
    hooks = merged["hooks"]
    for event in ("UserPromptSubmit", "SessionStart", "Stop"):
        assert hooks.get(event), event
    assert _fingerprint(_real_settings()) == real_before


def test_write_refuses_a_duplicate_install(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    first = _invoke(settings, "--write")
    assert first.exit_code == 0, first.output
    installed = _fingerprint(settings)
    real_before = _fingerprint(_real_settings())

    result = _invoke(settings, "--write")

    assert result.exit_code != 0
    assert "already installed" in result.output
    # The refusal leaves the file exactly as the first install left it.
    assert _fingerprint(settings) == installed
    assert _fingerprint(_real_settings()) == real_before


def test_write_surfaces_an_installer_refusal(tmp_path: Path) -> None:
    """A file the installer cannot parse exits non-zero with its message."""
    settings = tmp_path / "settings.json"
    settings.write_bytes(b"this is not JSON")
    real_before = _fingerprint(_real_settings())

    result = _invoke(settings, "--write")

    assert result.exit_code != 0
    assert str(settings) in result.output
    assert settings.read_bytes() == b"this is not JSON"
    assert _fingerprint(_real_settings()) == real_before
