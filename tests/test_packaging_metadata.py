from __future__ import annotations

import os
import subprocess
import textwrap
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]


def _built_wheel(tmp_path: Path) -> Path:
    supplied_wheel = os.environ.get("RECKON_BUILT_WHEEL")
    if supplied_wheel:
        wheel = Path(supplied_wheel)
        assert wheel.is_file(), f"built wheel does not exist: {wheel}"
        return wheel

    output_dir = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output_dir)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(output_dir.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _wheel_metadata(wheel: Path):
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        assert len(metadata_names) == 1
        return BytesParser(policy=default).parsebytes(archive.read(metadata_names[0]))


def test_built_wheel_carries_discovery_metadata(tmp_path):
    wheel = _built_wheel(tmp_path)
    metadata = _wheel_metadata(wheel)

    project_urls = metadata.get_all("Project-URL", [])
    classifiers = metadata.get_all("Classifier", [])

    assert len(project_urls) >= 4
    assert {entry.split(",", 1)[0] for entry in project_urls} >= {
        "Documentation",
        "Homepage",
        "Issues",
        "Source",
    }
    assert len(classifiers) >= 6
    assert "Development Status :: 4 - Beta" in classifiers
    assert "Intended Audience :: Developers" in classifiers
    assert "Programming Language :: Python :: 3.12" in classifiers
    assert any(item.startswith("Topic :: ") for item in classifiers)


def test_untagged_wheel_version_has_no_local_segment(tmp_path):
    metadata = _wheel_metadata(_built_wheel(tmp_path))

    assert "+" not in metadata["Version"]


def test_tagged_build_matches_tag(tmp_path):
    project = tmp_path / "tagged-project"
    package = project / "sample_package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (project / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [build-system]
            requires = ["hatchling", "hatch-vcs"]
            build-backend = "hatchling.build"

            [project]
            name = "tagged-version-fixture"
            dynamic = ["version"]

            [tool.hatch.version]
            source = "vcs"

            [tool.hatch.version.raw-options]
            local_scheme = "no-local-version"

            [tool.hatch.build.targets.wheel]
            packages = ["sample_package"]
            """
        ).lstrip()
    )
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=project,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Packaging Test"],
        cwd=project,
        check=True,
    )
    subprocess.run(
        ["git", "add", "pyproject.toml", "sample_package"], cwd=project, check=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "Create package"], cwd=project, check=True
    )
    subprocess.run(["git", "tag", "v1.2.3"], cwd=project, check=True)

    output_dir = tmp_path / "tagged-dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output_dir)],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(output_dir.glob("*.whl"))

    assert _wheel_metadata(wheel)["Version"] == "1.2.3"
