# Writing a WASM plugin

A plugin is a directory with two files:

```
my-plugin/
  my_plugin.wasm     the module, built for wasm32-wasip1
  my_plugin.json     the manifest (same stem as the module)
```

`WasmPluginHost.load()` takes the `.wasm` path and looks for `<stem>.json` beside
it. Nothing else is required — no installation step, no registry entry.

For the reference implementation of everything below, read
`examples/wasm-plugins/reference-metadata/` (about 180 lines of Rust). The ABI is
defined in `packages/plugin-wasm/src/deskweave_plugin_wasm/abi.py`.

## 1. The ABI

Your module must export exactly these five names:

```wat
;; an exported linear memory
(memory (export "memory") 1)

;; must return 1, the current ABI_VERSION
(func (export "dw_abi_version") (result i32) i32.const 1)

;; hand the host a buffer of `len` bytes it may write to; 0 means "failed"
(func (export "dw_alloc") (param i32) (result i32) ...)

;; release a buffer the host wrote into
(func (export "dw_free") (param i32 i32) ...)

;; read the request frame at `ptr`, return a pointer to the response frame
(func (export "dw_invoke") (param i32 i32) (result i32) ...)
```

and may import exactly one function:

```wat
(func (import "env" "dw_host_call") (param i32 i32 i32 i32) (result i32))
```

If your module imports anything else, instantiation fails with
`plugin_malformed`. If it is missing an export, `load()` refuses it before any
code runs — and it checks the **signatures**, not just the names, so a
`dw_alloc` with two parameters is a load error rather than a `TypeError`
somewhere inside your first invocation.

### Frames

Both request and response are `u32 little-endian length || UTF-8 JSON`:

```
[0x1A 0x00 0x00 0x00] ["{\"images\":[{\"artifact_id\":\"a\"...}"] 
   ^ 26, the body length         ^ exactly 26 bytes
```

`dw_invoke` receives a pointer to the *whole frame* (header included). It must
return a pointer to a whole frame too.

### Host calls

```c
int32_t dw_host_call(const uint8_t *req_ptr, uint32_t req_len,
                     uint8_t *out_ptr, uint32_t out_cap);
```

- `req_ptr`/`req_len`: a JSON object `{"capability": "...", "payload": {...}}`
  (no length prefix — just the bytes).
- `out_ptr`/`out_cap`: **your** buffer for the response. The host writes there
  only if the response fits; otherwise you get `-5`. A negative `out_ptr` is
  refused the same way — `-5`, not `-4`: `_guest_write` reports "cannot write
  there" for both a negative address and an oversize one, and the format-checking
  happens before anything is written.
- Return value: bytes written, or one of the negative codes below.

| Code | Meaning |
| --- | --- |
| `>= 0` | success; the value is the number of bytes written |
| `-1` | `DENIED_NOT_DECLARED` — your manifest never listed this capability |
| `-2` | `DENIED_NO_GRANT` — declared, but nobody granted it |
| `-3` | `DENIED_UNKNOWN_CAPABILITY` — this host build does not offer it |
| `-4` | `INVALID_ARGUMENT` — payload failed the capability's schema, or the request frame could not be decoded |
| `-5` | `BUFFER_TOO_SMALL` — your `out_cap` is too small |
| `-6` | `HOST_ERROR` — the capability raised, the call timed out, or the host is shutting down |
| `-7` | `BUDGET_EXHAUSTED` — you have used all `max_host_calls` for this invocation |
| `-8` | `COST_EXHAUSTED` — your cost budget cannot cover another call at this capability's weight |
| `-9` | `CONCURRENCY_LIMIT` — no slot for you and no room in the admission queue; nothing ran and nothing was charged |
| `-10` | `CANCELLED` — the operation stopped, confirmed |
| `-11` | `CANCELLATION_UNCONFIRMED` — cancellation was requested and not confirmed in time |
| `-12` | `RATE_LIMITED` — this plugin is calling this capability faster than its window allows |
| `-13` | `QUEUE_TIMEOUT` — you waited `max_queue_wait_s` for a slot and never got one |

`-8`, `-9`, `-12` and `-13` are the four you will meet while tuning, and they mean
four different things:

