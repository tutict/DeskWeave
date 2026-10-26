"""Host capabilities: the only door out of the sandbox.

A plugin cannot read a file, open a socket or start a process. The only thing it
can do beyond pure computation is call ``env.dw_host_call``, and every such call
passes through :meth:`HostCapabilityRegistry.dispatch`, which enforces, in order:

1. the capability exists in this build's catalogue;
2. the plugin **declared** it in its manifest (declaration is necessary, never
   sufficient);
3. the payload satisfies the capability's JSON Schema;
4. the **existing** :class:`~deskweave_adapter_sdk.PermissionChecker` authorises
   it — the same permission layer that guards browser and Blender calls. There is
   no second, plugin-specific permission system;
5. when a lifecycle supplies one, the **approval in force for this build** covers
   this capability *now* (``dispatch_guard``). Step 4 answers "is this principal
   allowed to do this?"; step 5 answers "did a person approve this build for
   this?" — and a grant cannot stand in for the second.

With no checker wired the answer is *deny*, mirroring
:meth:`deskweave_adapter_sdk.Adapter._authorize`: an unwired host must not be a
permissive one.

Every dispatch returns a :class:`HostCallOutcome` that the runtime appends to its
audit record, including the denial reason, so "who asked for what and what
happened" survives even when the plugin swallows the numeric status.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from deskweave_adapter_sdk import PermissionChecker
from deskweave_contracts import (
    DecisionVia,
    PermissionDecision,
    PermissionRequest,
    PermissionScope,
    RiskLevel,
)

from .abi import HostCallStatus
from .errors import PluginError
from .execution import (
    DEFAULT_CAPABILITY_COST,
    DEFAULT_SCOPE_COST,
    CallState,
    ExecutionResult,
    ExecutionScope,
    HostExecutor,
    Refusal,
)

#: Prefix used to namespace a plugin inside the shared permission layer. Keeping
#: it distinct from adapter ids means a grant to ``blender`` never silently
#: becomes a grant to a plugin that happens to use the same capability name.
PLUGIN_PRINCIPAL_PREFIX = "wasm-plugin:"

#: Default budget for one host call when nobody supplies one. The runtime always
#: supplies its own (patchable) value; this exists so a directly-driven registry
#: is still bounded rather than unbounded.
DEFAULT_HOST_CALL_TIMEOUT_S = 5.0


def principal_for(plugin_id: str) -> str:
    """Return the permission principal used for ``plugin_id``."""
    return f"{PLUGIN_PRINCIPAL_PREFIX}{plugin_id}"


#: Asked once per host call: "does a *current* approval cover this capability
#: for this build?" Its answer is required **in addition to** the permission
#: check, never instead of it — a grant is a permission-layer fact, and the
#: approval is a decision about a specific build.
DispatchGuard = Callable[[str, str], Awaitable[None]]


@runtime_checkable
class HostCapability(Protocol):
    """A single operation the host is willing to perform for a plugin.

    Two optional attributes change how the call is governed. They are read with
    ``getattr`` so an existing capability keeps working unchanged:

    ``cost`` — logical cost units charged against the invocation's cost budget
    (default :data:`DEFAULT_CAPABILITY_COST`). It is a *declared weight*, not a
    measurement: this phase deliberately does not pretend to know real CPU time.

    ``supports_cancellation`` — ``True`` only for an ``async def run`` that
    actually stops when cancelled. It is documentation for the audit, not a
    switch: the executor decides by inspecting ``run``, because a capability
    that claims to be cancellable and then ignores ``CancelledError`` would make
    the claim false.

    ``rate_limit_per_s`` — optional calls-per-second ceiling for this capability
    *per plugin*. ``None`` means "use the executor's default", which is also
    ``None`` (unlimited) unless the host configures one. It is a host-side
    declaration: a plugin cannot raise it, and it is not a substitute for either
    the call budget or the cost budget — those bound one invocation, this bounds
    how fast one plugin may hammer one capability over time.
    """

    name: str
    scope: PermissionScope
    risk: RiskLevel
    parameters_schema: Mapping[str, Any]

    def run(self, payload: Mapping[str, Any]) -> Any:
        """Execute the capability. Must be free of side effects outside the host.

        A plain ``def`` runs in the host's bounded blocking pool and can only be
        timed out. An ``async def`` runs on the event loop and can be cancelled.
        """
        ...


@dataclass(frozen=True)
class HostCallOutcome:
    """What the host decided, and why. Recorded in the audit trail.

    ``admitted`` and ``executed`` are separate on purpose. Authorisation passing
    is not a statement that the operation ran: admission can still refuse it for
    cost or concurrency, and an admitted operation can time out or raise. An
    auditor reading ``decision="allow"`` must not conclude "it worked" — that is
    what ``state`` is for.
    """

    capability: str
    status: int
    decision: str
    reason: str
    duration_ms: float
    #: Terminal state of the call, always one of
    #: :class:`~deskweave_plugin_wasm.execution.CallState`. Required, not
    #: defaulted: an audit record that cannot say how a call ended is worse than
    #: no record, and the empty string this field used to default to read as
    #: "something happened" without saying what.
    state: str
    data: dict[str, Any] | None = None
    admitted: bool = False
    executed: bool = False
    cost: int = 0
    confirmed_stopped: bool = True

    @property
    def ok(self) -> bool:
        return self.status >= 0


@dataclass
class HostCapabilityRegistry:
    """Catalogue plus policy for host capabilities."""

    capabilities: Mapping[str, HostCapability] = field(default_factory=dict)
    permission_checker: PermissionChecker | None = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("deskweave.wasm.host"))
    executor: HostExecutor | None = None
    #: Set by :class:`~deskweave_plugin_wasm.lifecycle.PluginLifecycle`. When
    #: present, a permission-layer ALLOW is not enough: the call must also be
    #: covered by the approval that is in force for this build *right now*.
    #: That is what stops a hand-written grant from authorising a plugin that
    #: no person approved, without changing what ``grant`` means for adapters.
    dispatch_guard: DispatchGuard | None = None

    def register(self, capability: HostCapability) -> None:
        """Add a capability to the catalogue."""
        merged = dict(self.capabilities)
        merged[capability.name] = capability
        self.capabilities = merged

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.capabilities))

    def host_executor(self) -> HostExecutor:
        """The executor this registry dispatches through, created on first use.

        Lazily created so constructing a registry stays cheap, but *always*
        present: a registry that ran capabilities inline on the event loop would
        reintroduce exactly the blocking bug this module exists to fix.
        """
        if self.executor is None:
            self.executor = HostExecutor()
        return self.executor

    async def dispatch(
        self,
        *,
        plugin_id: str,
        declared: frozenset[str],
        capability: str,
        payload: Mapping[str, Any],
        scope: ExecutionScope | None = None,
        timeout_s: float | None = None,
    ) -> HostCallOutcome:
        """Authorise and run one host call, or explain the refusal."""
        started = time.perf_counter()

        def done(
            status: int,
            decision: str,
            reason: str,
            data: dict[str, Any] | None = None,
            *,
            state: str,
            admitted: bool = False,
            executed: bool = False,
            cost: int = 0,
            confirmed_stopped: bool = True,
        ) -> HostCallOutcome:
            return HostCallOutcome(
                capability=capability,
                status=status,
                decision=decision,
                reason=reason,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                data=data,
                state=state,
                admitted=admitted,
                executed=executed,
                cost=cost,
                confirmed_stopped=confirmed_stopped,
            )

        handler = self.capabilities.get(capability)
        if handler is None:
            self.logger.warning("plugin %s asked for unknown capability %s", plugin_id, capability)
            return done(
                HostCallStatus.DENIED_UNKNOWN_CAPABILITY,
                "unknown",
                f"capability {capability!r} is not offered by this host",
                state=CallState.REFUSED.value,
            )

        if capability not in declared:
            # The module imported the host call and used it for something its
            # manifest never mentioned: treat as a contract violation, not a
            # policy decision.
            self.logger.warning("plugin %s called undeclared capability %s", plugin_id, capability)
            return done(
                HostCallStatus.DENIED_NOT_DECLARED,
                "undeclared",
                f"capability {capability!r} is not declared by the plugin manifest",
                state=CallState.REFUSED.value,
            )

        try:
            Draft202012Validator(dict(handler.parameters_schema)).validate(dict(payload))
        except JsonSchemaValidationError as exc:
            return done(
                HostCallStatus.INVALID_ARGUMENT,
                "invalid",
                f"arguments do not match the capability schema: {exc.message}",
                state=CallState.REFUSED.value,
            )

        request = PermissionRequest(
            adapter_id=principal_for(plugin_id),
            capability=capability,
            scope=handler.scope,
            risk=handler.risk,
            requires_confirmation=False,
            resource=plugin_id,
            reason="sandboxed plugin host call",
        )

        if self.permission_checker is None:
            return done(
                HostCallStatus.DENIED_NO_GRANT,
                "deny",
                "no permission checker is wired; plugin host calls are denied by default",
                state=CallState.REFUSED.value,
            )

        verdict = await self.permission_checker.check(request)
        if verdict.decision is not PermissionDecision.ALLOW:
            self.logger.info(
                "plugin %s denied %s: %s", plugin_id, capability, verdict.reason or verdict.decision
            )
            return done(
                HostCallStatus.DENIED_NO_GRANT,
                verdict.decision.value,
                verdict.reason or f"capability {capability!r} was not granted",
                state=CallState.REFUSED.value,
            )

        # An ALLOW is not enough for a plugin: it has to be an ALLOW that a person
        # produced. The policy auto-allows read-only operations, which is right for
        # an adapter and wrong here — a manifest that declares a read-only
        # capability would otherwise authorise itself. Measured rather than
        # assumed: `test_a_read_only_capability_cannot_authorise_itself`.
        if verdict.via not in (DecisionVia.GRANT, DecisionVia.ONE_SHOT):
            self.logger.warning(
                "plugin %s allowed %s by policy alone (%s), refusing",
                plugin_id,
                capability,
                verdict.via.value if verdict.via else "no provenance",
            )
            return done(
                HostCallStatus.DENIED_NO_GRANT,
                "policy_default",
                (
                    f"capability {capability!r} was allowed by policy "
                    f"({verdict.via.value if verdict.via else 'unnamed'} check), not by an "
                    "explicit grant or a one-shot confirmation; plugin calls require a decision"
                ),
                state=CallState.REFUSED.value,
            )

        # Authorised by the permission layer. For a plugin that is necessary but
        # still not sufficient: a grant can be written by anything that can reach
        # the permission store, so the approval itself is asked too. Skipped when
        # the host was built without a lifecycle, which is the explicit
        # "authorisation is not being modelled here" case.
        #
        # Every exit from here owes the one-shot a disposition. A confirmation
        # parked by `check()` is neither spent nor returned until something says
        # which; a path that returns without settling leaves it stranded in
        # `_pending` forever, and the user's "yes, once" is silently gone.
        one_shot_settled = False

        def settle(started: bool) -> None:
            """Exactly one terminal disposition for a one-shot, whatever happened.

            ``started=False`` returns it; ``started=True`` spends it. Idempotent,
            so a later path cannot return what an earlier one spent.
            """
            nonlocal one_shot_settled
            if verdict.via is not DecisionVia.ONE_SHOT or one_shot_settled:
                return
            one_shot_settled = True
            conclude = getattr(self.permission_checker, "conclude", None)
            if conclude is not None:
                conclude(request, started=started)

        if self.dispatch_guard is not None:
            try:
                await self.dispatch_guard(plugin_id, capability)
            except PluginError as exc:
                self.logger.info(
                    "plugin %s refused %s by the approval boundary: %s",
                    plugin_id,
                    capability,
                    exc.message,
                )
                settle(False)
                return done(
                    HostCallStatus.DENIED_NO_GRANT,
                    "not_approved",
                    exc.message,
                    state=CallState.REFUSED.value,
                )
            except Exception as exc:
                # A defect in the approval check is still a refusal, and it must
                # look like one: before this branch existed the exception escaped
                # `dispatch`, was caught by the bridge's generic handler, and was
                # audited as `admitted=True executed=True state=failed` for a
                # capability that never ran. Same category as the refusal above,
                # distinguished by the exception *type* only — the message can
                # carry paths or connection strings and is logged, not returned.
                self.logger.exception(
                    "the approval check for plugin %s raised while authorising %s",
                    plugin_id,
                    capability,
                )
                settle(False)
                return done(
                    HostCallStatus.DENIED_NO_GRANT,
                    "not_approved",
                    f"the approval check failed ({type(exc).__name__}); "
                    "the call was refused before execution",
                    state=CallState.REFUSED.value,
                )

        # Authorised. From here on the call is *admitted* or *refused* by
        # execution governance, never by policy — and the two are recorded
        # separately so "the plugin was allowed" is never read as "the work
        # happened".
        budget = (
            scope if scope is not None else ExecutionScope(plugin_id, max_cost=DEFAULT_SCOPE_COST)
        )
        try:
            outcome = await self.host_executor().run(
                plugin_id=plugin_id,
                capability=capability,
                handler=handler,
                payload=payload,
                scope=budget,
                timeout_s=DEFAULT_HOST_CALL_TIMEOUT_S if timeout_s is None else timeout_s,
            )
        except BaseException:
            # The executor is not supposed to raise, so if it does we do not know
            # whether the capability started. Spending the allowance is the only
            # safe reading of "unknown": the alternative is handing back a
            # confirmation for work that may be running right now.
            settle(True)
            raise
        # Refused for cost, concurrency, rate, queue timeout or revocation never
        # happened, so the confirmation is returned. A timeout or a cancellation
        # is `executed`, so it is spent — the work may well have run.
        settle(outcome.executed)
        return self._outcome_from(capability, outcome, done)

    def _outcome_from(
        self,
        capability: str,
        outcome: ExecutionResult,
        done,
    ) -> HostCallOutcome:
        """Translate an execution result into the audit record and wire status."""
        if outcome.state is CallState.REFUSED:
            if outcome.refusal is Refusal.COST:
                return done(
                    HostCallStatus.COST_EXHAUSTED,
                    "cost",
                    outcome.reason,
                    state=outcome.state.value,
                    cost=outcome.cost,
                )
            if outcome.refusal is Refusal.CONCURRENCY:
                return done(
                    HostCallStatus.CONCURRENCY_LIMIT,
                    "concurrency",
                    outcome.reason,
                    state=outcome.state.value,
                    cost=outcome.cost,
                )
            if outcome.refusal is Refusal.QUEUE_FULL:
                return done(
                    HostCallStatus.CONCURRENCY_LIMIT,
                    "queue_full",
                    outcome.reason,
                    state=outcome.state.value,
                    cost=outcome.cost,
                )
            if outcome.refusal is Refusal.QUEUE_TIMEOUT:
                return done(
                    HostCallStatus.QUEUE_TIMEOUT,
                    "queue_timeout",
                    outcome.reason,
                    state=outcome.state.value,
                    cost=outcome.cost,
                )
            if outcome.refusal is Refusal.RATE:
                return done(
                    HostCallStatus.RATE_LIMITED,
                    "rate",
                    outcome.reason,
                    state=outcome.state.value,
                    cost=outcome.cost,
                )
            if outcome.refusal is Refusal.CANCELLED:
                return done(
                    HostCallStatus.CANCELLED, "cancelled", outcome.reason, state=outcome.state.value
                )
            if outcome.refusal is Refusal.REVOKED:
                # Distinct from shutdown and from a capacity refusal: the plugin
                # has to be approved again, not retried.
                return done(
                    HostCallStatus.DENIED_NO_GRANT,
                    "revoked",
                    outcome.reason,
                    state=CallState.REFUSED.value,
                )
            return done(
                HostCallStatus.HOST_ERROR, "shutdown", outcome.reason, state=outcome.state.value
            )
        if outcome.state is CallState.COMPLETED:
            return done(
                0,
                "allow",
                outcome.reason or "granted",
                outcome.data,
                state=outcome.state.value,
                admitted=True,
                executed=True,
                cost=outcome.cost,
            )
        if outcome.state is CallState.FAILED:
            exc = outcome.error
            self.logger.exception(
                "host capability %s failed",
                capability,
                exc_info=exc if exc is not None else None,
            )
            return done(
                HostCallStatus.HOST_ERROR,
                "error",
                outcome.reason,
                state=outcome.state.value,
                admitted=True,
                executed=True,
                cost=outcome.cost,
            )
        if outcome.state is CallState.TIMED_OUT:
            return done(
                HostCallStatus.HOST_ERROR,
                "timeout",
                outcome.reason,
                state=outcome.state.value,
                admitted=True,
                executed=True,
                cost=outcome.cost,
                confirmed_stopped=outcome.confirmed_stopped,
            )
        if outcome.state is CallState.CANCELLED:
            return done(
                HostCallStatus.CANCELLED,
                "cancelled",
                outcome.reason,
                state=outcome.state.value,
                admitted=True,
                executed=True,
                cost=outcome.cost,
                confirmed_stopped=True,
            )
        return done(
            HostCallStatus.CANCELLATION_UNCONFIRMED,
            "cancellation_requested",
            outcome.reason,
            state=outcome.state.value,
            admitted=True,
            executed=True,
            cost=outcome.cost,
            confirmed_stopped=False,
        )


class ReferenceMetadataValidate:
    """``reference.metadata.validate`` — validate one reference-image record.

    Pure computation on data the host already holds: no filesystem, no network,
    no Blender. It exists to prove the capability channel end to end, and it is
    the shape a future ``blender.create_reference_plane`` call would take.

    The response is intentionally minimal: problems only, never an echo of the
    input. Any absolute path or token a malicious plugin smuggled into a field
    therefore never travels back out.
    """

    name = "reference.metadata.validate"
    scope = PermissionScope.PLUGIN_HOST_CALL
    # LOW, not READ_ONLY: read-only capabilities are auto-allowed by the shared
    # policy, and a plugin call must never be auto-allowed. LOW makes the call
    # require an explicit grant or a one-shot confirmation, using the existing
    # PermissionManager rules rather than a bespoke rule set.
    risk = RiskLevel.LOW
    #: Cheapest non-trivial weight: pure computation on a payload the host already
    #: holds. A capability that launches a browser or starts a render must declare
    #: a cost high enough that the budget, not the call count, is what stops a
    #: loop.
    cost = 1
    #: Synchronous by design — it is microseconds of arithmetic, and an
    #: ``async def`` would add a task for no reason.
    supports_cancellation = False
    parameters_schema: Mapping[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["image"],
        "properties": {
            "image": {
                "type": "object",
                "additionalProperties": False,
                "required": ["artifact_id", "title", "width", "height"],
                "properties": {
                    "artifact_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "width": {"type": "integer", "minimum": 1, "maximum": 100_000},
                    "height": {"type": "integer", "minimum": 1, "maximum": 100_000},
                    "license": {"type": "string", "maxLength": 100},
                    "author": {"type": "string", "maxLength": 200},
                },
            }
        },
    }

    _REQUIRED_TEXT = ("title",)

    def run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        image = payload["image"]
        problems: list[dict[str, str]] = []

        for field_name in self._REQUIRED_TEXT:
            value = image.get(field_name)
            if not isinstance(value, str) or not value.strip():
                problems.append({"field": field_name, "reason": "must be non-empty text"})

        width = image.get("width")
        height = image.get("height")
        if isinstance(width, int) and isinstance(height, int):
            if width * height > 40_000_000:
                problems.append({"field": "dimensions", "reason": "pixel count exceeds limit"})
            elif abs(width - height) / max(width, height) > 0.5:
                problems.append({"field": "dimensions", "reason": "aspect ratio is extreme"})
        else:
            problems.append({"field": "dimensions", "reason": "width and height are required"})

        licence = image.get("license")
        if licence is not None and not str(licence).strip():
            problems.append({"field": "license", "reason": "present but empty"})

        return {"valid": not problems, "problems": problems}


def default_registry(
    permission_checker: PermissionChecker | None = None,
    executor: HostExecutor | None = None,
) -> HostCapabilityRegistry:
    """Return the host catalogue this build ships with."""
    registry = HostCapabilityRegistry(
        permission_checker=permission_checker, capabilities={}, executor=executor
    )
    registry.register(ReferenceMetadataValidate())
    return registry


__all__ = [
    "DEFAULT_CAPABILITY_COST",
    "DEFAULT_HOST_CALL_TIMEOUT_S",
    "PLUGIN_PRINCIPAL_PREFIX",
    "DispatchGuard",
    "HostCallOutcome",
    "HostCapability",
    "HostCapabilityRegistry",
    "ReferenceMetadataValidate",
    "default_registry",
    "principal_for",
]
