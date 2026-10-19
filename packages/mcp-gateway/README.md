# deskweave-mcp-gateway

The MCP (Model Context Protocol) boundary for DeskWeave. Agents such as Codex
connect here; the gateway translates MCP tool calls into runtime queries.

Phase 0 exposes only read-only, side-effect-free tools:

- `runtime_health` — real runtime health (state, schema version, adapters).
- `list_adapters` — registered adapter descriptors.
- `list_capabilities` — flattened capability list.
- `list_tasks` — persisted tasks.

Rules enforced by design:

- internal Python objects are never returned to the agent, only JSON;
- logs go to **stderr**, never stdout, so the stdio protocol stream stays clean;
- every future tool must pass through the runtime permission layer before it can
  cause a side effect (extension point documented in `server.py`).