- `-8` — your *weights* are too high for the budget you asked for. A cheaper
  capability or a larger `max_cost` is the answer, not a retry.
- `-12` — you are calling too fast. The window is one second; the limit is the
  capability's `rate_limit_per_s` or the host default. Back off; retrying in the
  same instant fails the same way.
- `-9` — the host is saturated *and* the queue is full. Since Phase 2.5 a call
  that finds no slot waits up to 0.25 s for one instead of being refused on
  sight, so `-9` now means the pressure was sustained, not momentary.
- `-13` — you waited your 0.25 s and still did not get a turn. Same pressure as
  `-9`, different moment: `-9` = turned away at the door, `-13` = gave up in the
  queue.

None of the four executes anything, so none of them has side effects you have to
reason about. All four are recorded by the host, so returning a happy answer
afterwards does not hide them.

Three rules that matter more than they look:

1. **Check the return value.** `-2` means "denied". Returning a happy summary
   after a denial does not make the invocation succeed — the host records every
   refusal itself and re-checks that record after your module returns — it only
   makes your plugin lie to the person debugging it.
2. **Do not signal failure by returning a success-shaped response.** The host
   cannot tell your `{"ok": true}` from a real one. If the host call was refused,
   your plugin has not done its job; say so in your response.
3. **Do not loop on a refusal.** Every attempt spends `max_host_calls`, including
   a `-4` from a malformed request and a `-12` from calling too fast. A retry loop
   is the fastest way to reach `-7`, and `-7` fails the whole invocation as a
   resource problem rather than an authorisation one.

### What `-6` does and does not mean

The capability is bounded by the host's call timeout, but a timeout is **not** a
cancellation. If the capability is synchronous (a plain `def` in Python), the host
stops waiting and the work keeps running; you get `-6` and the audit records
`state="timed_out"`, `confirmed_stopped=false`. Only a coroutine capability can
actually be stopped, and even then the host waits out a grace period before it
says so. Nothing about this is a property your plugin can rely on — treat `-6` as
"this did not finish in time" and do not retry a call whose side effects you are
unsure about.

## 2. The manifest

