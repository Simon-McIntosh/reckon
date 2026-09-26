"""Measure archived Python trees without importing their executable code.

Pinned commit identities live beside this script in snapshots.json. All source
references are relative to that commit, never to today's checkout. The emitted
methodology defines the deliberately conservative structural instruments.
"""

from __future__ import annotations

import argparse
import ast
import collections
import hashlib
import io
import json
import math
import re
import subprocess
import tarfile
import tempfile
import tokenize
from pathlib import Path

from candidates import ranked_candidates

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
DEFINITIONS = (*FUNCTIONS, ast.ClassDef)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def docstring_spans(tree):
    spans = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, *DEFINITIONS)) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                spans.update(range(first.lineno, first.end_lineno + 1))
    return spans


def token_lines(source, tree):
    """Keep names/operators; canonicalise literals and ignore prose/layout."""
    ignored = docstring_spans(tree)
    lines = collections.defaultdict(list)
    skip = {
        tokenize.ENCODING,
        tokenize.ENDMARKER,
        tokenize.COMMENT,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.NEWLINE,
        tokenize.NL,
    }
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in skip or tok.start[0] in ignored:
            continue
        value = (
            tokenize.tok_name[tok.type]
            if tok.type in (tokenize.STRING, tokenize.NUMBER)
            else tok.string
        )
        lines[tok.start[0]].append(value)
    return [(line, " ".join(tokens)) for line, tokens in sorted(lines.items())]


def owned_walk(node):
    """Walk one implementation, excluding nested definitions' implementations."""
    yield node
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, DEFINITIONS):
            yield from owned_walk(child)


def call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return call_name(node.value) + "." + node.attr
    return ""


def body_without_doc(node):
    body = node.body
    if body and isinstance(body[0], ast.Expr):
        expr = body[0].value
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return body[1:]
    return body


def forwards_only(node):
    body = body_without_doc(node)
    if len(body) != 1 or not isinstance(body[0], (ast.Return, ast.Expr)):
        return False
    value = body[0].value
    if isinstance(value, ast.Await):
        value = value.value
    if not isinstance(value, ast.Call):
        return False
    positional = [a.arg for a in (*node.args.posonlyargs, *node.args.args)]
    if positional and positional[0] in {"self", "cls"}:
        positional = positional[1:]
    supplied = []
    for arg in value.args:
        if isinstance(arg, ast.Name):
            supplied.append(arg.id)
        elif isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name):
            supplied.append("*" + arg.value.id)
        else:
            return False
    keywords = []
    for kw in value.keywords:
        if not isinstance(kw.value, ast.Name):
            return False
        keywords.append(kw.value.id if kw.arg else "**" + kw.value.id)
    expected = positional + [a.arg for a in node.args.kwonlyargs]
    if node.args.vararg:
        expected.append("*" + node.args.vararg.arg)
    if node.args.kwarg:
        expected.append("**" + node.args.kwarg.arg)
    return sorted(expected) == sorted(supplied + keywords)


def definitions(tree):
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
                yield from visit(child, ancestors)

    return list(visit(tree))


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    return round(values[lower] + (values[upper] - values[lower]) * (index - lower), 4)


def distribution(values):
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "median": percentile(values, 0.5),
        "p75": percentile(values, 0.75),
        "p90": percentile(values, 0.9),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
        "histogram": {
            label: sum(low <= n <= high for n in values)
            for label, low, high in (
                ("1-5", 1, 5),
                ("6-10", 6, 10),
                ("11-20", 11, 20),
                ("21-50", 21, 50),
                ("51-100", 51, 100),
                ("101-200", 101, 200),
                ("201+", 201, math.inf),
            )
        },
    }


