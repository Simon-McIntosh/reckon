"""Count the public interface of the reckon package at a pinned revision.

The crew-pattern review measured how wide a codebase's interface is by parsing
its abstract syntax trees without importing them; this module carries the same
counting rules so the velocity view can report an interface level and its weekly
change. It reads one revision's ``reckon/`` tree straight out of git — no
checkout and no import — so a revision named by a commit sha is measured exactly
as that commit held it.

Four families are counted, the ones the plan-review rubric reads:

* public definitions — non-underscore module-level functions and classes plus
  non-underscore methods of public classes; nested functions are excluded;
* CLI options — option decorators sitting on a command or group callback, each
  decorator counted once with its aliases together, ``version_option`` included
  and the implicit Click help option excluded;
* MCP views — read-plan ``VIEW_NAMES``, audit-reachable names, literal ``view``
  annotations with wrapper aliases collapsed, and crew view comparisons, counted
  as distinct tool/name pairs;
* refusal families — distinct literal family codes passed to ``format_refusal``.

Counting a revision is the expensive part, so each revision's counts are cached
by its resolved sha; a warm read of the same revision recomputes nothing. The
cache lives under the same root the velocity view uses, so a caller that
isolated its cache isolated this one too.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
DEFINITIONS = (*FUNCTIONS, ast.ClassDef)
COUNT_KEYS = (
    "public_definitions",
    "cli_options",
    "mcp_views",
    "refusal_families",
)
CACHE_VERSION = 1
DEFAULT_PREFIX = "reckon"
__all__ = [
    "COUNT_KEYS",
    "count_revision",
    "count_revision_cached",
    "counts",
    "read_trees",
]


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], timeout=180)


def call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return call_name(node.value) + "." + node.attr
    return ""


def literal_strings(node):
    if node is None:
        return []
    return [
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    ]


def definitions(tree):
    """Every function/class definition with its qualified name and publicity.

    ``public`` is true for a non-underscore definition whose enclosing classes
    are all public; a function nested inside another function is never public.
    """

    def visit(node, ancestors=()):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, DEFINITIONS):
                qualified = ".".join([a.name for a in ancestors] + [child.name])
                nested = any(isinstance(a, FUNCTIONS) for a in ancestors)
                public = not nested and all(
                    not a.name.startswith("_") for a in (*ancestors, child)
                )
                yield child, qualified, public, nested
                yield from visit(child, (*ancestors, child))
            else:
                yield from visit(child, (ancestors))

    return list(visit(tree))


def interfaces(trees):
    """The interface surface of a mapping of module paths to parsed trees.

    Ported from the review's census so the counts agree with the study's over
    the same tree: command and group decorators, the option decorators they
    carry, ``format_refusal`` family codes, and every shape of MCP view name.
    """
    commands, groups, options, codes, views = [], [], [], [], []
    dispatch_raises = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, FUNCTIONS):
                command = None
                for dec in node.decorator_list:
                    if not isinstance(dec, ast.Call):
                        continue
                    name = call_name(dec.func)
                    if name.rsplit(".", 1)[-1] in {"command", "group"}:
                        explicit = next(
                            (
                                k.value.value
                                for k in dec.keywords
                                if k.arg == "name" and isinstance(k.value, ast.Constant)
                            ),
                            None,
                        )
                        if (
                            explicit is None
                            and dec.args
                            and isinstance(dec.args[0], ast.Constant)
                        ):
                            explicit = dec.args[0].value
                        command = {
                            "path": path,
                            "line": dec.lineno,
                            "function": node.name,
                            "parent": name.rsplit(".", 1)[0],
                            "name": explicit or node.name.replace("_", "-"),
                        }
                        (groups if name.endswith(".group") else commands).append(
                            command
                        )
                if command:
                    for dec in node.decorator_list:
                        if isinstance(dec, ast.Call) and call_name(dec.func).rsplit(
                            ".", 1
                        )[-1] in {"option", "version_option", "help_option"}:
                            flags = [
                                a.value
                                for a in dec.args
                                if isinstance(a, ast.Constant)
                                and isinstance(a.value, str)
                                and a.value.startswith("-")
                            ]
                            if not flags and call_name(dec.func).endswith(
                                "version_option"
                            ):
                                flags = ["--version"]
                            options.append(
                                {
                                    "path": path,
                                    "line": dec.lineno,
                                    "function": node.name,
                                    "flags": flags,
                                }
                            )
            if (
                isinstance(node, ast.Call)
                and call_name(node.func).endswith("format_refusal")
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                codes.append(
                    {"path": path, "line": node.lineno, "code": node.args[0].value}
                )
            if isinstance(node, ast.Raise) and "/crew/dispatch" in path:
                dispatch_raises.append(
                    {
                        "path": path,
                        "line": node.lineno,
                        "exception": call_name(node.exc.func)
                        if isinstance(node.exc, ast.Call)
                        else call_name(node.exc),
                    }
                )
            if path.endswith("mcp_views.py") and isinstance(
                node, (ast.Assign, ast.AnnAssign)
            ):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if any(
                    isinstance(t, ast.Name) and t.id == "VIEW_NAMES" for t in targets
                ):
                    views.extend(
                        {
                            "surface": "read_plan",
                            "name": name,
                            "path": path,
                            "line": node.lineno,
                        }
                        for name in literal_strings(node.value)
                    )
            if path.endswith("mcp.py") and isinstance(node, FUNCTIONS):
                view_arg = next((a for a in node.args.args if a.arg == "view"), None)
                if view_arg is not None:
                    views.extend(
                        {
                            "surface": node.name.lstrip("_").removesuffix("_tool"),
                            "name": name,
                            "path": path,
                            "line": node.lineno,
                        }
                        for name in literal_strings(view_arg.annotation)
                    )
                if node.name == "_crew":
                    for cmp in ast.walk(node):
                        if (
                            isinstance(cmp, ast.Compare)
                            and isinstance(cmp.left, ast.Name)
                            and cmp.left.id == "view"
                        ):
                            for value in cmp.comparators:
                                views.extend(
                                    {
                                        "surface": "crew",
                                        "name": name,
                                        "path": path,
                                        "line": cmp.lineno,
                                    }
                                    for name in literal_strings(value)
                                )
    resource_names = {v["name"] for v in views if v["surface"] == "read_plan"}
    for path, tree in trees.items():
        if not path.endswith("mcp_views.py"):
            continue
        for node in tree.body:
            if isinstance(node, FUNCTIONS) and node.name == "audit_view":
                rejected = set()
                for branch in ast.walk(node):
                    if (
                        isinstance(branch, ast.If)
                        and isinstance(branch.test, ast.Compare)
                        and isinstance(branch.test.left, ast.Name)
                        and branch.test.left.id == "selected"
                        and any(isinstance(stmt, ast.Raise) for stmt in branch.body)
                    ):
                        rejected.update(literal_strings(branch.test))
                views.extend(
                    {
                        "surface": "audit",
                        "name": name,
                        "path": path,
                        "line": node.lineno,
                    }
                    for name in sorted(resource_names - rejected)
                )
    unique_views = {}
    for view in views:
        unique_views.setdefault((view["surface"], view["name"]), view)
    return {
        "cli_command_count": len(commands),
        "cli_group_count": len(groups),
        "cli_option_count": len(options),
        "cli_option_unique_flag_count": len(
            {f for opt in options for f in opt["flags"]}
        ),
        "cli_commands": commands,
        "cli_groups": groups,
        "cli_options": options,
        "mcp_view_count": len(unique_views),
        "mcp_unique_view_name_count": len({v["name"] for v in views}),
        "mcp_views": [unique_views[k] for k in sorted(unique_views)],
        "dispatch_refusal_code_count": len({c["code"] for c in codes}),
        "dispatch_refusal_codes": sorted({c["code"] for c in codes}),
        "dispatch_refusal_sites": codes,
        "dispatch_raise_sites": dispatch_raises,
    }


def counts(trees):
    """The four counted families over a mapping of module paths to trees."""
    public = sum(
        1
        for tree in trees.values()
        for _node, _qualified, is_public, _nested in definitions(tree)
        if is_public
    )
    surface = interfaces(trees)
    return {
        "public_definitions": public,
        "cli_options": surface["cli_option_count"],
        "mcp_views": surface["mcp_view_count"],
        "refusal_families": surface["dispatch_refusal_code_count"],
    }


def _read_blobs(repo, revision, paths):
    """Read named blobs of a revision in one ``cat-file --batch`` pass."""
    if not paths:
        return {}
    payload = "".join(f"{revision}:{path}\n" for path in paths).encode()
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input=payload,
        capture_output=True,
        check=False,
        timeout=180,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )
    data, position, blobs = result.stdout, 0, {}
    for path in paths:
        newline = data.index(b"\n", position)
        header = data[position:newline].decode()
        position = newline + 1
        if header.strip().endswith("missing"):
            continue
        size = int(header.split()[2])
        blobs[path] = data[position : position + size]
        position += size + 1
    return blobs


def read_trees(repo, revision, *, prefix=DEFAULT_PREFIX):
    """Parse a revision's Python modules under ``prefix``, read through git.

    No checkout is made: the module list comes from ``ls-tree`` and the bytes
    from one batched object read, so a revision that is not the working tree is
    measured as that revision held it.
    """
    listing = git(repo, "ls-tree", "-r", "--name-only", revision, "--", prefix).decode()
    paths = [path for path in listing.splitlines() if path.endswith(".py")]
    blobs = _read_blobs(repo, revision, paths)
    return {
        path: ast.parse(text.decode("utf-8-sig"), filename=path)
        for path, text in blobs.items()
    }


def count_revision(repo, revision, *, prefix=DEFAULT_PREFIX):
    """The four counts at one revision, read straight out of git."""
    if not revision:
        return dict.fromkeys(COUNT_KEYS, 0)
    return counts(read_trees(repo, revision, prefix=prefix))


def _cache_root():
    # Late import: the velocity view imports this module, so importing it back
    # at module scope would close the cycle.
    from reckon.velocity import _velocity_cache_root

    return _velocity_cache_root()


def _cache_path(sha, cache_root=None):
    root = Path(cache_root) if cache_root is not None else _cache_root()
    return root / "interfaces" / (sha + ".json")


def _load_cached(path, prefix):
    try:
        entry = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if (
        not isinstance(entry, dict)
        or entry.get("version") != CACHE_VERSION
        or entry.get("prefix") != prefix
        or not isinstance(entry.get("counts"), dict)
    ):
        return None
    if any(key not in entry["counts"] for key in COUNT_KEYS):
        return None
    return {key: int(entry["counts"][key]) for key in COUNT_KEYS}


def _store_cached(path, sha, prefix, result):
    from reckon._store import write_json_atomically

    write_json_atomically(
        path,
        {
            "version": CACHE_VERSION,
            "revision": sha,
            "prefix": prefix,
            "counts": result,
        },
        fsync=False,
        indent=None,
    )


def count_revision_cached(repo, revision, *, cache_root=None, prefix=DEFAULT_PREFIX):
    """Count a revision, reusing a cached result for the same resolved sha.

    The counts are a pure function of the revision's tree, so a cache hit is the
    same reading a fresh parse would produce; a warm read of a revision already
    counted recomputes nothing. A corrupt or version-mismatched entry is
    discarded and rebuilt rather than trusted.
    """
    if not revision:
        return dict.fromkeys(COUNT_KEYS, 0)
    sha = git(repo, "rev-parse", revision + "^{commit}").decode().strip()
    path = _cache_path(sha, cache_root)
    cached = _load_cached(path, prefix)
    if cached is not None:
        return cached
    result = counts(read_trees(repo, sha, prefix=prefix))
    _store_cached(path, sha, prefix, result)
    return result
