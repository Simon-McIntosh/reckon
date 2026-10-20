from pathlib import Path

from tests.spa_browser_harness import ROOT, served_spa


def test_served_page_copies_only_its_state(tmp_path: Path) -> None:
    with served_spa(
        tmp_path,
        "unused",
        docs=ROOT / "docs",
        state_files=("shared-write-paths.json",),
    ):
        (server_home,) = tmp_path.glob("reckon-server-*")
        state = server_home / "state" / "reckon"
        copied = {
            path.relative_to(state).as_posix()
            for path in state.rglob("*")
            if path.is_file()
        }
        assert "index.json" in copied
        assert "project.json" in copied
        assert "shared-write-paths.json" in copied
        assert (
            sum(path.stat().st_size for path in state.rglob("*") if path.is_file())
            < 1_000_000
        )
        assert "crew.json" not in copied
        assert not (state / "runs").exists()
        assert not (state / "reviews").exists()
