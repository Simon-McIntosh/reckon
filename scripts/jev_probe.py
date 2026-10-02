"""Probe OpenRouter's chat and typed-decision surfaces for Jev."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
SYSTEM_ONE_URL = "https://openrouter.ai/api/v1/systemone"
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
ROUTER_MODEL = "typesafe/jev-router"
JEV_MODEL = "typesafe/jev-1.13"
DEFAULT_ENV = Path("/home/ITER/mcintos/Code/reckon/.env")

STATE = {
    "node": {
        "role": "implement",
        "specification_level": "exact",
        "goal": "Add a deterministic parser unit test",
        "risk": "low",
    },
    "lanes": {
        "local": {"available": True, "metered": False, "fit": "exact work"},
        "codex": {"available": True, "metered": True, "fit": "deep implementation"},
        "claude": {"available": True, "metered": True, "fit": "ambiguous design"},
    },
}
OPTIONS = {
    "local": "Straightforward, fully specified work needing no semantic decision.",
    "codex": "Complex implementation or debugging that benefits from deep reasoning.",
    "claude": "Ambiguous design work requiring broad synthesis.",
}
PROMPT = (
    "Choose exactly one lane for the JSON state. Options are local, codex, and claude. "
    "Return the option key only. State: " + json.dumps(STATE, separators=(",", ":"))
)


def load_key() -> str:
    """Read the credential without ever including it in output."""
    value = os.environ.get("OPENROUTER_API_KEY_RECKON", "").strip()
    if value:
        return value
    if DEFAULT_ENV.is_file():
        for line in DEFAULT_ENV.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY_RECKON="):
                value = line.split("=", 1)[1].strip().strip("'\"")
                if value:
                    return value
    raise SystemExit(
        "OPENROUTER_API_KEY_RECKON is not set and was not found in the configured .env"
    )


def choice_question() -> dict[str, Any]:
    return {
        "route": {
            "type": "choice",
            "instructions": "Which lane should handle this node?",
            "criteria": OPTIONS,
        }
    }


def request_shapes() -> list[dict[str, Any]]:
    common = {"model": ROUTER_MODEL, "messages": [{"role": "user", "content": PROMPT}]}
    return [
        {"name": "chat_plain", "url": CHAT_URL, "body": common},
        {
            "name": "chat_json_schema",
            "url": CHAT_URL,
            "body": {
                **common,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "lane_choice",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "choice": {"type": "string", "enum": list(OPTIONS)}
                            },
                            "required": ["choice"],
                            "additionalProperties": False,
                        },
                    },
                },
            },
        },
        {
            "name": "chat_logprobs",
            "url": CHAT_URL,
            "body": {**common, "logprobs": True, "top_logprobs": 5},
        },
        {
            "name": "systemone_body_at_chat_endpoint",
            "url": CHAT_URL,
            "body": {
                "model": ROUTER_MODEL,
                "state": STATE,
                "questions": choice_question(),
            },
        },
        {
            "name": "systemone_router_model",
            "url": SYSTEM_ONE_URL,
            "body": {
                "model": ROUTER_MODEL,
                "state": STATE,
                "questions": choice_question(),
            },
        },
        {
            "name": "systemone_jev_model",
            "url": SYSTEM_ONE_URL,
            "body": {
                "model": JEV_MODEL,
                "state": STATE,
                "questions": choice_question(),
            },
        },
        {
            "name": "decisions_jev_model",
            "url": DECISIONS_URL,
            "body": {
                "model": JEV_MODEL,
                "state": STATE,
                "questions": choice_question(),
            },
        },
    ]


def carries_distribution(body: Any) -> bool:
    if isinstance(body, dict):
        if isinstance(body.get("probabilities"), dict):
            return True
        return any(carries_distribution(value) for value in body.values())
    if isinstance(body, list):
        return any(carries_distribution(value) for value in body)
    return False


def call(key: str, shape: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(shape["body"], separators=(",", ":")).encode()
    if shape["url"] not in {CHAT_URL, SYSTEM_ONE_URL, DECISIONS_URL}:
        raise ValueError(f"Unexpected endpoint: {shape['url']}")
    request = urllib.request.Request(  # noqa: S310 - endpoints are checked above
        shape["url"],
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "reckon-jev-probe/1",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            status = response.status
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read().decode("utf-8", "replace")
    except urllib.error.URLError as error:
        status = None
        raw = json.dumps({"transport_error": str(error.reason)})
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    try:
        response_body: Any = json.loads(raw)
    except json.JSONDecodeError:
        response_body = {"non_json_body": raw}
    return {
        "shape": shape["name"],
        "url": shape["url"],
        "request": shape["body"],
        "http_status": status,
        "latency_ms": elapsed_ms,
        "response": response_body,
        "carries_probability_distribution": carries_distribution(response_body),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, help="Also write the complete redacted JSON receipt"
    )
    parser.add_argument(
        "--repeat", type=int, default=5, help="Identical calls of the best typed shape"
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress stdout")
    args = parser.parse_args()

    key = load_key()
    shapes = request_shapes()
    results = [call(key, shape) for shape in shapes]
    best = next(shape for shape in shapes if shape["name"] == "decisions_jev_model")
    repeats = [call(key, best) for _ in range(args.repeat)]
    latency_values = [item["latency_ms"] for item in repeats]
    receipt = {
        "credential_source": "OPENROUTER_API_KEY_RECKON environment or /home/ITER/mcintos/Code/reckon/.env",
        "credential_redacted": True,
        "question": {"state": STATE, "options": OPTIONS},
        "probes": results,
        "best_shape": best["name"],
        "best_shape_repeats": repeats,
        "best_shape_latency_ms": {
            "calls": len(latency_values),
            "min": min(latency_values),
            "median": round(statistics.median(latency_values), 3),
            "max": max(latency_values),
        },
    }
    rendered = json.dumps(receipt, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    if not args.quiet:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
