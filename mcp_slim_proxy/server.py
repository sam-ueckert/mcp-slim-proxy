"""mcp-slim-proxy — an MCP stdio server that fans out to one or more upstream
MCP servers and re-advertises their tools with minified schemas, cutting the
context tokens a host (Claude Code, OpenClaw, Cursor, …) spends on tool
metadata.

Modes:
  minify (default) — every upstream tool is advertised, schemas slimmed.
  defer            — only two meta-tools are advertised (find_tools, use_tool);
                     full schemas are fetched on demand. Tools listed in
                     defer.pinned are still advertised directly.

Usage: mcp-slim-proxy [--config path/to/config.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import mcp.types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .minify import MinifyOptions, minify_tool_list

SEP = "__"


def log(msg: str) -> None:
    # stdout is the MCP protocol channel — all logging goes to stderr.
    print(f"[mcp-slim-proxy] {msg}", file=sys.stderr, flush=True)


# ── Config ───────────────────────────────────────────────────────────────────


def load_config(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(prog="mcp-slim-proxy")
    parser.add_argument(
        "--config",
        default=os.environ.get("MCP_SLIM_CONFIG", "config.json"),
        help="path to config JSON (default: ./config.json or $MCP_SLIM_CONFIG)",
    )
    args = parser.parse_args(argv)
    config_path = os.path.abspath(args.config)
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as err:
        log(f"failed to load config at {config_path}: {err}")
        raise SystemExit(1)
    upstreams = cfg.get("upstreams")
    if not isinstance(upstreams, list) or not upstreams:
        log('config error: "upstreams" must be a non-empty array')
        raise SystemExit(1)
    return cfg


# ── Upstream connections ─────────────────────────────────────────────────────


@dataclass
class Connection:
    name: str
    session: ClientSession


async def connect_upstream(upstream: dict[str, Any], stack: AsyncExitStack) -> ClientSession:
    transport = upstream.get("transport") or ("sse" if upstream.get("url") else "stdio")
    if transport == "stdio":
        params = StdioServerParameters(
            command=upstream["command"],
            args=upstream.get("args", []),
            env={**os.environ, **upstream.get("env", {})},
        )
        read, write = await stack.enter_async_context(stdio_client(params))
    elif transport == "sse":
        read, write = await stack.enter_async_context(sse_client(upstream["url"]))
    elif transport in ("streamable-http", "http"):
        read, write, _ = await stack.enter_async_context(streamablehttp_client(upstream["url"]))
    else:
        raise ValueError(f'unknown transport "{transport}" for upstream "{upstream.get("name")}"')

    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


# ── Registry ─────────────────────────────────────────────────────────────────


@dataclass
class RegistryEntry:
    upstream_name: str
    session: ClientSession
    real_name: str
    tool: dict[str, Any]  # original tool as dict
    minified: dict[str, Any]  # minified tool dict, name already rewritten


async def build_registry(
    connections: list[Connection], cfg: dict[str, Any]
) -> dict[str, RegistryEntry]:
    prefix_cfg = cfg.get("prefixTools", "auto")
    prefix = prefix_cfg is True or (prefix_cfg != False and len(connections) > 1)  # noqa: E712

    deny = set(cfg.get("tools", {}).get("deny", []))
    allow_list = cfg.get("tools", {}).get("allow", [])
    allow = set(allow_list) if allow_list else None
    opts = MinifyOptions.from_config(cfg.get("minify"))

    registry: dict[str, RegistryEntry] = {}
    total_before = total_after = 0

    for conn in connections:
        try:
            result = await conn.session.list_tools()
        except Exception as err:
            log(f'listTools failed for "{conn.name}": {err}')
            continue
        tools = [t.model_dump(by_alias=True, exclude_none=True) for t in result.tools]
        minified, stats = minify_tool_list(tools, opts)
        total_before += stats["bytes_before"]
        total_after += stats["bytes_after"]

        for tool, slim in zip(tools, minified):
            advertised = f"{conn.name}{SEP}{tool['name']}" if prefix else tool["name"]
            if advertised in deny:
                continue
            if allow and advertised not in allow:
                continue
            if advertised in registry:
                log(f'name collision on "{advertised}" — keeping first, dropping {conn.name}\'s')
                continue
            registry[advertised] = RegistryEntry(
                upstream_name=conn.name,
                session=conn.session,
                real_name=tool["name"],
                tool=tool,
                minified={**slim, "name": advertised},
            )
        log(
            f"{conn.name}: {len(tools)} tool(s), schema bytes "
            f"{stats['bytes_before']} → {stats['bytes_after']} (-{stats['saved_pct']}%)"
        )

    if total_before:
        pct = round((total_before - total_after) / total_before * 100)
        log(f"total: {len(registry)} advertised, {total_before} → {total_after} bytes (-{pct}%)")
    return registry


# ── Defer-mode meta tools ────────────────────────────────────────────────────

FIND_TOOLS = types.Tool(
    name="find_tools",
    description=(
        "Search the available tool catalog by keywords. Returns matching tool "
        "names and their input schemas. Call this before use_tool."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "keywords to match against tool names and descriptions",
            },
            "limit": {"type": "number", "description": "max results (default 5)"},
        },
        "required": ["query"],
    },
)

USE_TOOL = types.Tool(
    name="use_tool",
    description=(
        "Invoke a tool from the catalog by its exact name (as returned by "
        "find_tools) with its arguments."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "exact tool name from find_tools"},
            "arguments": {
                "type": "object",
                "description": "arguments matching the tool's input schema",
            },
        },
        "required": ["name"],
    },
)


def search_registry(
    registry: dict[str, RegistryEntry], query: str, limit: int
) -> list[dict[str, Any]]:
    terms = [t for t in str(query).lower().split() if t]
    scored: list[tuple[int, str, RegistryEntry]] = []
    for name, entry in registry.items():
        hay = f"{name} {entry.tool.get('description', '')}".lower()
        score = 0
        for t in terms:
            if t in name.lower():
                score += 3
            elif t in hay:
                score += 1
        if score:
            scored.append((score, name, entry))
    scored.sort(key=lambda s: -s[0])
    return [entry.minified for _, _, entry in scored[:limit]]


def _dict_to_tool(d: dict[str, Any]) -> types.Tool:
    return types.Tool(
        name=d["name"],
        description=d.get("description"),
        inputSchema=d.get("inputSchema", {"type": "object"}),
    )


# ── Main ─────────────────────────────────────────────────────────────────────


async def run(cfg: dict[str, Any]) -> None:
    mode = "defer" if cfg.get("mode") == "defer" else "minify"
    pinned = set(cfg.get("defer", {}).get("pinned", []))

    async with AsyncExitStack() as stack:
        connections: list[Connection] = []
        for upstream in cfg["upstreams"]:
            name = upstream.get("name", "upstream")
            try:
                session = await connect_upstream(upstream, stack)
                connections.append(Connection(name=name, session=session))
                log(f'connected upstream "{name}"')
            except Exception as err:
                log(f'failed to connect upstream "{name}": {err} — skipping')
        if not connections:
            log("no upstreams connected; exiting")
            raise SystemExit(1)

        registry = await build_registry(connections, cfg)

        server: Server = Server("mcp-slim-proxy")

        @server.list_tools()
        async def list_tools() -> list[types.Tool]:
            if mode == "minify":
                return [_dict_to_tool(e.minified) for e in registry.values()]
            tools = [FIND_TOOLS, USE_TOOL]
            tools += [_dict_to_tool(e.minified) for n, e in registry.items() if n in pinned]
            return tools

        @server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any] | None) -> Any:
            arguments = arguments or {}

            if mode == "defer" and name == "find_tools":
                results = search_registry(
                    registry, arguments.get("query", ""), int(arguments.get("limit", 5))
                )
                text = (
                    json.dumps(results, indent=1)
                    if results
                    else "No tools matched. Try broader keywords."
                )
                return [types.TextContent(type="text", text=text)]

            target_name, target_args = name, arguments
            if mode == "defer" and name == "use_tool":
                target_name = arguments.get("name")
                target_args = arguments.get("arguments") or {}
                if not target_name:
                    raise ValueError("use_tool requires a 'name' argument.")

            entry = registry.get(target_name)
            if entry is None:
                raise ValueError(f'Unknown tool "{target_name}".')

            result = await entry.session.call_tool(entry.real_name, target_args)
            if result.isError:
                texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
                raise RuntimeError("; ".join(texts) or "upstream tool call failed")
            return result.content

        log(f"ready (mode={mode}, {len(registry)} tool(s) from {len(connections)} upstream(s))")
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


def cli() -> None:
    cfg = load_config()
    asyncio.run(run(cfg))


if __name__ == "__main__":
    cli()
