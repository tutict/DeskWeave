# Adapter SDK

Status: Phase 2. The lead question was whether a new capability can be added
*without* editing the runtime core. The `wikimedia` connector (a second image
source, a completely different transport) was built against this SDK and needed
**zero SDK changes** — see §8 for the one runtime hook it uses and
`docs/decisions/0013-collection-source-registry.md` for why the hook is data, not
code.

The SDK is `packages/adapter-sdk` (`deskweave_adapter_sdk`). It defines *how* a
connector behaves; `packages/contracts` defines the data it exchanges. Both are
independent of the runtime, so an adapter can be tested in isolation.

Everything in §1–§6 is the contract you must satisfy. §7 (recovery) and §8
(collection sources) are **optional** protocols: implement them only if your
connector leaves a durable file behind, or if it is a reference image source.

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

`OperationRequest(capability, arguments, timeout_seconds, reason, task_id)` →
`OperationResult(ok, capability, data, error, started_at, finished_at)`.

- `ok` is the single source of truth. `error` is populated only when `ok=False`.
- `data` must be JSON-serialisable: it crosses the HTTP and MCP boundaries.
- `task_id` ties an operation to the workflow task it serves, so the audit record
  and the artifacts can be joined without trusting the caller's claim.
- Failures are **returned**, not raised. Only programming errors escape.

`execute()` runs, in order:

1. resolve the capability (unknown name → `ValidationError`);
2. validate arguments against the JSON Schema (`ValidationError`, including
   undeclared fields — every schema sets `additionalProperties: false`);
3. permission check (see §5), passing `requires_confirmation` and
   `resource=request.task_id`;
4. `_run` under `asyncio.wait_for` when a timeout is set;
5. wrap any exception into a structured `ErrorInfo`.

### Artifact access

Adapters never accept a filesystem path from the caller. A capability that needs
an artifact takes an **artifact id**, and the adapter resolves it through the
injected resolver:

```python
path = await self.resolve_artifact(artifact_id)  # raises UnsafeResourceError
```

The resolver is owned by the runtime (`ArtifactStore.resolve_path`) and re-checks
that the file is inside the artifact root on every call, so an id — or a row
edited by hand — cannot address an arbitrary file. `AdapterRegistry` installs it
via `set_resource_resolver()` at registration time. With no resolver wired,
`resolve_artifact()` raises `PermissionDeniedError` (no local file can be
addressed at all).

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

There is still no per-operation cancellation token. Long operations are bounded by
`timeout_seconds`, and the reference workflow cancels in-flight work by cancelling
the `asyncio` task that owns the call (see
[`reference-workflow.md`](reference-workflow.md) §8).

## 5. Permission checks

Every `execute()` calls `PermissionChecker.check(PermissionRequest)` before any
side effect. The runtime wires `PermissionManager` into the registry, and the
registry propagates it to every adapter (`set_permission_checker`).

If **no** checker is wired, the SDK falls back to *deny* for anything that is not
read-only or that requires confirmation. There is no "allow everything" default.

`PermissionManager` policy (Phase 1), evaluated in this order:

1. `code:execution` scope → deny, **always**. No generic code-run entry point exists.
2. one-shot allowance for `(adapter_id, capability)` → allow **and consume it**.
   This is how a confirmed `create_reference_plane` gets through exactly once; a
   retry or a stale request cannot reuse the allowance.
3. explicit, unexpired grant for `(adapter_id, capability)` → allow;
4. `risk == read_only` → allow;
5. `browser:control` **without** `requires_confirmation` → allow (reads public
   pages; writes nothing outside the artifact sandbox);
6. otherwise → `require_confirmation`. The SDK turns that decision into
   `permission_denied` with `details.decision == "require_confirmation"`.

`required_confirmation=True` in a capability is a *declaration*, not an
authorisation: the caller cannot satisfy it by passing a flag. Only the runtime
can record a one-shot allowance, and it does so after the workflow has verified
that the task is `waiting_for_confirmation` and that the selection belongs to it.

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
- [ ] capabilities that need local files take **artifact ids** and call
      `resolve_artifact()` — never a caller-supplied path;
