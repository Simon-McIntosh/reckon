"""Tests for reckon doctor and reckon install-skills commands."""

import json

from click.testing import CliRunner

from reckon.cli import _skills_source, main


# ── doctor ─────────────────────────────────────────────────────────────────


class TestDoctor:
    def _make_env(
        self,
        tmp_path,
        has_skills=True,
        has_mounts=True,
        mounts_ok=True,
        has_mcp=True,
        mcp_has_reckon=True,
    ):
        """Build a temporary environment for doctor to inspect."""
        home = tmp_path / "home"
        home.mkdir()

        skills_dir = home / ".claude" / "skills"
        if has_skills:
            skills = sorted(
                path.name
                for path in _skills_source().iterdir()
                if path.is_dir() and (path / "SKILL.md").is_file()
            )
            for skill in skills:
                skill_path = skills_dir / skill
                skill_path.mkdir(parents=True)
                (skill_path / "SKILL.md").write_text(f"# {skill}\n")

        mounts_dir = home / "docs-server"
        mounts_dir.mkdir(parents=True)
        if has_mounts:
            if mounts_ok:
                proj_dir = tmp_path / "myproject" / "docs"
                proj_dir.mkdir(parents=True)
                mounts = {"myproject": str(proj_dir)}
            else:
                mounts = {"myproject": str(tmp_path / "nonexistent" / "docs")}
            (mounts_dir / "mounts.json").write_text(json.dumps(mounts))

        if has_mcp:
            mcp_dir = home / ".claude"
            mcp_dir.mkdir(parents=True, exist_ok=True)
            cfg = {"mcpServers": {}}
            if mcp_has_reckon:
                cfg["mcpServers"]["reckon"] = {
                    "command": "uv",
                    "args": ["run", "reckon", "mcp"],
                }
            (mcp_dir / "claude_desktop_config.json").write_text(json.dumps(cfg))

        return home

    def _run_doctor(self, tmp_path, **kwargs):
        """Run reckon doctor with a fake HOME."""
        home = self._make_env(tmp_path, **kwargs)
        runner = CliRunner()

        import unittest.mock as mock

        with mock.patch("pathlib.Path.home", return_value=home):
            result = runner.invoke(main, ["doctor"])
        return result

    def test_happy_path(self, tmp_path):
        result = self._run_doctor(
            tmp_path,
            has_skills=True,
            has_mounts=True,
            mounts_ok=True,
            has_mcp=True,
            mcp_has_reckon=True,
        )
        assert result.exit_code == 0
        assert "All checks passed" in result.output

    def test_missing_skill(self, tmp_path):
        result = self._run_doctor(
            tmp_path,
            has_skills=False,
            has_mounts=True,
            mounts_ok=True,
            has_mcp=True,
            mcp_has_reckon=True,
        )
        assert result.exit_code != 0
        assert "install-skills" in result.output

    def test_missing_mount_dir(self, tmp_path):
        result = self._run_doctor(
            tmp_path,
            has_skills=True,
            has_mounts=True,
            mounts_ok=False,
            has_mcp=True,
            mcp_has_reckon=True,
        )
        assert result.exit_code != 0
        assert "directory not found" in result.output

    def test_no_mounts_file(self, tmp_path):
        result = self._run_doctor(
            tmp_path,
            has_skills=True,
            has_mounts=False,
            mounts_ok=True,
            has_mcp=True,
            mcp_has_reckon=True,
        )
        assert result.exit_code != 0
        assert "not found" in result.output
