"""Dispatch renders SVG inputs for image-capable workers and keeps the raster."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

from reckon import cli as cli_module
from reckon import crew, crew_dispatch_commands
from reckon.crew.runs import pointer_path
from tests import test_dispatch_attaches_the_figures as figures_dispatch

config = figures_dispatch.config
crew_home = figures_dispatch.crew_home
harness_bin = figures_dispatch.harness_bin
operator_home = figures_dispatch.operator_home
repo = figures_dispatch.repo


def _source(repo: Path, name: str, *, valid: bool = True) -> Path:
    path = repo / "docs" / "figures" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 700 350">'
        '<rect width="700" height="350" fill="blue"/></svg>'
        if valid
        else "<svg><broken"
    )
    path.write_text(body, encoding="utf-8")
    return path


def _images(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, flag in enumerate(argv[:-1]) if flag == "-i"]


def _tree(repo: Path) -> set[Path]:
    return {
        path.relative_to(repo) for path in repo.rglob("*") if ".git" not in path.parts
    }


def test_svg_cli_dispatch_records_a_run_local_png_and_resume_reuses_it(
    config: dict, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(repo, "review/figure.svg")
    before = _tree(repo)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *a, **k: config
    )
    recorder = figures_dispatch._Recorder()
    real_dispatch = crew.dispatch

    def dispatch_with_launcher(**kwargs):
        return real_dispatch(**{**kwargs, "launcher": recorder})

    monkeypatch.setattr(crew, "dispatch", dispatch_with_launcher)
    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "dispatch",
            "--project",
            figures_dispatch.PROJECT,
            "--plan",
            figures_dispatch.PLAN,
            "--section",
            "s6",
            "--role",
            "implement",
            "--spec-level",
            "exact",
            "--node",
            "raster-review",
            "--goal",
            "view the figure",
            "--done-when",
            "tests/test_dispatch_renders_svg_figures.py passes",
            "--write-path",
            "allowed.txt",
            "--image",
            str(source.relative_to(repo)),
            "--session",
            "raster-review",
            "--repo",
            str(repo),
            "--no-watch",
        ],
    )
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    run_id = record["run_id"]
    directory = crew.run_dir(run_id)
    image = Path(record["node"]["images"][0])
    assert image.is_relative_to(directory), image
    assert image.suffix == ".png"
    with Image.open(image) as rendered:
        assert rendered.format == "PNG"
        assert rendered.size == (1400, 700)
    assert _images(record["argv"]) == [str(image)]
    assert _images(recorder.argv[0]) == [str(image)]
    assert source.as_posix() not in record["argv"]
    assert _tree(repo) == before

    stopped = figures_dispatch._stopped_pointer(tmp_path, repo, run_id, backend="alpha")
    stopped["node"]["images"] = [str(image)]
    crew._write_json(pointer_path(run_id), stopped)
    resumed = crew.resume_plan(run_id, "continue", config=config)
    assert _images(list(resumed.argv)) == [str(image)]


def test_png_cli_dispatch_keeps_the_input_path(
    config: dict, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    png = repo / "figure.png"
    Image.new("RGB", (8, 4), "blue").save(png)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *a, **k: config
    )
    recorder = figures_dispatch._Recorder()
    real_dispatch = crew.dispatch
    monkeypatch.setattr(
        crew,
        "dispatch",
        lambda **kwargs: real_dispatch(**{**kwargs, "launcher": recorder}),
    )
    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "dispatch",
            "--project",
            figures_dispatch.PROJECT,
            "--plan",
            figures_dispatch.PLAN,
            "--section",
            "s6",
            "--role",
            "implement",
            "--spec-level",
            "exact",
            "--node",
            "png-review",
            "--goal",
            "view the figure",
            "--done-when",
            "tests/test_dispatch_renders_svg_figures.py passes",
            "--write-path",
            "allowed.txt",
            "--image",
            str(png),
            "--session",
            "png-review",
            "--repo",
            str(repo),
            "--no-watch",
        ],
    )
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record["node"]["images"] == [str(png)]
    assert _images(record["argv"]) == [str(png)]


def test_invalid_svg_refuses_dispatch_naming_the_source(
    config: dict, repo: Path, tmp_path: Path
) -> None:
    source = _source(repo, "review/broken.svg", valid=False)
    with pytest.raises(crew.CrewError, match=r"broken\.svg"):
        figures_dispatch.dispatch_module.dispatch(
            node=figures_dispatch._node(tmp_path, [str(source.relative_to(repo))]),
            project=figures_dispatch.PROJECT,
            repo=repo,
            config=config,
            session="broken-raster",
            check_budget=False,
            launcher=figures_dispatch._Recorder(),
        )
