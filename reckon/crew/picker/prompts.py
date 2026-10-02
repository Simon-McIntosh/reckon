"""Render concise JSON prompts from package-local strict Jinja templates."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import (
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    StrictUndefined,
    Undefined,
)

from . import lane_context

TEMPLATES_DIR = Path(__file__).parent / "templates"


@lru_cache(maxsize=1)
def environment() -> Environment:
    env = Environment(
        loader=ChoiceLoader(
            [
                FileSystemLoader(TEMPLATES_DIR / "shared"),
                FileSystemLoader(TEMPLATES_DIR),
            ]
        ),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - JSON prompts, never HTML
    )

    def encode(value: Any) -> str:
        if isinstance(value, Undefined):
            str(value)
        return json.dumps(value, separators=(",", ":"))

    env.filters["json"] = encode
    return env


def render(name: str, **context: Any) -> str:
    if name == "state.jinja":
        # The lane context is built here so every caller of the state render
        # carries the return-time and local-lane blocks without duplicating the
        # derivation. A caller that supplies no project gets null figures.
        context["lane"] = lane_context.build(
            node=context.get("node"),
            candidates=context.get("candidates") or [],
            project=context.get("project"),
            budget_snapshot=context.get("budget_snapshot"),
            config=context.get("config"),
            now=context.get("now"),
        )
    return environment().get_template(name).render(**context).strip()
