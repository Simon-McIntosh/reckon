"""A module-scoped fixture that runs sync still links into the override.

``tests/conftest.py`` points ``RECKON_CLAUDE_SKILLS_DIR`` at a temporary
directory for the whole session, so a fixture scoped wider than one test sees
it. A function-scoped override would leave a module- or session-scoped fixture
resolving the operator's real ``~/.claude/skills``, and a sync run there would
write a link into it. This module runs the sync from a module-scoped fixture and
proves the link landed in the override directory instead.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli_entry
from reckon.cli import CLAUDE_SKILLS_DIR_ENV, main

PLUGIN_NAME = "reckon-crew-host"


def _write_plugin(root: Path) -> Path:
    """Lay out a plugin directory the sync accepts as built."""
    plugin = root / "plugins" / "crew-host"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "crew-host", "version": "0.0.1"})
    )
    return plugin


@pytest.fixture(scope="module")
def module_scoped_sync(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Run ``reckon sync`` from a module-scoped fixture in a main checkout.

    The checkout is a plain directory, so the sync links rather than refusing.
    ``Path.home`` and the reckon checkout are patched for the duration of the
    run only; the override directory itself comes from the session-wide fixture
    in ``conftest``.
    """
    root = tmp_path_factory.mktemp("sync-module")
    checkout = root / "checkout"
    checkout.mkdir()
    source = _write_plugin(checkout)

    home = root / "home"
    home.mkdir()
    docs = root / "project" / "docs"
    docs.mkdir(parents=True)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "home", classmethod(lambda cls: home))
        patch.setenv("RECKON_HOME", str(home / "docs-server"))
        patch.setattr(cli_entry, "_reckon_checkout", lambda: checkout)
        result = CliRunner().invoke(
            main,
            [
                "sync",
                str(docs),
                "--project",
                "sample",
                "--mounts",
                str(root / "mounts.json"),
                "--state-root",
                str(root / "config-state"),
                "--claude-settings",
                str(root / "settings.json"),
            ],
        )
    assert result.exit_code == 0, result.output
    return {"checkout": checkout, "home": home, "source": source}


def test_module_scoped_sync_lands_in_the_override_directory(module_scoped_sync):
    override = os.environ.get(CLAUDE_SKILLS_DIR_ENV)
    assert override is not None, "the session must point RECKON_CLAUDE_SKILLS_DIR"
    dest = Path(override) / PLUGIN_NAME
    assert dest.is_symlink()
    assert dest.resolve() == module_scoped_sync["source"].resolve()


def test_module_scoped_sync_leaves_the_operator_home_unlinked(module_scoped_sync):
    assert not (module_scoped_sync["home"] / ".claude" / "skills").exists()
