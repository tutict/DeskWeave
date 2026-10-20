# Writing an adapter

Audience: someone who has read `docs/architecture/adapter-sdk.md` and wants to add
a capability. Target: a connector that passes its own tests in under an hour,
without editing the runtime.

This guide is honest about what is *done* versus what is *experimental* — see §9.

## 1. Five-minute quick start

```bash
# 1. copy the smallest real connector as a starting point
cp -r adapters/wikimedia adapters/mine
# 2. rename the distribution and the package
#    adapters/mine/pyproject.toml -> name = "deskweave-adapter-mine"
#    src/deskweave_adapter_mine/{__init__,adapter,api}.py
# 3. install it in the workspace (adapters/* is already a uv workspace member)
uv sync --all-packages
# 4. run the two suites that must stay green
uv run pytest tests/contract -q
uv run ruff check . && uv run ruff format --check .
```

The connector is **not** wired yet — see §8. A package that imports and passes its
contract tests is a working adapter; wiring is a one-line change in the composition
root, not in the workflow.

## 2. The minimal adapter

```python
from __future__ import annotations

from typing import Any

from deskweave_adapter_sdk import Adapter
from deskweave_contracts import AdapterDescriptor, AdapterStatus, Capability, RiskLevel

CAPABILITY = "read_widget"


class WidgetAdapter(Adapter):
    def __init__(self) -> None:
        super().__init__(
            AdapterDescriptor(
                adapter_id="widget",
                name="Widget",
                capabilities=[
                    Capability(
                        name=CAPABILITY,
                        description="Read one widget by id.",
                        parameters_schema={
                            "type": "object",
                            "properties": {"widget_id": {"type": "string", "minLength": 1}},
                            "required": ["widget_id"],
                            "additionalProperties": False,
                        },
                        risk=RiskLevel.READ_ONLY,
                    )
                ],
            )
        )

    async def connect(self) -> None:
        self._set_status(AdapterStatus.CONNECTED)

    async def get_capabilities(self) -> list[Capability]:
        return self.descriptor.capabilities

    async def _run(self, request: Any) -> dict[str, Any]:
        return {"widget_id": request.arguments["widget_id"], "value": 42}

    async def disconnect(self) -> None:
        self._set_status(AdapterStatus.DISCONNECTED)
```

What you get for free, in this order, before your `_run` is reached: unknown
capability → `validation_error`; bad arguments → `validation_error` with
`details.path`; permission check; timeout; exception → `adapter_execution_error`.
You do not write any of it.

## 3. Capabilities, permissions, schemas

| Decision | How to express it | Consequence |
| --- | --- | --- |
| touches the network | `scope=PermissionScope.NETWORK` | recorded in the permission request; not automatically denied |
| reads a local file | `scope=PermissionScope.FILESYSTEM_READ` | read-only risk is allowed by the default policy |
| runs a desktop application | `scope=PermissionScope.BLENDER_CONTROL` (or a new scope) + `requires_confirmation=True` | policy returns `require_confirmation`; only a human-granted one-shot allowance gets through |
| should never be allowed | `scope=PermissionScope.CODE_EXECUTION` | **always denied**, no override |

Schema rules that are enforced by tests, not by convention:

- `additionalProperties: false` on every capability — an undeclared argument is an
  error, because a permissive schema is how a "read" tool grows a `path` parameter.
- Enumerate closed sets: `{"enum": ["wikimedia"]}`, not `{"type": "string"}`.
- Bound everything: `minLength`/`maxLength`, `minimum`/`maximum`, `maxItems`.
- **Never accept a filesystem path, a URL to fetch, a shell command, or code.** Take
  an artifact id and call `await self.resolve_artifact(artifact_id)`; take a query
  string and build the URL yourself inside the adapter.

## 4. Test template

Two files, both small. Copy the shape from the Wikimedia connector.

`tests/unit/test_<name>_adapter.py` — the adapter in isolation, transport faked:

```python
class FakeClient:
    def __init__(self, *, delay: float = 0.0, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self.delay, self.error = delay, error

    async def search(self, query: str, limit: int) -> Result:
        self.calls.append((query, limit))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return Result(...)


async def test_rejects_an_undeclared_argument() -> None:
    adapter, client = make(FakeClient())
    result = await adapter.execute(request({"query": "x", "path": "C:\\Windows"}))
    assert result.ok is False
    assert result.error.code == "validation_error"
    assert client.calls == [], "no work may start before validation"
```

Cover at least: happy path, every declared bound, one undeclared field, unknown
capability, permission denied, permission requiring confirmation, timeout,
transport error, unexpected exception, repeated calls are independent.

`tests/contract/test_<name>_contract.py` — the invariants every connector of this
kind shares. The project keeps one parameterised file
(`tests/contract/test_collection_connector_contract.py`) so a new connector is
added by appending to a dict, not by writing another file:

```python
CONNECTORS = {"browser": BrowserAdapter, "wikimedia": WikimediaAdapter}


@pytest.mark.parametrize("adapter", list(CONNECTORS.values()), ids=list(CONNECTORS))
async def test_declares_exactly_one_shared_capability(adapter) -> None: ...
```

