from __future__ import annotations

import os
import subprocess
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


def test_built_wheel_carries_discovery_metadata(tmp_path):
    wheel = _built_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        assert len(metadata_names) == 1
        metadata = BytesParser(policy=default).parsebytes(
            archive.read(metadata_names[0])
        )

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
