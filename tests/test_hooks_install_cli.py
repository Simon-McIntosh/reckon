"""Tests for the ``reckon hooks install`` command.

The command wraps :func:`reckon.hooks.install.install_hook_settings`: the dry
run prints the fragment and opens no file, ``--write`` merges it into the target
settings file, and a target that already carries the whole fragment is refused
rather than silently reported as a no-op.

Every case that installs points the target at a path under the pytest temporary
directory — a named ``--settings`` file, or a ``HOME`` redirect — and every case
fingerprints the real user-scope settings file before and after, so a command
that reached for the real path instead of the named one fails here rather than
installing onto the machine running the suite.
"""

from __future__ import annotations

import json
import os
import shlex
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


def _real_user_settings() -> Path:
    """The real user-scope settings file, resolved independently of ``HOME``.

    ``getpwuid`` names the account's home from the password database, so this is
    the one file a test must never touch even when ``HOME`` points elsewhere.
    """
    return Path(getpwuid(os.getuid()).pw_dir) / ".claude" / "settings.json"


def _commanded_settings() -> Path:
    """The path the command writes when ``--settings`` is not given.

    Resolved through the installer's own helper, so it follows ``HOME`` exactly
    as the command does rather than assuming the two agree.
    """
    return installer.user_settings_path()


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


def _install(*extra: str):
    return CliRunner().invoke(main, ["hooks", "install", "--scope", "user", *extra])


def _fragment_commands(fragment: dict) -> list[str]:
    commands: list[str] = []
    for groups in fragment["hooks"].values():
        for group in groups:
            commands.extend(hook["command"] for hook in group["hooks"])
    return commands


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
    real_before = _fingerprint(_real_user_settings())

    result = _invoke(settings)

    assert result.exit_code == 0, result.output
    printed = json.loads(result.stdout)
    assert printed == installer.build_hook_snippet()
    # The fragment, not the merged document: the file's own hook is not printed.
    assert "/opt/guard.py" not in result.output
    assert settings.read_bytes() == original
    assert _fingerprint(_real_user_settings()) == real_before


def test_fragment_scripts_lie_under_the_checkout_the_command_ran_from(
    tmp_path: Path,
) -> None:
    """The fragment names this checkout's scripts, and stderr says which tree.

    A fragment composed from another tree would install commands pointing into
    it; the checkout is resolved here from this file's own location, never from
    the installer's helper, so a fragment naming a path outside this repository
    fails rather than agreeing with whatever the command computed.
    """
    checkout = Path(__file__).resolve().parents[1]
    settings = tmp_path / "settings.json"

    result = _invoke(settings)

    assert result.exit_code == 0, result.output
    assert str(checkout) in result.stderr

    fragment = json.loads(result.stdout)
    scripts = sorted(
        {
            token
            for command in _fragment_commands(fragment)
            for token in shlex.split(command)
            if token.startswith("/") and token.endswith(".py")
        }
    )
    assert scripts, "the fragment names no hook script path to check"
    for token in scripts:
        assert Path(token).is_relative_to(checkout), token


def test_write_merges_into_the_named_settings_file(tmp_path: Path) -> None:
    settings = tmp_path / "claude" / "settings.json"
    _write(settings, EXISTING_SETTINGS)
    real_before = _fingerprint(_real_user_settings())

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
    assert _fingerprint(_real_user_settings()) == real_before


def test_write_refuses_a_duplicate_install(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    first = _invoke(settings, "--write")
    assert first.exit_code == 0, first.output
    installed = _fingerprint(settings)
    real_before = _fingerprint(_real_user_settings())

    result = _invoke(settings, "--write")

    assert result.exit_code != 0
    assert "already installed" in result.output
    # The refusal leaves the file exactly as the first install left it.
    assert _fingerprint(settings) == installed
    assert _fingerprint(_real_user_settings()) == real_before


def test_write_surfaces_an_installer_refusal(tmp_path: Path) -> None:
    """A file the installer cannot parse exits non-zero with its message."""
    settings = tmp_path / "settings.json"
    settings.write_bytes(b"this is not JSON")
    real_before = _fingerprint(_real_user_settings())

    result = _invoke(settings, "--write")

    assert result.exit_code != 0
    assert str(settings) in result.output
    assert settings.read_bytes() == b"this is not JSON"
    assert _fingerprint(_real_user_settings()) == real_before


def test_default_target_follows_home_and_leaves_the_real_file_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    """With no ``--settings``, the install lands in ``HOME``, not the real file.

    The command resolves its default through the installer, which follows
    ``HOME``; the real file is the one the password database names, and it must
    be untouched even while the default path is redirected elsewhere.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    expected = fake_home / ".claude" / "settings.json"
    assert _commanded_settings() == expected
    real_before = _fingerprint(_real_user_settings())

    dry = _install()
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.stdout) == installer.build_hook_snippet()
    assert not expected.exists()

    written = _install("--write")
    assert written.exit_code == 0, written.output
    assert expected.exists()
    hooks = json.loads(expected.read_text())["hooks"]
    for event in ("UserPromptSubmit", "SessionStart", "Stop"):
        assert hooks.get(event), event
    assert _fingerprint(_real_user_settings()) == real_before
