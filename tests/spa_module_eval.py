from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from reckon.serve import compile_jsx

ROOT = Path(__file__).parents[1]


def evaluate_jsx_module(source_path: Path, expression: str) -> Any:
    """Compile a JSX module and evaluate an expression inside its module scope."""

    source = source_path.read_text(encoding="utf-8")
    instrumented = f"{source}\nconsole.log(JSON.stringify({expression}));\n"
    compiled = compile_jsx(instrumented, filename=source_path.name).decode()
    script = f"globalThis.window = globalThis;\n{compiled}"
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)
