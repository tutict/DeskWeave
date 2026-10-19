# ADR 0003 — MCP as the agent integration boundary

- Status: accepted
- Date: 2026-10-19
- Phase: 0

## Context

An agent (Codex first) must be able to ask DeskWeave what it can do and to trigger
work. Two failure modes are unacceptable: exposing Python objects across the
boundary (no schema, no versioning), and letting the agent reach adapters or the
database directly (no permissions, no audit).

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **MCP server over stdio** | Standard, agent-native; SDK handles framing; no ports; process lifetime = session | One server instance per agent session; no cross-session push; stdio means no remote clients (which is the point) |
| MCP over Streamable HTTP | Multiple clients, can bind later to a UI | Needs auth, CORS and origin checks for a local port; more surface than Phase 0 needs |
| Custom JSON-RPC over the existing control API | Reuses one transport | Non-standard; every agent needs a bespoke adapter |
| Import the runtime as a library in the agent's process | Simplest call path | No process isolation, no permission boundary, couples agent runtime to Python version |

## Decision

Ship a standalone **MCP server over stdio** (`deskweave-mcp-gateway`) that holds one
`Runtime` instance. Tools are read-only in Phase 0:

- `runtime_health`
- `list_adapters`
- `list_capabilities`
- `list_tasks`

Tool arguments are validated twice: the gateway rejects any argument the tool did
not declare, then validates against the tool's published JSON Schema.

## Reasons

1. stdio keeps the local-only promise: there is no port to misconfigure, and the
   OS process boundary is the permission boundary.
2. Tools-not-objects forces serialisable, versionable contracts.
3. One runtime per gateway keeps the permission layer authoritative — the gateway
   has no bypass.
4. Logging to stderr is a hard requirement of stdio transports and is verified by
   a test that asserts stdout stays byte-empty before the first request.

## Consequences

- No server push: the agent must poll to see changes. Acceptable while all tools
  are read-only.
- The gateway is synchronous with respect to the runtime it owns; a long tool call
  blocks that session.
- Adding a side-effecting tool requires going through `Runtime.execute`, which
  means the permission layer and the audit record come for free — but also that a
  tool cannot be "temporarily" fast-pathed around them.

## Revisit if

- Agents need to share one runtime session (then add Streamable HTTP with a
  loopback bind, token auth and an Origin allow-list), or
- a tool must stream progress/notifications, which stdio supports but the current
  tool set does not use.