def clone_census(modules):
    windows = collections.defaultdict(list)
    for path, module in modules.items():
        lines = module["tokens"]
        for index in range(len(lines) - 5):
            window = lines[index : index + 6]
            key = digest("\n".join(text for _, text in window))
            windows[key].append((path, tuple(line for line, _ in window)))
    covered = collections.defaultdict(set)
    groups = []
    for key, copies in sorted(windows.items()):
        if len(copies) < 2:
            continue
        # Overlapping windows in one repeated run are not independent copies.
        independent = []
        for path, lines in copies:
            if not any(p == path and set(lines) & set(ls) for p, ls in independent):
                independent.append((path, lines))
        if len(independent) < 2:
            continue
        for path, lines in copies:
            covered[path].update(lines)
        groups.append(
            {
                "fingerprint": key,
                "copies": [
                    {"path": path, "line": lines[0], "end_line": lines[-1]}
                    for path, lines in independent
                ],
            }
        )
    denominator = sum(len(m["tokens"]) for m in modules.values())
    numerator = sum(map(len, covered.values()))
    return {
        "window_code_lines": 6,
        "source_code_lines": denominator,
        "cloned_source_code_lines": numerator,
        "clone_share": round(numerator / denominator, 8) if denominator else None,
        "duplicate_window_fingerprints": len(groups),
        "by_module": [
            {"path": p, "cloned_code_lines": len(lines)}
            for p, lines in sorted(covered.items())
        ],
        "windows": groups,
    }


