"""End-to-end test: spins up the proxy (which spawns verbose_server.py as its
upstream), connects an MCP client to the proxy, and verifies:
  1. all upstream tools are advertised (minify mode)
  2. the advertised schemas are materially smaller than the originals
  3. tool calls route through to the upstream and return its result
  4. defer mode advertises only meta-tools and find_tools/use_tool work

Run: .venv/bin/python tests/run_test.py
"""

import asyncio
import json
import os
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

import mcp.types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
VERBOSE_SERVER = Path(__file__).resolve().parent / "verbose_server.py"

failures = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global failures
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures += 1


def write_config(mode: str) -> str:
    cfg = {
        "mode": mode,
        "upstreams": [
            {
                "name": "verbose",
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(VERBOSE_SERVER)],
            }
        ],
    }
    fd, path = tempfile.mkstemp(prefix=f"mcp-slim-test-{mode}-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    return path


async def connect(stack: AsyncExitStack, command: str, args: list[str]) -> ClientSession:
    params = StdioServerParameters(
        command=command,
        args=args,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


def tools_bytes(tools: list[types.Tool]) -> int:
    return len(json.dumps([t.model_dump(by_alias=True, exclude_none=True) for t in tools]))


async def main() -> None:
    # Baseline: talk to the verbose server directly to measure original size.
    async with AsyncExitStack() as stack:
        direct = await connect(stack, sys.executable, [str(VERBOSE_SERVER)])
        original = (await direct.list_tools()).tools
        original_bytes = tools_bytes(original)

    proxy_args = ["-m", "mcp_slim_proxy"]

    print("minify mode:")
    async with AsyncExitStack() as stack:
        client = await connect(
            stack, sys.executable, proxy_args + ["--config", write_config("minify")]
        )
        tools = (await client.list_tools()).tools
        proxied_bytes = tools_bytes(tools)

        check(
            "all tools advertised",
            len(tools) == len(original),
            f"got {len(tools)}/{len(original)}",
        )
        check(
            "schemas at least 50% smaller",
            proxied_bytes < original_bytes * 0.5,
            f"{original_bytes} → {proxied_bytes}",
        )
        print(
            f"        ({original_bytes} → {proxied_bytes} bytes, "
            f"-{round((1 - proxied_bytes / original_bytes) * 100)}%)"
        )

        search = next((t for t in tools if t.name == "memory_search"), None)
        props = (search.inputSchema or {}).get("properties", {}) if search else {}
        check("structure preserved: required", "query" in (search.inputSchema or {}).get("required", []))
        check("structure preserved: enum", isinstance(props.get("scene", {}).get("enum"), list))
        check("structure preserved: default", props.get("limit", {}).get("default") == 10)
        check(
            "examples dropped",
            '"examples"' not in json.dumps([t.model_dump(by_alias=True) for t in tools]),
        )

        result = await client.call_tool("memory_search", {"query": "hello"})
        text = result.content[0].text if result.content else ""
        check(
            "call routed to upstream",
            text == 'echo:memory_search:{"query":"hello"}',
            repr(text),
        )

    print("defer mode:")
    async with AsyncExitStack() as stack:
        client = await connect(
            stack, sys.executable, proxy_args + ["--config", write_config("defer")]
        )
        tools = (await client.list_tools()).tools
        names = {t.name for t in tools}
        check(
            "only meta-tools advertised",
            names == {"find_tools", "use_tool"},
            ",".join(sorted(names)),
        )

        found = await client.call_tool("find_tools", {"query": "search memories"})
        found_text = found.content[0].text if found.content else ""
        check("find_tools returns matches", "memory_search" in found_text, found_text[:120])

        used = await client.call_tool(
            "use_tool", {"name": "memory_store", "arguments": {"content": "test memory"}}
        )
        used_text = used.content[0].text if used.content else ""
        check(
            "use_tool routes to upstream",
            used_text == 'echo:memory_store:{"content":"test memory"}',
            repr(used_text),
        )

        unknown = await client.call_tool("use_tool", {"name": "nope"})
        check("unknown tool returns isError", unknown.isError is True)

    print("\nall tests passed" if failures == 0 else f"\n{failures} test(s) FAILED")
    sys.exit(0 if failures == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
