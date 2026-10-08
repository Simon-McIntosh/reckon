"""No test launches a provider binary outside the account-probe seam.

A test that spawns ``claude`` or ``codex`` reaches a paid model provider: the
launch bills the account and makes the test's outcome depend on a live
credential and provider state rather than on the code under test. The
account-probe seam is the one launch that reaches a provider on purpose — the
``app-server`` exchange ``reckon._backends.run_probe`` issues — and it is
refused separately by the suite's ``no_live_account_probe`` guard, so a spawn
carrying that subcommand is the seam this scan exempts.

The scan reads the test sources rather than the running process, so it reports
a spawn that no test executed on this machine, and it cannot be defeated by a
branch that happened not to run.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# The executable names that reach a paid model provider.
_PROVIDER_BINARIES = frozenset({"claude", "codex"})

# ``reckon._backends`` composes the account probe as ``[command, "app-server"]``.
# A spawn carrying this subcommand is the account-probe seam, refused for every
# test by ``tests/conftest.py``'s ``no_live_account_probe``; the scan exempts it
# so the seam is named here rather than silently allowed.
_ACCOUNT_PROBE_SEAM_ARGV = "app-server"

# The ``subprocess`` entry points a test reaches to launch a process; every one
# funnels through ``Popen``. The ``os`` exec family takes its program as the
# first argument of a two-argument call, so it is spelled out separately.
_SPAWN_FUNCTIONS = frozenset(
    {
        "Popen",
        "run",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
        "system",
        "execv",
        "execvp",
        "execve",
    }
)


def _spawn_entry(func: ast.expr) -> str | None:
    """The called name when it is a ``subprocess``/``os`` spawn entry point."""
    if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
        return None
    if func.value.id not in {"subprocess", "os"}:
        return None
    return func.attr if func.attr in _SPAWN_FUNCTIONS else None


def _spawn_argv(call: ast.Call) -> list[ast.expr] | None:
    """The argv expressions a spawn call carries, or ``None`` when it carries none."""
    if call.args:
        return list(call.args)
    for keyword in call.keywords:
        if keyword.arg == "args":
            if isinstance(keyword.value, (ast.List, ast.Tuple)):
                return list(keyword.value.elts)
            return [keyword.value]
    return None


def _string_literals(node: ast.expr) -> list[str]:
    """String literals an argv expression contains at one level of nesting."""
    found: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        found.append(node.value)
    elif isinstance(node, (ast.List, ast.Tuple)):
        for element in node.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                found.append(element.value)
    return found


def _program_name(argv: list[ast.expr]) -> str | None:
    """The literal program a spawn names, or ``None`` when it is not a literal."""
    if not argv:
        return None
    head = argv[0]
    if isinstance(head, ast.Constant) and isinstance(head.value, str):
        parts = head.value.split()
        return parts[0] if parts else None
    if isinstance(head, (ast.List, ast.Tuple)) and head.elts:
        first = head.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _declared_provider_seams(tree: ast.AST) -> list[tuple[int, int, set[str]]]:
    """Functions whose decorators name a provider binary as a declared dependency.

    A test that skips when the binary is absent declares it reaches that binary
    on purpose, so its spawn is a seam the suite opted into rather than an
    unguarded reach. The plugin-loader check is the one such seam today.
    """
    spans: list[tuple[int, int, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        declared = {
            binary
            for binary in _PROVIDER_BINARIES
            if binary in " ".join(ast.unparse(d) for d in node.decorator_list).lower()
        }
        if declared:
            spans.append((node.lineno, node.end_lineno or node.lineno, declared))
    return spans


def provider_spawns(source: str, filename: str = "<source>") -> list[str]:
    """Spawning calls in ``source`` that launch a provider binary outside the seam.

    Each finding is ``<filename>:<line>: <entry>('<binary>')``. A spawn whose
    program is built at run time is left to the reader: the scan reports what it
    can see, never a guess.
    """
    findings: list[str] = []
    tree = ast.parse(source, filename=filename)
    seams = _declared_provider_seams(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        entry = _spawn_entry(node.func)
        if entry is None:
            continue
        argv = _spawn_argv(node)
        if argv is None:
            continue
        program = _program_name(argv)
        if program is None:
            continue
        binary = Path(program).name
        if binary not in _PROVIDER_BINARIES:
            continue
        literals = {value for expr in argv for value in _string_literals(expr)}
        if _ACCOUNT_PROBE_SEAM_ARGV in literals:
            continue
        if any(
            start <= node.lineno <= end and binary in declared
            for start, end, declared in seams
        ):
            continue
        findings.append(f"{filename}:{node.lineno}: {entry}({binary!r})")
    return findings


def test_no_test_spawns_a_provider_binary() -> None:
    """The suite's own sources launch no provider binary outside the seam."""
    findings: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        findings.extend(provider_spawns(path.read_text(encoding="utf-8"), path.name))
    assert findings == [], (
        "these tests spawn a provider binary (claude/codex) rather than a stub: "
        + "; ".join(findings)
    )


def test_the_scan_detects_a_provider_spawn() -> None:
    """The positive control: a real codex spawn must be reported."""
    sample = 'import subprocess\nsubprocess.run(["codex", "--version"])\n'
    assert provider_spawns(sample, "sample.py") == ["sample.py:2: run('codex')"]


def test_the_scan_exempts_the_account_probe_seam() -> None:
    """A spawn carrying the account-probe's ``app-server`` subcommand is the seam."""
    sample = 'import subprocess\nsubprocess.Popen(["codex", "app-server"])\n'
    assert provider_spawns(sample, "sample.py") == []


def test_the_scan_ignores_a_stub_named_after_a_provider() -> None:
    """A stub named ``codex`` reached through ``/bin/sh`` is not a provider launch."""
    sample = 'import subprocess\nsubprocess.Popen(["/bin/sh", "codex"])\n'
    assert provider_spawns(sample, "sample.py") == []