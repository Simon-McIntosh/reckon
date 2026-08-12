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
        has_codex_mcp=False,
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

        if has_codex_mcp:
            codex_dir = home / ".codex"
            codex_dir.mkdir(parents=True, exist_ok=True)
            (codex_dir / "config.toml").write_text(
                '[mcp_servers.reckon]\ncommand = "reckon"\n'
            )

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

    def test_codex_mcp_registration(self, tmp_path):
        result = self._run_doctor(
            tmp_path,
            has_skills=True,
            has_mounts=True,
            mounts_ok=True,
            has_mcp=False,
            has_codex_mcp=True,
        )
        assert result.exit_code == 0
        assert "registered in config.toml" in result.output

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


def test_install_skills_excludes_python_bytecode(tmp_path, monkeypatch):
    source = tmp_path / "source"
    skill = source / "reckon-example"
    cache = skill / "scripts" / "__pycache__"
    cache.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: reckon-example\ndescription: Example.\n---\n")
    (skill / "scripts" / "run.py").write_text("print('ok')\n")
    (cache / "run.cpython-314.pyc").write_bytes(b"compiled")

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("reckon.cli._skills_source", lambda: source)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    result = CliRunner().invoke(main, ["install-skills"])

    assert result.exit_code == 0
    for runtime in (".claude", ".codex", ".agents"):
        installed = home / runtime / "skills" / "reckon-example"
        assert (installed / "SKILL.md").is_file()
        assert (installed / "scripts" / "run.py").is_file()
        assert not (installed / "scripts" / "__pycache__").exists()


def _seed_link_drift(tmp_path, monkeypatch):
    source = tmp_path / "source"
    for name in ("reckon-linked", "reckon-copied"):
        skill = source / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}\n")

    home = tmp_path / "home"
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "reckon-linked").symlink_to(
        source / "reckon-linked", target_is_directory=True
    )
    copied = skills / "reckon-copied"
    copied.mkdir()
    (copied / "SKILL.md").write_text("# reckon-copied\n")
    monkeypatch.setattr("reckon.cli._skills_source", lambda: source)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    return source, home, copied


def test_install_skills_reports_copied_directory_among_links(
    tmp_path, monkeypatch
) -> None:
    source, _home, copied = _seed_link_drift(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["install-skills"])

    assert result.exit_code == 0
    assert "reckon-copied" in result.output
    assert "copied directory" in result.output
    assert f"expected symlink → {source / 'reckon-copied'}" in result.output
    assert copied.is_dir()
    assert not copied.is_symlink()


def test_install_skills_repairs_copied_directory_when_requested(
    tmp_path, monkeypatch
) -> None:
    source, _home, copied = _seed_link_drift(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["install-skills", "--repair"])

    assert result.exit_code == 0
    assert "repaired .claude/reckon-copied" in result.output
    assert copied.is_symlink()
    assert copied.resolve() == source / "reckon-copied"


def test_install_skills_accepts_a_consistently_copied_runtime(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "source"
    home = tmp_path / "home"
    for name in ("reckon-one", "reckon-two"):
        skill = source / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}\n")
        installed = home / ".claude" / "skills" / name
        installed.mkdir(parents=True)
        (installed / "SKILL.md").write_text(f"# {name}\n")
    monkeypatch.setattr("reckon.cli._skills_source", lambda: source)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    result = CliRunner().invoke(main, ["install-skills"])

    assert result.exit_code == 0
    assert "copied directory" not in result.output
    assert "repaired" not in result.output