A contract test must assert **behaviour**, not literals, where the two are
equivalent. Example from the real file: the browser connector is `MODERATE` risk
and the Wikimedia one is `READ_ONLY`, so asserting `risk is READ_ONLY` would be
false for a correct connector. The test asks the real `PermissionManager` instead
and asserts the decision is `ALLOW`. Assert the property you actually need.

## 5. Debugging

| Symptom | Where to look |
| --- | --- |
| `validation_error` with `details.path` | your JSON Schema disagrees with what you send; the path is the JSON pointer |
| `permission_denied`, `details.decision == "require_confirmation"` | the capability declared `requires_confirmation=True` and nobody granted an allowance — expected outside the confirmation gate |
| `adapter_connection_error` | raised by *you* in `connect()` or the transport; keep the original reason in `details` and as `__cause__` |
| `adapter_execution_error` with `RuntimeError` in the message | an unhandled exception; the class name is preserved deliberately |
| `adapter_timeout_error` | `_run` exceeded `timeout_seconds`; the coroutine was cancelled, not killed — make `finally` blocks safe |
| everything returns `ok=True` but nothing happened | you caught an exception and returned a dict. Return failures, never a fabricated success |

Set `DESKWEAVE_LOG_LEVEL=DEBUG` before running the runtime to see the adapter
lifecycle and the permission decision per call.

## 6. Lifecycle and cleanup

- `connect()` may be called once; `disconnect()` may be called any number of times
  and must not raise on the second call. The runtime disconnects every registered
  adapter at shutdown and **logs** failures instead of swallowing them.
- Anything you open, you close: HTTP sessions, sockets, subprocesses, temporary
  files. A subprocess must be terminated in a `finally` block, and its tree killed
  if it spawns children.
- Temporary files go under the path the runtime gives you (the artifact root). Do
  not write into the repository or the user's home directory.
- `_set_status()` is the only supported way to change lifecycle state; the base
  class owns the descriptor.

## 7. Dependency boundaries

```
contracts  ←  adapter-sdk  ←  your-adapter
```

- Depend on `deskweave-contracts` and `deskweave-adapter-sdk` only.
- **Never** import `deskweave_runtime.*` (workflow, service, persistence, artifacts).
  If you think you need to, the thing you need belongs in the SDK — say so in an
  ADR rather than reaching across.
- Never touch the database, the event bus, or a task row. An adapter that writes
  task state makes recovery undecidable.
- No new third-party dependency without a reason that survives review; the
  Wikimedia connector deliberately uses `urllib` from the standard library so the
  install stays small and the transport stays replaceable in tests.

## 8. Wiring it in

Exactly one file may know that your adapter exists: `runtime/.../bootstrap.py`.

```python
try:
    from deskweave_adapter_mine import MineAdapter

    adapters.append(MineAdapter())
except ImportError as exc:
    logger.warning("mine connector is not installed: %s", exc)
```

Declining to install a connector is not an error: the rest of the runtime keeps
working and `list_adapters` simply does not show it. If your connector is a
reference-image source, also register the name it serves:

```python
if "mine" in registered:
    runtime.register_collection_source("mine", "mine")  # and add the enum member
```

That is the whole integration. If a new connector forces you to edit
`references/service.py`, `mcp_gateway/server.py` or the UI, the SDK is missing an
abstraction — that is worth an ADR, not a patch.

The wiring itself is tested, not assumed: `tests/integration/test_bootstrap.py`
asserts which connectors ship, that each source resolves to its connector, and
that a connector which cannot be imported leaves the others working (it simulates
the ImportError through `sys.modules` rather than uninstalling anything).

## 9. Implemented, experimental, not implemented

| Thing | State |
| --- | --- |
| Adapter SDK: lifecycle, capability model, schema validation, timeouts, structured errors | **implemented**, contract-tested |
| Permission layer, default deny, one-shot allowances | **implemented** |
| Artifact id resolution (`resolve_artifact`) | **implemented** |
| Optional recovery protocol (`RecoverableOutput`) | **implemented**, used by the Blender connector |
| Collection source registry (`register_collection_source`) | **implemented** in Phase 2 |
| `MockAdapter` for tests and demos | **implemented** (`deskweave_adapter_sdk.mock`) |
| Plugin SDK (`Plugin`, `PluginManifest`, `PluginContext`) | **experimental** — the `anime-modeling` plugin registers connectors only; there is no workflow authoring API yet |
| Plugin marketplace, remote plugin loading, signing | **not implemented**, and out of scope until the local contract is stable |
| Per-operation cancellation token | **not implemented** — cancellation is `asyncio` task cancellation plus `timeout_seconds` |
| macOS/Linux-specific connectors | **not implemented** |

### Adapter SDK vs Plugin SDK

They answer different questions and neither wraps the other:

- **Adapter** = *how to talk to one external thing*. One transport, one lifecycle,
  one or more capabilities. Runs inside the runtime process, subject to the
  permission layer.
- **Plugin** = *how several connectors are combined into a workflow*. It receives a
  narrow `PluginContext` (`register_adapter`, `list_adapters`) and must not reach
  the database or bypass permissions.

A plugin that offers a workflow which agents can call **does not exist yet**. Today
a plugin can register connectors and nothing else; the workflow logic still lives
in the runtime. Treat the plugin layer as a seam, not a stable API.
