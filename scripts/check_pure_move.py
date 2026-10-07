#!/usr/bin/env python3
"""Prove a module split moved every statement unchanged.

    python scripts/check_pure_move.py --base <rev> --module <original> \\
        --into <produced> [--into <produced> ...]

A split of a source module is a pure move: every non-import top-level
statement of the base module is carried, byte-identical, into exactly one
resulting module, and the only statements a resulting module may hold that did
not come from the base are imports (the moved imports plus the re-export lines
that keep ``from <original> import <name>`` working).  This turns a review of a
large diff into a review of the printed receipt and of the few lines that are
not moves.

The base module is read at ``--base`` through the repository's one revision
reader, :func:`reckon.velocity.read_sources`.  The resulting modules are the
``--into`` paths plus the original's remainder, each read from the working
tree, so no second git reader exists.

The receipt is one JSON object on stdout: ``moved`` lists each base statement
and the resulting module it landed in, ``new`` lists each statement in a
resulting module that did not come from the base, and ``estimated_tokens``
carries each resulting module's context-fit estimate.  The command exits 1 --
naming every lost, altered, duplicated or non-import new statement on stderr --
when the split is not a pure move.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reckon import velocity  # noqa: E402
from reckon.crew.routing import _tokens_for_bytes  # noqa: E402

_IMPORTS = (ast.Import, ast.ImportFrom)


def _names_in(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Starred):
        return _names_in(node.value)
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [name for item in node.elts for name in _names_in(item)]
    return []


def _label(node: ast.AST) -> str:
    """A short, stable name for one statement, for the receipt and diagnostics."""

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.Assign):
        names = [name for target in node.targets for name in _names_in(target)]
        return " = ".join(names) if names else "<assignment>"
    return ast.unparse(node).splitlines()[0][:80]


def _statement_text(source: str, node: ast.stmt) -> str:
    """The statement's source, including any decorator lines above it."""

    lines = source.splitlines(keepends=True)
    start = node.lineno
    for decorator in getattr(node, "decorator_list", []):
        start = min(start, decorator.lineno)
    return "".join(lines[start - 1 : node.end_lineno])


def _is_import(node: ast.stmt) -> bool:
    return isinstance(node, _IMPORTS)


def _receipt(repo: Path, base: str, module: str, produced: list[str]) -> dict:
    base_sources = _read_base(repo, base, module)
    base_source = base_sources[module]
    base_tree = ast.parse(base_source, filename=module)
    base_nodes = [node for node in base_tree.body if not _is_import(node)]
    base_statements = [_statement_text(base_source, node) for node in base_nodes]
    base_counter = Counter(base_statements)
    labels = {
        text: _label(node)
        for text, node in zip(base_statements, base_nodes, strict=True)
    }

    paths = list(dict.fromkeys([*produced, module]))
    sources = {path: _read_working(repo, path) for path in paths}
    trees = {path: ast.parse(sources[path], filename=path) for path in paths}

    where: dict[str, list[str]] = defaultdict(list)
    new: list[dict] = []
    problems: list[str] = []
    for path in paths:
        for node in trees[path].body:
            text = _statement_text(sources[path], node)
            if text in base_counter:
                where[text].append(path)
            else:
                new.append({"statement": _label(node), "module": path})
                if not _is_import(node):
                    problems.append(f"new {_label(node)!r} in {path}")

    moved: list[dict] = []
    for text in base_counter:
        hits = where.get(text, [])
        label = labels[text]
        if len(hits) == base_counter[text]:
            moved.extend(
                {"statement": label, "from": module, "to": hit} for hit in hits
            )
        elif len(hits) < base_counter[text]:
            problems.append(f"lost {label!r} from {module}")
        else:
            problems.append(f"duplicated {label!r} across {sorted(set(hits))}")

    return {
        "base": {
            "revision": base,
            "module": module,
            "statements": len(base_statements),
        },
        "moved": moved,
        "new": new,
        "estimated_tokens": {
            path: _tokens_for_bytes(len(sources[path].encode("utf-8")))
            for path in paths
        },
        "problems": problems,
    }


def _read_base(repo: Path, revision: str, module: str) -> dict[str, str]:
    sources = velocity.read_sources(repo, revision, prefix=module)
    text = sources.get(module)
    if text is None:
        raise SystemExit(f"{module} is not tracked at {revision}")
    return {module: text.decode("utf-8-sig")}


def _read_working(repo: Path, path: str) -> str:
    target = repo / path
    if not target.exists():
        raise SystemExit(f"{path} is not present in the working tree")
    return target.read_text(encoding="utf-8-sig")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repository root (default: cwd)")
    parser.add_argument(
        "--base", required=True, help="base revision holding the module"
    )
    parser.add_argument("--module", required=True, help="the original module path")
    parser.add_argument(
        "--into",
        action="append",
        required=True,
        help="a produced module path (repeatable)",
    )
    args = parser.parse_args(argv)

    receipt = _receipt(Path(args.repo), args.base, args.module, list(args.into))
    print(json.dumps(receipt, indent=2))
    if receipt["problems"]:
        for problem in receipt["problems"]:
            print(problem, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