def primitive_concepts(node):
    nodes = list(owned_walk(node))
    calls = {call_name(n.func) for n in nodes if isinstance(n, ast.Call)}
    tails = {c.rsplit(".", 1)[-1] for c in calls}
    strings = {
        n.value
        for n in nodes
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    concepts = []
    if "fromisoformat" in tails:
        concepts.append("ISO timestamp parsing")
    if any(c in {"os.kill", "os.killpg"} for c in calls) and any(
        isinstance(n, ast.Call)
        and call_name(n.func) in {"os.kill", "os.killpg"}
        and len(n.args) > 1
        and isinstance(n.args[1], ast.Constant)
        and n.args[1].value == 0
        for n in nodes
    ):
        concepts.append("PID existence probing")
    if (
        "json.loads" in calls and ("read_text" in tails or "open" in tails)
    ) or "json.load" in calls:
        concepts.append("JSON document decoding")
    if (
        "json.dumps" in calls and ("write_text" in tails or "write" in tails)
    ) or "json.dump" in calls:
        concepts.append("JSON document encoding")
        if any(
            isinstance(call, ast.Call)
            and (
                call_name(call.func) in {"os.replace", "os.rename"}
                or (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr in {"replace", "rename"}
                    and len(call.args) == 1
                )
            )
            for call in nodes
        ):
            concepts.append("Atomic JSON file replacement")
    if "json.loads" in calls and any(
        isinstance(loop, ast.For)
        and any(
            isinstance(call, ast.Call)
            and call_name(call.func) == "json.loads"
            and call.args
            and (
                isinstance(call.args[0], ast.Name)
                or (
                    isinstance(call.args[0], ast.Call)
                    and call_name(call.args[0].func).endswith(
                        (".strip", ".lstrip", ".rstrip")
                    )
                )
            )
            for call in owned_walk(loop)
        )
        for loop in nodes
    ):
        concepts.append("JSON record stream decoding")
    if "git" in strings and any(c.startswith("subprocess.") for c in calls):
        concepts.append("Git subprocess invocation")
    if "read_text" in tails and any(".html" in s for s in strings):
        concepts.append("HTML file discovery and reading")
    if {"sha256", "sha1", "blake2b", "md5"} & tails:
        concepts.append("Content digest construction")
    if "flock" in tails:
        concepts.append("Advisory file locking")
    if "trapz" in tails or "trapezoid" in tails:
        concepts.append("Trapezoidal integration")
    if "interp" in tails or "interp1d" in tails:
        concepts.append("One-dimensional interpolation")
    if "meshgrid" in tails:
        concepts.append("Tensor grid construction")
    if {"gradient", "diff"} & tails:
        concepts.append("Array finite differences")
    if (
        "isfinite" in tails
        and ("float" in calls or "asarray" in tails)
        and any(isinstance(n, ast.Raise) for n in nodes)
    ):
        concepts.append("Finite numeric input validation")
    if (
        "writeable" in {n.attr for n in nodes if isinstance(n, ast.Attribute)}
        or "setflags" in tails
    ):
        concepts.append("Read-only array construction")
    return concepts


def reimplementations(modules):
    concepts = collections.defaultdict(dict)
    for path, module in modules.items():
        for node, name, _public, nested in module["definitions"]:
            if not isinstance(node, FUNCTIONS) or nested or forwards_only(node):
                continue
            ref = {
                "path": path,
                "line": node.lineno,
                "end_line": node.end_lineno,
                "function": name,
            }
            for concept in primitive_concepts(node):
                concepts[("primitive", concept)][(path, node.lineno)] = ref
            if "." not in name and len(body_without_doc(node)) > 1:
                concepts[("same_name", node.name.lstrip("_"))][(path, node.lineno)] = (
                    ref
                )
            body = ast.Module(body=body_without_doc(node), type_ignores=[])
            if sum(1 for _ in ast.walk(body)) >= 12:
                fingerprint = digest(ast.dump(body, include_attributes=False))
                concepts[("identical_body", fingerprint)][(path, node.lineno)] = ref
            statements = body_without_doc(node)
            lines = [
                text
                for line, text in module["tokens"]
                if statements and statements[0].lineno <= line <= node.end_lineno
            ]
            if len(lines) >= 6:
                concepts[("normalized_body", digest("\n".join(lines)))][
                    (path, node.lineno)
                ] = ref
    result = []
    for (kind, concept), copies in sorted(concepts.items()):
        if len(copies) < 3:
            continue
        result.append(
            {
                "kind": kind,
                "concept": concept,
                "copy_count": len(copies),
                "distinct_files": len({p for p, _ in copies}),
                "copies": list(copies.values()),
            }
        )
    return result


def literal_strings(node):
    if node is None:
        return []
    return [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def interfaces(modules):
    commands, groups, options, codes, views = [], [], [], [], []
    dispatch_raises = []
    for path, module in modules.items():
        tree = module["tree"]
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
    for path, module in modules.items():
        if not path.endswith("mcp_views.py"):
            continue
        for node in module["tree"].body:
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


def load_modules(root, prefix):
    modules = {}
    for path in sorted((root / prefix).rglob("*.py")):
        text = path.read_text(encoding="utf-8-sig")
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(text, filename=rel)
        modules[rel] = {
            "tree": tree,
            "definitions": definitions(tree),
            "tokens": token_lines(text, tree),
            "physical_lines": len(text.splitlines()),
            "sha256": digest(text),
        }
    return modules


def measure(modules, tests):
    functions, public_functions, public_classes, pass_through, widths = (
        [],
        [],
        [],
        [],
        [],
    )
    for path, module in modules.items():
        funcs, classes, methods = [], [], []
        for node, name, public, _nested in module["definitions"]:
            ref = {"path": path, "line": node.lineno, "name": name}
            if isinstance(node, FUNCTIONS):
                functions.append(node.end_lineno - node.lineno + 1)
                if public:
                    public_functions.append(ref)
                    (methods if "." in name else funcs).append(name)
                if forwards_only(node):
                    pass_through.append(ref)
            elif public:
                public_classes.append(ref)
                classes.append(name)
        width = len(funcs) + len(classes) + len(methods)
        widths.append(
            {
                "path": path,
                "physical_lines": module["physical_lines"],
                "code_lines": len(module["tokens"]),
                "public_top_level_functions": len(funcs),
                "public_methods": len(methods),
                "public_classes": len(classes),
                "public_surface": width,
                "public_surface_per_100_code_lines": round(
                    width * 100 / len(module["tokens"]), 4
                )
                if module["tokens"]
                else None,
                "sha256": module["sha256"],
            }
        )
    return {
        "source_lines": sum(m["physical_lines"] for m in modules.values()),
        "source_code_lines": sum(len(m["tokens"]) for m in modules.values()),
        "test_lines": sum(m["physical_lines"] for m in tests.values()),
        "test_code_lines": sum(len(m["tokens"]) for m in tests.values()),
        "test_files": sorted(tests),
        "module_count": len(modules),
        "test_module_count": len(tests),
        "public_function_count": len(public_functions),
        "public_class_count": len(public_classes),
        "public_surface": len(public_functions) + len(public_classes),
        "public_functions": public_functions,
        "public_classes": public_classes,
        "function_length_distribution": distribution(functions),
        "pass_through_function_count": len(pass_through),
        "pass_through_functions": pass_through,
        "module_surfaces": widths,
        "clones": clone_census(modules),
        "reimplementation_census": reimplementations(modules),
        "primitive_inventory": primitive_inventory(modules),
        "interfaces": interfaces(modules),
    }


def primitive_inventory(modules):
    inventory = collections.defaultdict(list)
    for path, module in modules.items():
        for node, name, _public, nested in module["definitions"]:
            if isinstance(node, FUNCTIONS) and not nested:
                for concept in primitive_concepts(node):
                    inventory[concept].append(
                        {"path": path, "line": node.lineno, "function": name}
                    )
    return [
        {"concept": concept, "copy_count": len(copies), "copies": copies}
        for concept, copies in sorted(inventory.items())
    ]


def crew_tests(tests):
    result = {}
    for path, module in tests.items():
        imports = []
        for node in ast.walk(module["tree"]):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
                imports.extend((node.module or "") + "." + a.name for a in node.names)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and re.match(r"reckon\.crew(?:\.|$)", node.value)
            ):
                imports.append(node.value)
        if any(
            name == "reckon.crew" or name.startswith("reckon.crew.") for name in imports
        ):
            result[path] = module
    return result


def controls():
    fixture = """def parse_time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    stamp = result.timestamp()
    return stamp
"""
    modules = {}
    for name in ("left.py", "right.py"):
        tree = ast.parse(fixture)
        modules[name] = {"tokens": token_lines(fixture, tree)}
    assert clone_census(modules)["cloned_source_code_lines"] == 12
    assert (
        clone_census({"left.py": modules["left.py"]})["cloned_source_code_lines"] == 0
    )
    assert forwards_only(
        ast.parse(
            "def wrapper(value, **kwargs):\n return target(value, **kwargs)"
        ).body[0]
    )
    assert not forwards_only(
        ast.parse("def wrapper(value):\n return target(value + 1)").body[0]
    )
    assert "ISO timestamp parsing" in primitive_concepts(ast.parse(fixture).body[0])
    atomic = ast.parse(
        "def persist(path, data):\n tmp.write_text(json.dumps(data))\n tmp.replace(path)"
    ).body[0]
    dataclass = ast.parse(
        'def persist(path, data):\n data = replace(data, name="value")\n path.write_text(json.dumps(data))'
    ).body[0]
    assert "Atomic JSON file replacement" in primitive_concepts(atomic)
    assert "Atomic JSON file replacement" not in primitive_concepts(dataclass)
    surface_source = """@root.command(name="inspect")
@click.option("--path", "-p")
def inspect_path(path):
    return path
def _crew(view="summary"):
    if view not in ("summary", "detail"):
        raise ValueError("invalid")
    return format_refusal("sample-code", view)
"""
    surface = interfaces({"example/mcp.py": {"tree": ast.parse(surface_source)}})
    assert surface["cli_command_count"] == 1 and surface["cli_option_count"] == 1
    assert (
        surface["mcp_view_count"] == 2 and surface["dispatch_refusal_code_count"] == 1
    )
    return {
        "duplicate_fixture": {"expected_clone_lines": 12, "observed_clone_lines": 12},
        "single_copy_fixture": {"expected_clone_lines": 0, "observed_clone_lines": 0},
        "forwarder_and_transformer_discriminated": True,
        "timestamp_primitive_found": True,
        "file_replacement_and_dataclass_replacement_discriminated": True,
        "surface_fixture": {
            "commands": 1,
            "options": 1,
            "mcp_views": 2,
            "refusal_codes": 1,
        },
    }


def compact_census(full, full_path, full_bytes):
    """Retain report figures and ranked copies while raw evidence stays external."""
    compact = {key: value for key, value in full.items() if key != "snapshots"}
    compact["full_output"] = {
        "path": str(full_path.resolve()),
        "bytes": len(full_bytes),
        "sha256": hashlib.sha256(full_bytes).hexdigest(),
    }
    compact["ranking_source_sha256"] = digest(
        Path(__file__).with_name("candidates.py").read_text()
    )
    compact["ranked_candidates"] = ranked_candidates(full)
    compact["snapshots"] = []
    scalar_keys = (
        "source_lines",
        "source_code_lines",
        "test_lines",
        "test_code_lines",
        "module_count",
        "test_module_count",
        "public_function_count",
        "public_class_count",
        "public_surface",
        "pass_through_function_count",
    )
    for snapshot in full["snapshots"]:
        row = {key: value for key, value in snapshot.items() if key != "scopes"}
        row["scopes"] = {}
        for scope, measured in snapshot["scopes"].items():
            summary = {key: measured[key] for key in scalar_keys}
            summary["function_length_distribution"] = {
                key: value
                for key, value in measured["function_length_distribution"].items()
                if key != "histogram"
            }
            summary["module_surfaces"] = sorted(
                (
                    module
                    for module in measured["module_surfaces"]
                    if module["public_surface"] >= 5
                ),
                key=lambda module: (
                    -module["public_surface_per_100_code_lines"],
                    module["path"],
                ),
            )[:5]
            summary["clones"] = {
                key: value
                for key, value in measured["clones"].items()
                if key not in {"windows", "by_module"}
            }
            summary["interfaces"] = {
                key: value
                for key, value in measured["interfaces"].items()
                if key.endswith("_count") or key == "dispatch_refusal_codes"
            }
            summary["interfaces"]["dispatch_raise_site_count"] = len(
                measured["interfaces"]["dispatch_raise_sites"]
            )
            summary["reimplementation_group_count"] = len(
                measured["reimplementation_census"]
            )
            summary["primitive_counts"] = {
                item["concept"]: item["copy_count"]
                for item in measured["primitive_inventory"]
            }
            row["scopes"][scope] = summary
        compact["snapshots"].append(row)
    return compact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", type=Path, default=Path("/home/ITER/mcintos/Code")
    )
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).with_name("code-depth.json")
    )
    parser.add_argument(
        "--full-output",
        type=Path,
        help="External full-census path; defaults to the path recorded in the compact output.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Recompute and require byte identity with the existing output.",
    )
    args = parser.parse_args()
    if args.full_output is None:
        if args.output.exists():
            recorded = json.loads(args.output.read_text()).get("full_output", {})
            args.full_output = Path(recorded["path"]) if recorded.get("path") else None
        if args.full_output is None:
            parser.error(
                "provide --full-output outside the repository for the first measurement"
            )
    if args.full_output.resolve().is_relative_to(Path(__file__).resolve().parents[5]):
        parser.error("--full-output must be outside the repository")
    pins_path = Path(__file__).with_name("snapshots.json")
    pins = json.loads(pins_path.read_text())
    result = {
        "inputs": pins,
        "instrument_sha256": digest(Path(__file__).read_text()),
        "positive_controls": controls(),
        "methodology": {
            "population": "Tracked Python modules under package directory and tests/; package-contained scripts included; no top-level scripts, notebooks, HTML, generated assets or other languages. Source archives are extracted with git archive; no target package imports or execution.",
            "source_lines": "Physical splitlines count, including blanks/comments/docstrings. Code lines are token-bearing physical start lines excluding comments/docstrings/layout; multiline tokens count at their start line.",
            "crew_tests": "Whole test files containing AST import/from import of reckon.crew or a literal patch/import path starting reckon.crew. Tests can cover other concerns too; this is an inclusive test association, not disjoint attribution.",
            "public_surface": "Non-underscore module-level functions/classes plus non-underscore methods/classes whose enclosing classes are public. Nested functions are excluded. Imports, reexports, dynamic facade exports and attributes are not counted.",
            "function_length": "Inclusive def-to-end physical span for every function, method and nested function; decorators excluded, docstrings included. Percentiles use linear interpolation.",
            "clones": "Hash every six consecutive token-bearing code-line window, ignoring comments/docstrings/layout; preserve identifier/operator spelling and map string/number literals to token kinds. Require two non-overlapping occurrences; count union of covered physical token start lines, including both copies, once. Every duplicate window is retained. Crew clones are recalculated within crew only.",
            "reimplementation": "Exhaustive within four disclosed candidate detectors: direct primitive signatures listed by primitive_concepts; same unprefixed module-level function name with multiple body statements; identical AST bodies with at least 12 nodes; literal-normalized token bodies with at least six code lines. Require at least three distinct function definitions, excluding pure forwarders and nested functions. Candidate equality is not semantic equivalence; primitive clients often require no consolidation. Unrecognised semantic equivalences remain unmeasured.",
            "pass_through": "After removing docstring, exactly one call expression/return (optionally awaited), forwarding every declared argument once with no value transformations; self/cls receiver excluded. Imports, adaptation, constants and extra statements disqualify.",
            "cli": "Static command and group decorators counted separately; option decorators on them count occurrences, aliases together as one option, version_option included, implicit Click help excluded. Callback registration generated dynamically is unmeasured.",
            "mcp": "Read-plan VIEW_NAMES, audit acceptance (shared names minus explicit refusal branches), Literal view annotations with wrapper aliases collapsed, and crew view comparisons. Count tool/name pairs; also give distinct spelling count. This includes accepted audit fallback names even if documentation omits them.",
            "refusals": "Distinct literal family codes passed to format_refusal across measured source; list every call site. Earlier unstructured refusals do not count as coded families; dispatch raise sites are retained separately. Not a count of all refusal mechanisms.",
        },
        "snapshots": [],
    }
    for repository in pins["repositories"]:
        name = repository["repository"]
        repo = args.repo_root / name
        for pin in repository["snapshots"]:
            commit = pin["commit"]
            files = (
                git(repo, "ls-tree", "-r", "--name-only", commit, name, "tests")
                .decode()
                .splitlines()
            )
            files = [p for p in files if p.endswith(".py")]
            with tempfile.TemporaryDirectory(
                prefix="code-depth-", dir="/tmp"
            ) as temporary:
                root = Path(temporary)
                archive = git(repo, "archive", "--format=tar", commit, "--", *files)
                with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                    tar.extractall(root, filter="data")
                modules = load_modules(root, name)
                tests = load_modules(root, "tests")
                scopes = {name + "/": measure(modules, tests)}
                if name == "reckon":
                    crew = {
                        p: m for p, m in modules.items() if p.startswith("reckon/crew/")
                    }
                    scopes["reckon/crew/"] = measure(crew, crew_tests(tests))
                result["snapshots"].append(
                    {"repository": name, **pin, "scopes": scopes}
                )
                print(
                    f"measured {name} {pin['target']} {commit}: {len(modules)} source modules, {len(tests)} test modules",
                    flush=True,
                )
    full_bytes = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
    compact = compact_census(result, args.full_output, full_bytes)
    encoded = json.dumps(compact, indent=2, sort_keys=True) + "\n"
    if len(encoded.encode()) >= 300_000:
        raise SystemExit("compact census exceeds the 300000-byte repository limit")
    args.full_output.write_bytes(full_bytes)
    print(
        f"full output {args.full_output}: bytes={len(full_bytes)} sha256={compact['full_output']['sha256']}",
        flush=True,
    )
    if args.check:
        if args.output.read_bytes() != encoded.encode():
            raise SystemExit("byte-identity check failed")
        print("byte-identity check passed", flush=True)
    else:
        args.output.write_text(encoded)
    print(f"output {args.output}: sha256={digest(args.output.read_text())}", flush=True)


if __name__ == "__main__":
    main()
