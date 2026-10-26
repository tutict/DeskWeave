# Host capability catalogue

The complete list of things a WASM plugin can ask the host to do. As of Phase
2.3 this is **one** capability, deliberately: the point of the phase is the
boundary, not the catalogue.

A plugin cannot reach anything not on this list. There is no capability that
accepts a file path, a URL, a command line or a snippet of Python, and that is a
design decision rather than an omission — see "Explicitly not offered" below.

## The format

Every entry states the same eight things, because a capability that cannot
answer all eight is not ready to be exposed:

| Field | Why it is required |
| --- | --- |
| Name and version | What the plugin writes in `dw_host_call`, and how it changes |
| Input / output schema | What is validated, and where |
| Authorisation rule | Which principal, scope and risk the permission layer sees |
| Side effects | Whether calling it twice is safe, and whether it touches the world |
| Cost and timeout | The declared cost weight, what the host spends, and what bounds it |
| Rate | `rate_limit_per_s` — calls allowed per second per plugin, or `None` for the host default (Phase 2.5) |
| Cancellation | Whether the operation can be stopped, and what "stopped" is based on |
| Audit fields | What survives in `InvocationResult.host_calls` |
| Failure semantics | What the plugin is told, and what the caller sees |

## `reference.metadata.validate`

| | |
| --- | --- |
| **Name** | `reference.metadata.validate` |
| **Version** | *none of its own.* The wire format is covered by `ABI_VERSION` (1); the capability's own shape is not versioned. See "Gaps". |
| **Input schema** | `{image: {artifact_id: str(1..64), title: str(1..200), width: int(1..100000), height: int(1..100000), license?: str(≤100), author?: str(≤200)}}`, `additionalProperties: false` at both levels, `required: [artifact_id, title, width, height]` |
| **Output schema** | `{valid: bool, problems: [{field: str, reason: str}]}` — problems only, never an echo of the input |
| **Authorisation** | Principal `wasm-plugin:<plugin_id>`, scope `PermissionScope.PLUGIN_HOST_CALL`, risk `RiskLevel.LOW`. Requires an explicit grant or a one-shot allowance; **never** auto-allowed (that is why it is `LOW`, not `READ_ONLY`) |
| **Side effects** | None. Pure computation on values already in the payload. Idempotent, order-independent, no I/O, no clock, no randomness |
| **Cost** | `cost = 1` — the cheapest non-trivial weight. A `max_cost` budget of 64 buys 64 of these. Real spend is microseconds of CPU |
| **Rate** | `rate_limit_per_s` is *not declared*, so the call falls back to the executor's `default_rate_per_s` — which is also `None` (no ceiling) unless the host configures one. Deliberate: this capability is pure and cheap, and a tight ceiling would only punish batch validation. Any value that *is* set is clamped to `MAX_RATE_PER_S` (1000/s) |
| **Cancellation** | `supports_cancellation = False`, and it could not be otherwise: `run` is a plain `def`. The call is subject to the host-call timeout, and a timeout means "we stopped waiting", never "we stopped it" |
| **Audit fields** | `capability`, `status`, `decision`, `reason`, `duration_ms`, `state`, `admitted`, `executed`, `cost`, `confirmed_stopped`, and the response `data` on success |
| **Failure semantics** | Plugin side: `-4` `INVALID_ARGUMENT` if the payload fails the schema; `-8` if the invocation's cost budget cannot cover one more call; `-12` `RATE_LIMITED` if this plugin is calling faster than the window allows; `-9` / `-13` if no slot is available. Host side: `-6` `HOST_ERROR` if `run` raises, if it times out, or if the host is shutting down. All are recorded, and all fail the invocation |
| **Response is minimal** | Deliberately: any absolute path or token a plugin smuggled into `license` or `author` never travels back out. Proven by `test_wasm_host_capability.py::test_response_is_minimal_and_does_not_echo_the_input` |

### Why this one exists

It is the smallest capability that exercises the whole channel: a schema, an
authorisation decision, a pure implementation, a sized response and an audit
record. It is also the shape a future `blender.create_reference_plane` call would
take — which is exactly why the boundary was built and tested *before* anything
that creates a file or moves a mesh.

## Explicitly not offered

None of these exist as a capability, and none can be reached another way:

| Not offered | Why |
| --- | --- |
| Read/write/list/stat any path | A path argument would move the security question from "is this plugin allowed to act" to "what may it name". Not the model this sandbox implements |
| Fetch any URL, open a socket, resolve a name | Same reason. There is also no network import |
| Run any command or process | `PermissionManager` denies `code:execution` unconditionally, and the sandbox has no process import |
| Evaluate Python, import a module, load a library | The guest is WebAssembly. There is no FFI and no `dlopen` |
| Blender scene modification | Deliberately out of scope for this phase. The connector exists; exposing it to plugins is a separate decision with its own review |
| Read the database, the artifact store or the event bus | A plugin gets a JSON answer to a question, not a handle |

## Adding a capability

1. Implement `HostCapability` (name, scope, risk, `parameters_schema`, `run`), and
   declare `cost`, `rate_limit_per_s` and `supports_cancellation`.
2. Register it in `default_registry` — or in a registry you pass in.
3. Fill in all eight fields above in this document.
4. Test: the schema rejects extra fields, an unauthorised call is refused, the
   refusal survives a plugin that ignores it, a raising `run` becomes
   `plugin_host_error`, the cost budget stops it at the declared weight, a burst
   above `rate_limit_per_s` returns `-12` *without* executing, and — if you
   declared it cancellable — a cancelled call really does raise `CancelledError`
   in your coroutine.

No change to `runtime.py`, the ABI, or the manifest format. That is the
extensibility claim this design makes, and `reference.metadata.validate` is the
proof that adding one requires no edits to the sandbox.

## Gaps

- **No capability version field.** `HostCapability` has `name`, `scope`, `risk`,
  `parameters_schema`, `cost`, `rate_limit_per_s`, `supports_cancellation`, `run`.
  Two builds that ship different schemas under the same name are
  indistinguishable to a plugin. Recorded rather than fixed: a version field with
  no consumer would be speculative, and the ABI version already covers the
  channel. It becomes necessary the first time a capability changes shape.
- **No declared cost *curve*.** Cost is one static integer. A capability whose
  cost depends on its input (render two frames vs two thousand) cannot express
  that, and the budget will be wrong in one direction or the other.
- **No check that a declared weight is plausible.** Nothing stops a capability
  from declaring `cost = 1` while launching a browser. Review catches it, not the
  host.
- **Rate limits are per `(plugin, capability)`, not per caller.** Two invocations
  of the same plugin — from two different users, or two different tasks — share
  one window. That is the intended reading of "one plugin cannot take everything",
  but it does mean a plugin cannot be *trusted* with a higher rate for one
  principal and a lower one for another.
- **Rate limiting is a call count, not a cost meter.** `rate_limit_per_s = 100`
  allows 100 calls per second whatever each one costs. Expensive capabilities are
  bounded by `cost`, not by the rate window, and cheap ones by the rate, not by
  `cost`. Neither is a measurement of real spend.
- **Fairness is bounded waiting, not fairness.** The queue is round-robin, 16
  waiters, 0.25 s, per host instance. No weighting, no priority, no latency
  guarantee; a plugin that monopolises slots still delays others by up to
  `max_queue_wait_s` before they are refused rather than served. See
  [host-capability-execution.md](host-capability-execution.md).
