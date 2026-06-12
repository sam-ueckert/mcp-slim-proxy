# mcp-slim-proxy

An MCP stdio server that sits between your MCP host (Claude Code, OpenClaw,
Cursor — anything MCP-compatible) and your real MCP servers, re-advertising
their tools with **minified schemas** to cut the context tokens spent on tool
metadata every single request.

```
host (Claude Code / OpenClaw)
   │  sees slim schemas
   ▼
mcp-slim-proxy (stdio)
   │  forwards calls unchanged
   ▼
upstream MCP servers (stdio / SSE / streamable-http)
```

## What gets slimmed

Preserved exactly (everything needed to call the tool correctly):

- type structure, `required`, `enum`, `default`, property names

Reduced:

- tool descriptions → first sentence(s) within a budget (default 160 chars)
- parameter descriptions → trimmed to 80 chars (or dropped entirely)
- `examples`, `title`, `$comment`, `outputSchema`, `annotations` → removed
- markdown decoration stripped, whitespace collapsed

Typical reduction: 60–85% of tool-schema bytes for verbose real-world servers.

## Modes

**`minify`** (default) — every upstream tool is advertised with a slim schema.
Zero behavior change for the model beyond shorter descriptions.

**`defer`** — only two meta-tools are advertised: `find_tools` (keyword search
over the catalog, returns matching slim schemas) and `use_tool` (invoke by
name). Near-zero fixed context cost regardless of how many upstream tools
exist; the model pays for schemas only when it needs them. Pin
always-available tools via `defer.pinned`.

> **OpenClaw note:** OpenClaw ≥2026.6 ships this pattern natively as
> `tools.toolSearch` — prefer that for OpenClaw gateways. This proxy is for
> hosts without an equivalent (Claude Code, Cursor) or when you want minify
> mode's always-visible slim schemas.

## Setup

Requires Python ≥3.10.

```bash
python3 -m venv .venv
.venv/bin/pip install mcp
cp config.example.json config.json   # edit upstreams
```

Config:

```json
{
  "mode": "minify",
  "prefixTools": "auto",
  "minify": {
    "toolDescriptionMaxChars": 160,
    "paramDescriptionMaxChars": 80,
    "dropParamDescriptions": false
  },
  "tools": { "deny": [], "allow": [] },
  "defer": { "pinned": [] },
  "upstreams": [
    { "name": "archy", "transport": "sse", "url": "http://host:30765/sse" },
    { "name": "foreman", "transport": "streamable-http", "url": "http://host:30766/mcp" },
    { "name": "vault", "transport": "stdio", "command": "bash", "args": ["/path/start.sh"] }
  ]
}
```

With multiple upstreams, tools are prefixed `<upstream>__<tool>` to avoid
collisions (`prefixTools: "auto"`; set `true`/`false` to force).

### Claude Code

```bash
claude mcp add slim -- /path/to/mcp-slim-proxy/.venv/bin/python -m mcp_slim_proxy --config /path/to/config.json
```

Then remove the wrapped servers from your direct MCP config so the host only
sees the proxy.

### OpenClaw

```json
"mcp": {
  "servers": {
    "slim": {
      "command": "/path/to/mcp-slim-proxy/.venv/bin/python",
      "args": ["-m", "mcp_slim_proxy", "--config", "/path/to/config.json"]
    }
  }
}
```

## Test

```bash
.venv/bin/python tests/run_test.py
```

Spins up a deliberately verbose upstream, connects through the proxy, and
verifies size reduction, schema-structure preservation, call routing, and
defer-mode meta-tools.

## Limitations

- Upstream `tools/list_changed` notifications are not yet propagated; the
  registry is built once at startup. Restart the proxy after changing an
  upstream's tool set.
