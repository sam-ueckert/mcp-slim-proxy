"""Tool schema minification.

Reduces the byte/token cost of MCP tool definitions while preserving
everything the model needs to call the tool correctly:
  - type structure, required fields, enums, defaults stay intact
  - description prose is trimmed to the first sentence(s) within a budget
  - decorative/duplicative keys (examples, title, $comment) are dropped
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

DROP_KEYS = {"examples", "title", "$comment", "$schema", "deprecated"}

# Schema keys whose values are themselves schemas (or lists of schemas).
NESTED_SCHEMA_KEYS = {
    "items",
    "additionalProperties",
    "contains",
    "not",
    "if",
    "then",
    "else",
    "anyOf",
    "oneOf",
    "allOf",
    "prefixItems",
}

# Schema keys whose values are name → schema maps.
SCHEMA_MAP_KEYS = {"properties", "patternProperties", "$defs", "definitions"}

_MD_PATTERNS = [
    (re.compile(r"```.*?```", re.DOTALL), ""),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\*\*([^*]*)\*\*"), r"\1"),
    (re.compile(r"__([^_]*)__"), r"\1"),
    (re.compile(r"^#+\s*", re.MULTILINE), ""),
]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class MinifyOptions:
    tool_description_max_chars: int = 160
    param_description_max_chars: int = 80
    drop_param_descriptions: bool = False
    strip_markdown: bool = True

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> "MinifyOptions":
        cfg = cfg or {}
        return cls(
            tool_description_max_chars=cfg.get("toolDescriptionMaxChars", 160),
            param_description_max_chars=cfg.get("paramDescriptionMaxChars", 80),
            drop_param_descriptions=cfg.get("dropParamDescriptions", False),
            strip_markdown=cfg.get("stripMarkdown", True),
        )


def _clean_text(text: str, strip_markdown: bool) -> str:
    t = _WS.sub(" ", str(text)).strip()
    if strip_markdown:
        for pattern, repl in _MD_PATTERNS:
            t = pattern.sub(repl, t)
        t = _WS.sub(" ", t).strip()
    return t


def trim_description(text: str, max_chars: int, strip_markdown: bool = True) -> str:
    """Take whole sentences from the front of `text` until adding the next one
    would exceed `max_chars`. Always returns at least something (hard-truncated
    first sentence if it alone exceeds the budget)."""
    if not text:
        return text
    cleaned = _clean_text(text, strip_markdown)
    if len(cleaned) <= max_chars:
        return cleaned

    out = ""
    for sentence in _SENTENCE_SPLIT.split(cleaned):
        candidate = f"{out} {sentence}" if out else sentence
        if len(candidate) > max_chars:
            break
        out = candidate
    if not out:
        out = cleaned[: max_chars - 1].rstrip() + "…"
    return out


def _minify_schema(node: Any, opts: MinifyOptions, depth: int = 0) -> Any:
    if isinstance(node, list):
        return [_minify_schema(n, opts, depth) for n in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in DROP_KEYS:
            continue

        if key == "description" and isinstance(value, str):
            # depth 0 is the schema root; its description duplicates the tool
            # description in most servers — drop it there, trim elsewhere.
            if depth == 0 or opts.drop_param_descriptions:
                continue
            trimmed = trim_description(value, opts.param_description_max_chars, opts.strip_markdown)
            if trimmed:
                out["description"] = trimmed
            continue

        if key in SCHEMA_MAP_KEYS:
            out[key] = {
                name: _minify_schema(schema, opts, depth + 1)
                for name, schema in (value or {}).items()
            }
            continue

        if key in NESTED_SCHEMA_KEYS:
            out[key] = _minify_schema(value, opts, depth + 1)
            continue

        out[key] = value
    return out


def minify_tool(tool: dict[str, Any], opts: MinifyOptions | None = None) -> dict[str, Any]:
    """Minify a single MCP tool definition (plain dict form). Returns a new dict."""
    opts = opts or MinifyOptions()
    out = dict(tool)
    if tool.get("description"):
        out["description"] = trim_description(
            tool["description"], opts.tool_description_max_chars, opts.strip_markdown
        )
    if tool.get("inputSchema"):
        out["inputSchema"] = _minify_schema(tool["inputSchema"], opts, 0)
    # outputSchema and annotations are pure context cost for the model
    out.pop("outputSchema", None)
    out.pop("annotations", None)
    return out


def minify_tool_list(
    tools: list[dict[str, Any]], opts: MinifyOptions | None = None
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Minify a list of tool dicts. Returns (tools, stats) where stats reports
    the serialized size before and after."""
    before = len(json.dumps(tools))
    minified = [minify_tool(t, opts) for t in tools]
    after = len(json.dumps(minified))
    return minified, {
        "count": len(tools),
        "bytes_before": before,
        "bytes_after": after,
        "saved": before - after,
        "saved_pct": round((before - after) / before * 100) if before else 0,
    }
