"""Test upstream: an MCP stdio server with deliberately verbose tool schemas,
mimicking real-world MCP servers that ship long markdown descriptions,
examples, and titles."""

import asyncio
import json

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def long(s: str) -> str:
    return s * 6


TOOLS = [
    types.Tool(
        name="memory_search",
        description=(
            "Search stored memories using **semantic similarity**. This tool embeds the "
            "query and compares it against all stored memory vectors using cosine similarity. "
            + long(
                "Use it whenever the user refers to something from a past conversation, "
                "a prior decision, or any stored context. "
            )
        ),
        inputSchema={
            "type": "object",
            "title": "MemorySearchInput",
            "description": "Input for memory search",
            "properties": {
                "query": {
                    "type": "string",
                    "title": "Query",
                    "description": "The natural-language search query. "
                    + long(
                        "Phrase it as a question or a topic; the embedding model handles "
                        "either form well. "
                    ),
                    "examples": ["what did we decide about the deploy pipeline"],
                },
                "limit": {
                    "type": "number",
                    "description": "Maximum number of results to return, ranked by similarity score descending.",
                    "default": 10,
                    "examples": [5, 10, 25],
                },
                "scene": {
                    "type": "string",
                    "description": "Optional scene filter. "
                    + long("Scenes partition memories by context, e.g. 'work' or 'home'. "),
                    "enum": ["work", "home", "project", "any"],
                },
            },
            "required": ["query"],
        },
        annotations=types.ToolAnnotations(readOnlyHint=True),
    ),
    types.Tool(
        name="memory_store",
        description="Store a new memory. "
        + long(
            "Memories persist across sessions and are retrievable via memory_search. "
            "Include enough context to be useful standalone. "
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The memory text. "
                    + long("Write it in third person, present tense. "),
                },
                "salience": {"type": "number", "description": "Importance from 0 to 1.", "default": 0.5},
                "tags": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "description": "A lowercase kebab-case tag. "
                        + long("Tags drive the tag-based recall path. "),
                    },
                },
            },
            "required": ["content"],
        },
    ),
    types.Tool(
        name="submit_job",
        description="Submit an engineering job for execution. "
        + long(
            "Jobs run asynchronously on the worker fleet; poll get_job for status. "
            "Provide the full repository URL and a precise task description. "
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Full git URL. " + long("HTTPS form preferred. ")},
                "task": {"type": "string", "description": "What to do. " + long("Be specific about acceptance criteria. ")},
                "priority": {"type": "string", "enum": ["low", "normal", "high"], "default": "normal"},
            },
            "required": ["repo", "task"],
        },
    ),
]


async def main() -> None:
    server: Server = Server("verbose-test")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        return [
            types.TextContent(
                type="text",
                text=f"echo:{name}:{json.dumps(arguments or {}, separators=(',', ':'))}",
            )
        ]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