```json
{
  "plugin_id": "my-plugin",
  "name": "My plugin",
  "version": "0.1.0",
  "interface_version": 1,
  "requested_capabilities": ["reference.metadata.validate"],
  "input_schema":  { "type": "object", "required": ["images"], "...": "..." },
  "output_schema": { "type": "object", "required": ["count"], "...": "..." },
  "limits": { "timeout_ms": 2000, "fuel": 50000000, "max_memory_bytes": 16777216, "max_host_calls": 16, "max_cost": 32 }
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `plugin_id` | yes | `^[a-z0-9][a-z0-9._-]*$`, max 64. Also the audit subject: `wasm-plugin:<id>` |
| `name` | yes | display name, max 128 |
| `version` | no | defaults to `0.0.0` |
| `interface_version` | no | defaults to `1`; a mismatch is refused at load |
| `entry` | no | defaults to `dw_invoke` |
| `requested_capabilities` | no | what you *intend* to call. A declaration, never a grant |
| `input_schema` / `output_schema` | no | Draft 2020-12. Empty means "not checked" — declare them |
| `limits` | no | clamped by host ceilings, see below |
| `wasm_path` | no | the module, as a path **relative to the manifest's directory**. Defaults to the historical convention (a sibling with the same stem). Absolute paths, drive letters, UNC prefixes and `..` components are load errors, not things to be normalised |
| `wasm_sha256` | no | 64 lowercase hex characters. Optional because every pre-2.5 manifest lacks it; a host constructed with `require_integrity=True` refuses a manifest without it. This is *content* identity: it detects substitution, it does not say who wrote the file |

Unknown fields are rejected (`extra="forbid"`), so a typo is a load error rather
than a silently ignored line.

### Pinning the module

A hash in the manifest answers "are these the bytes the manifest describes". It
does **not** answer "are these the bytes that were installed" — an attacker who
replaces the module can replace the manifest alongside it, and the two agree
perfectly. For that second question there is an install record, written by
`deskweave_plugin_wasm.integrity.install()` next to the manifest:

```json
{
  "record_version": 1,
  "plugin_id": "my-plugin",
  "version": "0.1.0",
  "interface_version": 1,
  "wasm_path": "my_plugin.wasm",
  "wasm_sha256": "<64 hex>",
  "size_bytes": 41216,
  "declared_capabilities": ["reference.metadata.validate"]
}
```

`install()` writes it atomically (`os.replace`), refuses to overwrite an existing
pin unless `replace=True`, and refuses to reinstall a different build under the
same version. On load, a host constructed with `require_install_record=True`
verifies against it and refuses on any drift — a changed module, a renamed path,
a different version, or a capability the plugin did not declare when it was
pinned. The error tells you *what* changed, never *who* changed it, and never
leaks an absolute path.

None of this is a signature. There is no publisher key and no trust root; see
[plugin installation](../architecture/plugin-installation.md) §8.

### Limits

| Field | Default | Ceiling |
| --- | --- | --- |
| `timeout_ms` | 2000 | 30 000 |
| `fuel` | 50 000 000 | 5 000 000 000 |
| `max_memory_bytes` | 16 MiB | 256 MiB |
| `max_input_bytes` | 256 KiB | 1 MiB |
| `max_output_bytes` | 256 KiB | 1 MiB |
| `max_host_calls` | 64 | 1 024 |
| `max_cost` | 64 | 4 096 |

Asking for more than a ceiling is a validation error. You cannot widen your own
budget.

`max_host_calls` bounds how many times you may call `dw_host_call` in **one
invocation** — refusals count, so it is a bound on attempts, not successes. It
resets on every invocation; it does not accumulate across a plugin's lifetime.
Each capability tells you what it costs in
[the capability catalogue](../architecture/wasm-capability-catalogue.md); if the
number you need is above the ceiling, the answer is fewer calls, not a bigger
number.

`max_cost` bounds the same calls by *weight*. A capability declares how expensive
it is; the two budgets are independent, and whichever runs out first stops you.
A refused call costs neither. This is not a measurement of CPU time — it is a
declared price, so treat it as "how many of these am I allowed" rather than "how
long may I run".

**Budget realistically.** The example plugin spends **198 483** fuel for two
images and finishes in ~1.5 ms. Fuel is deterministic, so a number that works
once works every time — but a plugin doing real parsing may need 10–100× that.
Set `fuel` from a measurement, not a guess: run once with a generous budget and
read `InvocationResult.fuel_used`.

## 3. Building

Rust example:

```bash
rustup target add wasm32-wasip1          # once
cd examples/wasm-plugins/reference-metadata
cargo build --release --target wasm32-wasip1
```

or from the repo root, which also copies the artifact next to the manifest and
prints its sha256:

```bash
uv run python scripts/build_wasm_plugin.py
```

Keep the release profile small — the example uses `opt-level = "s"`, `lto`,
`codegen-units = 1`, `panic = "abort"` and `strip`, and lands at 99 KB:

```toml
[profile.release]
opt-level = "s"
lto = true
codegen-units = 1
panic = "abort"
strip = true
```

Other languages that can emit `wasm32-wasip1` (TinyGo, Zig, C with
`wasm32-wasi-sdk`, AssemblyScript) work the same way: export the five names,
import nothing but `env.dw_host_call`. Nothing in the host is Rust-specific.

## 4. Running it

```python
import asyncio
from pathlib import Path

from deskweave_contracts import PermissionRequest, PermissionScope, RiskLevel
from deskweave_plugin_wasm import WasmPluginHost, default_registry, principal_for
from deskweave_runtime import Runtime, RuntimeConfig


async def main() -> None:
    runtime = await Runtime.create(RuntimeConfig(data_dir=Path("./data")))
    await runtime.start()
    try:
        host = WasmPluginHost(registry=default_registry(runtime.permissions))
        plugin = host.load(Path("my-plugin/my_plugin.wasm"))

        # Without this, the call is denied with `plugin_capability_denied`.
        await runtime.permissions.grant(
            PermissionRequest(
                adapter_id=principal_for(plugin.manifest.plugin_id),
                capability="reference.metadata.validate",
                scope=PermissionScope.PLUGIN_HOST_CALL,
                risk=RiskLevel.LOW,
            ),
            granted_by="me",
        )

        result = await host.invoke(plugin, {"images": [...]})
        if result.ok:
            print(result.data, f"{result.fuel_used} fuel, {result.duration_ms:.1f} ms")
        else:
            print(result.error.code, result.error.details)
    finally:
        await runtime.close()


