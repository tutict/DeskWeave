# deskweave-adapter-sdk

Stable contract for DeskWeave desktop-application connectors.

- `Adapter` — abstract base class implementing the lifecycle template
  (`discover` / `connect` / `get_capabilities` / `execute` / `get_status` /
  `disconnect`). `execute` performs argument validation and a permission check
  before delegating to the adapter's `_run` implementation.
- `AdapterRegistry` — register / look up / unregister adapters by id.
- `PermissionChecker` — protocol implemented by the runtime permission layer.
- `MockAdapter` — a fully working in-memory adapter used by contract tests.

Adapters must not import the runtime, the MCP gateway, or workflow business
logic. They depend only on this SDK and on `deskweave-contracts`.
