"""``reckon doctor`` flags MCP launches that would sync or resolve from the cwd.

A worker's session starts in a worktree, so a stdio MCP launch that runs
``uv run`` without ``--no-sync`` spends the following sync inside the client's
connect timeout, one that names no absolute ``--project`` resolves its project
from the working directory, one carrying a literal ``/home/<user>`` path is
wrong for every other user, and one whose ``--project`` names a directory that
no longer exists cannot start at all. The launches are read from the user-scope
entries in ``~/.claude.json`` and the ``.mcp.json`` at the root of each mounted
project's checkout.
"""

import json
import os
from pathlib import Path

from click.testing import CliRunner

from reckon.cli import main


def _real_user_config():
    """The operator's own config, resolved before any home is patched."""
    return Path(os.path.expanduser("~")) / ".claude.json"


def _mentions(path, needle):
    if not path.is_file():
        return False
    return needle in path.read_text(errors="ignore")


def _assert_real_config_untouched(real, marker):
    """The real ``~/.claude.json`` never carries this fixture's config.

    Doctor reads ``~/.claude.json``, so a run that resolved the operator's home
    instead of the fixture's would write its own entries there. ``marker`` is
    the fixture home's path, which appears in no real entry; the real file is
    checked for it rather than assumed out of reach.
    """
    assert not _mentions(real, marker), f"fixture config leaked into {real}"


def _mcp_config(servers):
    return json.dumps({"mcpServers": servers})


def _build(tmp_path):
    """A fixture home: a user config and three mounted projects."""
    home = tmp_path / "home"
    (home / "docs-server").mkdir(parents=True)

    # Project alpha omits --no-sync and names no --project.
    alpha = tmp_path / "alpha"
    (alpha / "docs").mkdir(parents=True)
    (alpha / ".mcp.json").write_text(
        _mcp_config(
            {"alpha": {"command": "uv", "args": ["run", "imas-codex", "serve"]}}
        )
    )

    # Project beta is correct: no sync, and its --project exists.
    beta = tmp_path / "beta"
    (beta / "docs").mkdir(parents=True)
    (home / "Code" / "imas-codex").mkdir(parents=True)
    (beta / ".mcp.json").write_text(
        _mcp_config(
            {
                "beta": {
                    "command": "uv",
                    "args": [
                        "run",
                        "--no-sync",
                        "--project",
                        "${HOME}/Code/imas-codex",
                        "imas-codex",
                        "serve",
                    ],
                }
            }
        )
    )

    # Project gamma is sync-free but its --project names an absent directory.
    gamma = tmp_path / "gamma"
    (gamma / "docs").mkdir(parents=True)
    (gamma / ".mcp.json").write_text(
        _mcp_config(
            {
                "gamma": {
                    "command": "uv",
                    "args": [
                        "run",
                        "--no-sync",
                        "--project",
                        "${HOME}/Code/absent",
                        "serve",
                    ],
                }
            }
        )
    )

    mounts = {
        "alpha": str(alpha / "docs"),
        "beta": str(beta / "docs"),
        "gamma": str(gamma / "docs"),
    }
    (home / "docs-server" / "mounts.json").write_text(json.dumps(mounts))

    (home / ".claude.json").write_text(
        _mcp_config(
            {
                "reckon": {
                    "command": "uv",
                    "args": [
                        "run",
                        "--project",
                        "/home/someone/Code/reckon",
                        "reckon",
                        "mcp",
                    ],
                }
            }
        )
    )
    return home, alpha, beta, gamma


def _run_doctor(tmp_path):
    from unittest import mock

    home, _alpha, _beta, _gamma = _build(tmp_path)
    real = _real_user_config()
    _assert_real_config_untouched(real, str(home))
    before = (home / ".claude.json").read_bytes()

    runner = CliRunner()
    with (
        mock.patch("pathlib.Path.home", return_value=home),
        mock.patch.dict(
            os.environ,
            {"HOME": str(home), "RECKON_HOME": str(home / "docs-server")},
        ),
        mock.patch(
            "reckon.project_maintenance_commands._project_environment_drift",
            return_value=(None, []),
        ),
        # Doctor's plugin check shells out to the real ``claude`` CLI, which
        # rewrites the operator's own config; stub it so the run touches no
        # config outside the fixture home.
        mock.patch(
            "reckon.project_maintenance_commands._claude_plugin_validate",
            return_value=None,
        ),
    ):
        result = runner.invoke(main, ["doctor"])

    assert (home / ".claude.json").read_bytes() == before, "doctor rewrote its config"
    _assert_real_config_untouched(real, str(home))
    return result


def _flagged_launches(output):
    """Split doctor's ``MCP launches`` section into ``{where: [reasons, fix]}``.

    Only that section is read: the skills, mounts and configuration checks use
    the same ``✗`` marker but print no corrected command, so their blocks would
    otherwise be mistaken for launches.
    """
    blocks = {}
    current = None
    active = False
    for line in output.splitlines():
        if line.strip() == "MCP launches":
            active = True
            continue
        if not active:
            continue
        if line and not line.startswith(" "):
            break
        if line.startswith("  ✗  "):
            current = line[len("  ✗  ") :].strip()
            blocks[current] = []
        elif current is not None and line.startswith("       "):
            blocks[current].append(line.strip())
    return blocks


def _reasons(block):
    return [line for line in block if not line.startswith("fix:")]


def test_flags_unsafe_projects_and_passes_the_safe_one(tmp_path):
    result = _run_doctor(tmp_path)
    blocks = _flagged_launches(result.output)

    where = "project [alpha] .mcp.json → alpha"
    assert where in blocks, result.output
    alpha_reasons = " ".join(_reasons(blocks[where]))
    assert "sync" in alpha_reasons
    assert "no absolute --project" in alpha_reasons

    blocked = "project [beta] .mcp.json → beta"
    assert blocked not in blocks, result.output

    where = "project [gamma] .mcp.json → gamma"
    assert where in blocks, result.output
    assert "missing directory" in " ".join(_reasons(blocks[where]))


def test_flags_the_user_scope_entry_for_three_reasons(tmp_path):
    result = _run_doctor(tmp_path)
    blocks = _flagged_launches(result.output)

    where = "user config .claude.json → reckon"
    assert where in blocks, result.output
    reasons = _reasons(blocks[where])
    assert len(reasons) == 3, reasons
    joined = " ".join(reasons)
    assert "sync" in joined
    assert "names one user's home" in joined
    assert "missing directory" in joined

    # Every flagged launch prints a corrected command that syncs nothing and
    # names an absolute project.
    for block in blocks.values():
        fix = next(line for line in block if line.startswith("fix:"))
        assert fix.startswith("fix: uv run --no-sync --project "), fix


def test_exits_non_zero_when_any_launch_is_flagged(tmp_path):
    result = _run_doctor(tmp_path)

    assert result.exit_code != 0