asyncio.run(main())
```

`invoke()` never raises for a plugin failure — `result.ok` is the only thing you
need to branch on. `result.host_calls` holds the audit trail of every host call
that was attempted, including refusals.

The example loads with the defaults, which means `wasm_sha256` is recorded but
not enforced. For anything you actually install, turn the check into a condition:

```python
host = WasmPluginHost(
    registry=default_registry(runtime.permissions),
    require_integrity=True,  # manifest hash must match
    # require_install_record=True, # ...and the pin written by integrity.install()
)
```

With `require_integrity=True` a manifest without `wasm_sha256` is refused rather
than grandfathered in, so this is a switch you flip once your manifests carry the
field. Note what it buys: it proves the file did not change since the manifest was
written. It does not prove who wrote it.

## 4.5 Getting it approved

A host built on `PluginLifecycle` adds one step between "the file is intact" and
"the module runs": somebody has to approve the capability set for that exact
build. The code above bypasses that by granting directly — fine for an experiment,
not for anything a user installs.

```python
from deskweave_plugin_wasm import JsonApprovalStore, PluginLifecycle

lifecycle = PluginLifecycle(
    registry=default_registry(runtime.permissions),
    permissions=runtime.permissions,
    store=JsonApprovalStore("./data/plugin-approvals.json"),
)

