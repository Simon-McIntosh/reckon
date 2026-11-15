"""Every admission module can be the first admission import in a process."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULES = sorted((ROOT / "reckon/crew/admission").glob("*.py"))


@pytest.mark.parametrize("path", MODULES, ids=lambda path: path.stem)
def test_admission_module_imports_first(path):
    module = "reckon.crew.admission"
    if path.stem != "__init__":
        module += f".{path.stem}"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib, sys; "
                "module = importlib.import_module(sys.argv[1]); print(module.__file__)"
            ),
            module,
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"{module}:\n{result.stderr}"
    assert Path(result.stdout.strip()).resolve() == path.resolve()
