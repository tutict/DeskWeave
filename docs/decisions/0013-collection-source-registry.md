# ADR 0013 — A collection source is a name, not a connector class

- Status: accepted
- Date: 2026-10-21
- Phase: 2

## Context

Phase 1 supported one image source. `ReferenceService` opened a collection by
calling the browser connector directly:

```python
self._registry.get(self._browser_adapter_id).execute(op_request)
```

Phase 2 asked whether a new capability can be added *without editing the runtime
core*, and the first concrete test was a second image source. Adding one exposed a
real question: what exactly is a "source"?

Three facts constrain the answer:

- a source (`bing`, `wikimedia`) is **user-visible**: it appears in the MCP tool
  arguments, the HTTP request body and the task record, so it is part of the
  external contract;
- a connector (`browser`, `wikimedia`) is **deployment**: it can be absent (not
  installed, not configured), and its absence must degrade gracefully;
- the workflow must not learn either name. If `start_collection` names a connector,
  every new source edits the workflow, which is exactly the coupling this phase is
  trying to measure.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **Registry: `source → adapter_id`, wired by the composition root** | The workflow resolves a name it was given; `bootstrap.py` is the only place that knows concrete connectors; an absent connector is a runtime fact (`adapter_connection_error` with `details.source`), not an import error | One indirection to read; the mapping must be kept honest by tests |
| Put `source` in `AdapterDescriptor` | The mapping lives with the connector | A descriptor is per-connector, so declaring "I serve `bing`" would put the *contracts* vocabulary inside a connector package and force a contracts release for a new source; also invites a connector to claim a source it does not serve |
| Name the adapter after the source (`WikimediaAdapter` == `source`) | Zero indirection | Conflates two concepts in the one case where it matters: a browser could serve two sources, and a source could be served by a connector with a different name. It also makes "which connector serves `x`" unanswerable without knowing the naming convention |
| Hardcode a `if source == "wikimedia"` branch in the service | Smallest diff today | Grows a branch per source, and the workflow ends up importing concrete connectors — the failure mode this phase is measuring |
| A plugin that registers sources | Right long-term shape | The plugin layer has no workflow/source API yet (see `docs/development/writing-an-adapter.md` §9); inventing one to serve a single source would be speculative |

## Decision

1. **`ReferenceSource` is a shared vocabulary** in `packages/contracts`. Its members
   are the source names that exist. Validation at the HTTP/MCP edge rejects an
   unknown name with `validation_error` before a task row is created.
2. **The runtime holds `source → adapter_id`**, defaulting `bing → browser` (the
   built-in case) and filled by `Runtime.register_collection_source(source,
   adapter_id)` for everything else. `references/service.py` resolves the mapping
   and never names a connector.
3. **An unregistered source fails fast** with `adapter_connection_error`, `503`,
   `details = {source, registered_sources}`, *before* the task is created. A task
   that can never run is worse than a clear failure.
4. **The connector validates the name it was asked for.** A source-serving
   connector declares `{"enum": ["<its own source>"]}` in its schema and rejects
   another source with `validation_error`. `WikimediaAdapter` reads the *raw*
   argument and defaults to `wikimedia` when it is absent — `CollectionRequest`
   defaults to `bing`, and inheriting that default made the connector reject its own
   capability on the first real call (a bug the integration test caught).
5. **No cross-source fallback.** If the chosen source fails, the task fails. Falling
   back to another source would answer a question the user did not ask, and would
   make the collected result unattributable.
6. **No automatic retry of an API call.** A retry inside the connector would hide an
   outage from the person waiting. The real integration test retries a TLS handshake
   (measured: roughly one connection in three behind an intercepting proxy) because
   it needs a stable answer; the product does not.

## Consequences

- Adding a source is: one enum member, one connector package, one
  `register_collection_source` line in `bootstrap.py`. **No change** to
  `references/service.py`, the MCP gateway, the control API or the UI — verified by
  adding `wikimedia` for real.
- The source name is now a durable part of task records (`result.request.source`),
  so renaming a source is a migration, not a refactor. Treated as acceptable:
  sources are few and their names are what users type.
- `ReferenceSource` will accumulate members; when a connector serves several
  sources, the mapping (not the enum) is where that is expressed — one
  `register_collection_source` call per source.
- Tests: `tests/unit/test_wikimedia_adapter.py` (the schema enum, rejection of a
  foreign source, no HTTP call before validation),
  `tests/integration/test_reference_api.py` (routing to the right connector,
  503 for an unregistered source, 400 for an unknown one, attribution reaching the
  artifact), `tests/integration/test_mcp_reference_tools.py` (the gateway forwards
  the source and reports an unregistered one as data, not a protocol error), and
  `tests/contract/test_collection_connector_contract.py` (both connectors declare
  the same capability name and parameter contract).
