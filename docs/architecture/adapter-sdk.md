# Adapter SDK

Status: Phase 0, stable enough to build connectors against.

The SDK is `packages/adapter-sdk` (`deskweave_adapter_sdk`). It defines *how* a
connector behaves; `packages/contracts` defines the data it exchanges. Both are
independent of the runtime, so an adapter can be tested in isolation.

## 1. Lifecycle

```
DISCOVERED ──connect()──► CONNECTED ──disconnect()──► DISCONNECTED
     │                        │                              │
     └── error ──► ERROR ◄────┘                              └─── safe to repeat
```

| Method | Contract |
| --- | --- |
| `discover() -> AdapterDescriptor` | Cheap and side-effect free. Returns the static descriptor. |
| `connect() -> None` | Establish the connection. Raise `AdapterConnectionError` on failure. |
| `get_capabilities() -> list[Capability]` | Capabilities currently offered. May change after `connect()`. |
| `execute(request) -> OperationResult` | Template method owned by the base class — **do not override**. |
| `get_status() -> AdapterStatus` | Current lifecycle state. |
| `disconnect() -> None` | Release every resource. Must be idempotent; must not raise on a second call. |

`disconnect()` is the cleanup contract: the runtime calls it for every registered
adapter on shutdown, logging (not swallowing) failures. An adapter that opens a
browser context, a socket or a subprocess owns closing it.

## 2. Capability model

```python
Capability(
    name="read_text",  # stable, unique inside the adapter
    description="...",
    parameters_schema={...},  # JSON Schema (Draft 2020-12)
    risk=RiskLevel.READ_ONLY,  # info | read_only | moderate | high | critical
    scope=PermissionScope.FILESYSTEM_READ,
    requires_confirmation=False,
)
```

- `parameters_schema` is enforced with `jsonschema.Draft202012Validator` before
  `_run` is called. An empty schema means "no declared constraints".
- `risk` and `scope` are what the permission layer reasons about. They are
  declared by the adapter but *evaluated* centrally.
- Capabilities are also the unit agents see through the MCP gateway, so names are
  part of the external contract: rename = breaking change.

## 3. Operation model

`OperationRequest(capability, arguments, timeout_seconds, reason)` →
`OperationResult(ok, capability, data, error, started_at, finished_at)`.

- `ok` is the single source of truth. `error` is populated only when `ok=False`.
- `data` must be JSON-serialisable: it crosses the HTTP and MCP boundaries.
- Failures are **returned**, not raised. Only programming errors escape.

`execute()` runs, in order:

1. resolve the capability (unknown name → `ValidationError`);
2. validate arguments against the JSON Schema (`ValidationError`);
3. permission check (see §5);
4. `_run` under `asyncio.wait_for` when a timeout is set;
5. wrap any exception into a structured `ErrorInfo`.

## 4. Errors and cancellation

| Situation | Result |
| --- | --- |
| Unknown capability | `ok=False`, `code=validation_error` |
| Bad arguments | `ok=False`, `code=validation_error`, `details.path` |
| Permission denied / needs confirmation | `ok=False`, `code=permission_denied` |
| Deadline exceeded | `ok=False`, `code=adapter_timeout_error` |
| Adapter raised | `ok=False`, `code=adapter_execution_error`, original kept as `__cause__` |

Cancellation is expressed as `asyncio.CancelledError` propagating out of `_run`.
`CancelledError` is **not** caught by `execute()` (it derives from `BaseException`
in Python 3.8+), so cooperative cancellation is preserved. A connector that
spawns subprocesses must terminate them in a `finally` block.

Phase 0 has no cancellation token; a long operation is bounded by
`timeout_seconds` instead. A per-request cancellation handle is a Phase 1 item.

## 5. Permission checks

Every `execute()` calls `PermissionChecker.check(PermissionRequest)` before any
side effect. The runtime wires `PermissionManager` into the registry, and the
registry propagates it to every adapter (`set_permission_checker`).

If **no** checker is wired, the SDK falls back to *deny* for anything that is not
read-only or that requires confirmation. There is no "allow everything" default.

`PermissionManager` policy (Phase 0):

1. `code:execution` scope → deny, always. No generic code-run entry point exists.
2. explicit, unexpired grant for `(adapter_id, capability)` → allow;
3. `risk == read_only` → allow;
4. otherwise → `require_confirmation` (surfaced as denial with
   `decision=require_confirmation`; the confirmation UI is Phase 1).

Adapters must not implement their own policy. If a connector needs a narrower
rule (e.g. a path allow-list), it enforces it *in addition to*, never instead of,
the shared layer.

## 6. Implementing a new connector

```python
from deskweave_adapter_sdk import Adapter
from deskweave_contracts import AdapterDescriptor, AdapterStatus, Capability, RiskLevel


class MyAdapter(Adapter):
    def __init__(self) -> None:
        super().__init__(
            AdapterDescriptor(
                adapter_id="my-app",
                name="My App",
                version="0.1.0",
                capabilities=[
                    Capability(
                        name="do_thing",
                        parameters_schema={
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                        risk=RiskLevel.MODERATE,
                    )
                ],
            )
        )
        self._client = None

    async def connect(self) -> None:
        self._client = await open_client()  # raise AdapterConnectionError on failure
        self._set_status(AdapterStatus.CONNECTED)

    async def get_capabilities(self) -> list[Capability]:
        return self.descriptor.capabilities

    async def _run(self, request):
        value = request.arguments["value"]  # already validated
        return {"echo": value}

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._set_status(AdapterStatus.DISCONNECTED)
```

Checklist:

- [ ] one adapter = one distribution under `adapters/<name>` with its own `pyproject.toml`;
- [ ] declared `adapter_id` is stable and unique;
- [ ] every capability has a JSON Schema (`additionalProperties: false`);
- [ ] `disconnect()` is idempotent and closes everything it opened;
- [ ] no imports from `deskweave_runtime.workflow`, `mcp_gateway`, or the DB layer;
- [ ] contract tests added under `tests/contract/` (see `MockAdapter` for the shape);
- [ ] unimplemented work raises `NotImplementedYetError` — never a fake success.

## 7. Test support

`deskweave_adapter_sdk.mock.MockAdapter` is shipped, not test-only: the runtime,
the MCP gateway and the UI can all be exercised without a real desktop app. It
offers `echo` and `slow` capabilities, and can be constructed to fail on purpose
(`connect_error=...`, `run_error=...`) so contract tests cover error paths.
