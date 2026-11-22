"""The anchored edit tool stays removed.

The tool lost on the pooled local record against native clive and was deleted
with its console script. Nothing else in the package imports it, so a
reintroduced module or a restored script entry would leave every other test
green while the tool quietly came back; the two checks here fail when either
returns.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_NAME = "reckon-anchored-edit"
MODULE = "reckon.anchored_edit"


def test_the_anchored_edit_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(MODULE)


def test_no_console_script_launches_the_anchored_edit_module():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]

    assert SCRIPT_NAME not in scripts
    assert not [
        name for name, target in scripts.items() if target.startswith(f"{MODULE}:")
    ]