- [ ] `disconnect()` is idempotent and closes everything it opened (child
      processes included);
- [ ] no imports from `deskweave_runtime.workflow`, `mcp_gateway`, or the DB layer;
- [ ] contract tests added under `tests/contract/` (see `MockAdapter` for the shape);
- [ ] external content is **data**: a page, an API response or a filename is never
      interpreted as an instruction, and is never used to build a path or a command;
- [ ] a source-serving connector declares its own name in the schema `enum` and
      rejects the others (§8);
- [ ] a connector that writes a durable file implements `RecoverableOutput` (§7),
      or documents why it cannot;
- [ ] unimplemented work raises `NotImplementedYetError` — never a fake success.

## 7. Optional recovery contract

An adapter whose execution leaves a file on disk can answer "did the work already
happen?" after a crash, instead of forcing the user to redo it:

```python
from deskweave_adapter_sdk import RecoverableOutput  # runtime_checkable Protocol


class MyAdapter(Adapter):  # structural: do not inherit, just implement
    async def find_output(self, *, task_id: str, arguments: dict[str, Any]) -> Path | None: ...

    async def verify_output(self, path: Path, *, arguments: dict[str, Any]) -> dict[str, Any]: ...
```

Runtime rules (`runtime/.../references/recovery.py`):

- `find_output` returning `None` means **no evidence**, not "the work failed".
- `verify_output` must be an *independent* check — reopen the file with the target
  application, do not trust the bytes. The Blender connector opens the `.blend` in
  a second process; a truncated file lands in `confirmation_required`, not
  `succeeded`.
- The path is derived from the task id and the request arguments, never from a
  caller-supplied path, and stays inside the adapter's own output directory.
- Recovery **never re-executes**. It either adopts a verified artifact or parks the
  task for a human. A connector that lies here produces a false success, which is
  the one outcome this project treats as unacceptable.

Not implementing the protocol is fine: the runtime falls back to asking the user.

## 8. Serving a reference-collection source

A reference collection takes a `source` name (`bing`, `wikimedia`). The name is
data, not a class: the runtime maps `source → adapter_id` and asks that connector
for the `collect_reference_images` capability.

```python
# composition root only — never from inside an adapter
runtime.register_collection_source("wikimedia", "wikimedia")
```

Three rules keep this honest:

1. **The source name lives in `ReferenceSource`** (`packages/contracts`), the
   vocabulary every connector shares. It is deliberately *not* in
   `AdapterDescriptor`: a descriptor is per-connector, and a new source must not
   force a contracts release.
2. **The connector declares the name in its own parameter schema**
   (`enum: ["wikimedia"]`) and rejects a request for another source instead of
   silently answering for it. A source name is not a capability name.
3. **An unregistered source fails fast with `adapter_connection_error`**
   (`details.source`, `details.registered_sources`) before a task row is created.
   An unknown source name is a `validation_error` at the HTTP/MCP edge.

The `browser` connector serves `bing` because that is the built-in default; every
other source is wired by `bootstrap.py`. Nothing in `references/service.py` knows
the name of a concrete connector.

## 9. Test support

`deskweave_adapter_sdk.mock.MockAdapter` is shipped, not test-only: the runtime,
the MCP gateway and the UI can all be exercised without a real desktop app. It
offers `echo` and `slow` capabilities, and can be constructed to fail on purpose
(`connect_error=...`, `run_error=...`) so contract tests cover error paths.

For workflow-level tests, `tests/support/doubles.py` provides `FakeBrowser`,
`FakeBlender` and a `FakeDownloader` that writes real PNG bytes. The fakes are
registered as ordinary adapters, so they still traverse `execute()` → schema
validation → `PermissionManager`; the trust boundary is exercised, not bypassed.