await lifecycle.discover("my-plugins")  # reads manifests; loads nothing
await lifecycle.install("my-plugins/my_plugin.json")  # pins the bytes
await lifecycle.approve("my-plugin", ["reference.metadata.validate"])  # a decision
plugin = await lifecycle.activate("my-plugins/my_plugin.wasm")  # only now
```

| Step | Creates a grant? | What it refuses |
| --- | --- | --- |
| `discover` | no | — (never loads a module) |
| `install` | no | a different plugin in the same directory; a conflicting version without `replace=True` |
| `approve` | **yes**, for exactly the approved set | a capability the manifest never requested; a capability this host does not offer |
| `activate` | no | any build no current approval covers |

Five things will surprise you if you have not read
[plugin-lifecycle.md](../architecture/plugin-lifecycle.md):

1. **Approving a subset is normal.** `approve(id, [c1])` on a manifest that
   requests `c1, c2` grants `c1` only, and `c2` stays refused at dispatch.
2. **An approval is a set, not a growing list.** A later `approve(id, [c1])`
   where `c1` was already granted *removes* `c2` if it had been granted before.
   The record and the grants are reconciled, so what the human left out stops
   working — it does not linger because it was approved once.
3. **Reinstalling a new build clears the approval; reinstalling the same bytes
   does not.** `install(replace=True)` on a different module moves the plugin back
   to `installed`, and the old decision is kept only long enough to explain *why*
   activation is refused (`added`, `dropped`, `requires_reapproval`). An install
   whose ABI version, bytes, version and requested set all match — reached through
   *any* spelling of the same manifest path, relative or absolute — writes nothing
   at all, so a build script that re-installs on every run will not silently
   discard an approval. `last_detail["record_mismatch"]` names the reason when it
   does not short-circuit.
4. **A read-only capability is not self-authorising, and neither is a grant.** Two
   separate checks run at dispatch: the permission layer must answer `ALLOW` with
   provenance `GRANT` or `ONE_SHOT` (policy defaults are refused with
   `decision="policy_default"`), *and* a current approval must cover that
   capability for that build (otherwise `decision="not_approved"`). Writing a
   grant row by hand buys you nothing.
5. **Revocation takes effect on the next call, not the current one.** A call still
   waiting in the queue is removed and refused with `decision="revoked"`; a call
   already inside your module runs to completion. `RevocationReport` separates the
   three cases — `queued_stopped=N`, `running=M`, `stopped=False` — rather than
   pretending anything was killed. The block is shared by every host serving the
   same approval store, so a queued call in another host over that store is refused
   too; the counts are still per host, because only one queue is drained. Two
   *processes* on one store do not share it.

`scripts/plugin_admin.py` can tell you what state a directory is in without
changing anything:

```bash
uv run python scripts/plugin_admin.py inspect my-plugin --plugins-dir my-plugins
```

It prints `requested`, `approved` and `granted` side by side, plus the pin state
(`pinned-and-matching`, `pinned-but-modified`, `record-missing`, `module-missing`).
Exit code 3 means drift. There is deliberately no verb that grants anything — see
[plugin-lifecycle.md](../architecture/plugin-lifecycle.md) §10.

## 5. Getting it wrong: the errors you will actually hit

| Symptom | Cause |
| --- | --- |
| `plugin_abi_mismatch` at load | `dw_abi_version` returns something other than `1`, or the manifest says another `interface_version` |
| `plugin_malformed` — "memory minimum size … exceeds memory limits" | Your module declares a minimum linear memory larger than `max_memory_bytes` |
| `plugin_malformed` — "export … has the wrong signature" | An export exists with the right name but the wrong arity or types. Check `wasm-objdump -x` |
| `plugin_malformed` — import error | You imported something the host does not provide. Check `wasm-objdump -x` on your module |
| `plugin_invalid_output` — "declares N bytes but only M are readable" | The length prefix does not match the body you wrote |
| `plugin_invalid_output` — "must be a JSON object" | You returned an array or a bare string |
| `plugin_capability_undeclared` | You called a capability that is not in `requested_capabilities` |
| `plugin_capability_denied` | Declared, but no grant. See §4 |
| `plugin_capability_denied` with `decision "policy_default"` | The capability was allowed by *policy* (typically read-only), not by a grant. A plugin call needs a decision: approve it (§4.5) |
| `plugin_not_approved` at activation | No current approval covers this build: never approved, re-pinned, new version, different bytes, or the manifest grew a capability. `details["reason"]` and `details["added"]` name which |
| `plugin_lifecycle` | A sequencing or naming error: activating something not installed, approving a capability the manifest never requested, or one the host does not offer |
| `plugin_host_error` | The capability raised, or the host call timed out, or it was cancelled. Not your bug and not a denial — check `error.details["state"]` |
| `plugin_resource_exhausted` — `OUT_OF_FUEL` | Fuel ran out. Measure a real run and raise `fuel` |
| `plugin_resource_exhausted` — budget | You made more than `max_host_calls` host calls. Batch your requests |
| `plugin_resource_exhausted` — `details.state == "refused"` with `decision "cost"` | Your cost budget cannot cover another call at this capability's weight. Fewer or cheaper calls |
| `plugin_resource_exhausted` — `details.state == "refused"` with `decision "concurrency"` | The host is saturated (or your own earlier calls are still running). Wait for them; retrying immediately cannot help |
| `plugin_resource_exhausted` — `decision "queue_full"` / `"queue_timeout"` | Same pressure as above, past the admission queue: 16 waiters and 0.25 s were not enough. Back off, do not spin |
| `plugin_resource_exhausted` — `decision "rate"` | You are calling this capability faster than `rate_limit_per_s` (or the host default) allows in a one-second window. Batch or back off |
| `plugin_integrity` at load | The module on disk is not the module the manifest describes, or not the one that was pinned. Something else wrote the file, or you forgot to rebuild and re-pin |
| `plugin_install` | A pin already exists for this plugin (reinstall without `replace=True`), or the same `version` names different bytes. Bump the version |
| `plugin_timeout` | Wall clock ran out. Usually a genuine infinite loop |
| `memory.grow` returns `-1` | The memory cap is working. Check the result; do not assume growth succeeded |
| `invoke()` returned `-4` twice in a row | Your request frame is malformed. Decode errors and schema rejections share the code; the audit record in `result.host_calls` carries the reason |

## 6. What you cannot do

By design, and not configurable:

- read a file, list a directory, or check whether a path exists;
- open a socket, resolve a name, or make an HTTP request;
- start a process, run a command, or load another module;
- read the environment, the clock, or the host's memory;
- persist anything between invocations — you get a fresh `Store` every call, so
  in-memory state does not survive;
- reach Blender, the database or the artifact store except through a granted
  capability.

If a plugin needs one of these, the answer is a new `HostCapability` in the host,
where the policy is visible and auditable — not a new import in the plugin.
