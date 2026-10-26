"""S2-1: the boundary a restricted Agent credential must not cross.

Why this file contains authentication controls instead of the three
expected-red authorization tests
--------------------------------------------------------------------------
S2-1 asks for three tests proving that a *restricted Agent credential* is
refused (403) by ``POST /tasks/{id}/confirm``, ``POST /artifacts/{id}/open`` and
``POST /shutdown``. Those tests cannot be written honestly against the current
code, because the server has no second identity to hand them:

1. ``RuntimeService.__init__`` takes exactly one ``token`` and stores it
   (``service/app.py:199-211``); ``is_authorized`` compares the presented
   credential with that single value using ``hmac.compare_digest``
   (``service/app.py:259-265``). There is no role, scope, principal or
   per-route permission model in ``runtime/``, ``packages/contracts`` or
   ``packages/mcp-gateway``.
2. Therefore every credential that is not the UI token is rejected by
   ``_dispatch`` (``service/app.py:133-136``) with ``401 unauthorized`` *before*
   any route handler runs. An ``assert status == 403`` test would fail while the
   request never entered the target route — that is an invalid permission red
   light under S2-1 constraint 9 ("must confirm the request reached the target
   route").
3. Manufacturing a second identity (a magic header, patching ``_token``, a
   test-only scope flag) would assert a mechanism that does not exist, which
   S2-1 explicitly forbids.

Note also that ``403`` is already reachable on ``confirm`` for a *different*
reason: a foreign artifact id yields ``permission_denied``
(``test_reference_api.py:228-243``). A future Agent test must therefore assert
``403`` **plus** "no execution happened", or it will not distinguish "artifact
not yours" from "your credential may not confirm".

So this file pins down what is true today and is still required by the step: the
authentication controls that make the 401 vs 403 distinction executable, paired
with positive controls proving each route really works with the UI credential
(so a 401 cannot be mistaken for a dead route). When the authorization model
lands — a second, narrowly scoped credential — the three 403 tests belong right
next to these controls and can reuse ``_call`` / the ``api`` fixture unchanged.

Every test asserts the *side effect* did not happen, not just the status code.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import pytest

from deskweave_contracts import ArtifactKind
from deskweave_runtime import Runtime, RuntimeConfig
from deskweave_runtime.artifacts import ArtifactOpener, LaunchPlan
from deskweave_runtime.service import RuntimeService
from support.doubles import FakeBlender, candidates, install_fakes

UI_TOKEN = "ui-credential-under-test"
#: A well-formed credential the server has never been told about. Today this is
#: indistinguishable from "wrong token"; it is *not* an Agent identity.
UNRECOGNISED_TOKEN = "agent-credential-that-does-not-exist-yet"


@dataclass
class Api:
    """One isolated runtime + control API on a temporary data directory."""

    runtime: Runtime
    service: RuntimeService
    blender: FakeBlender
    launches: list[LaunchPlan] = field(default_factory=list)


def _call(
    url: str,
    *,
    token: str | None,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


@pytest.fixture
async def api(config: RuntimeConfig):
    runtime = await Runtime.create(config)
    await runtime.start()
    _, blender, _ = await install_fakes(runtime)
    service = RuntimeService(runtime, token=UI_TOKEN, host=config.host, port=0)
    await service.start()
    handle = Api(runtime=runtime, service=service, blender=blender)
    # Replace the OS opener with one that only records the plan: nothing is
    # launched, and "was it called?" becomes observable.
    runtime._opener = ArtifactOpener(  # type: ignore[attr-defined]
        runtime.artifacts, platform="win32", launch=handle.launches.append
    )
    try:
        yield handle
    finally:
        await service.stop()
        await runtime.close()


async def _seed_waiting_task(api: Api) -> tuple[str, list[str]]:
    """Drive a real collection to ``waiting_for_confirmation`` over HTTP."""
    api.runtime.registry.get("browser").candidates = candidates(2)
    status, _, body = await asyncio.to_thread(
        _call,
        f"{api.service.url}/tasks",
        token=UI_TOKEN,
        method="POST",
        body={"query": "anime character reference", "limit": 2},
    )
    assert status == 202, body
    task_id = json.loads(body)["task"]["task_id"]
    await api.runtime.references.wait_until_settled(task_id)
    view = await api.runtime.references.get_view(task_id)
    assert view.task.status.value == "waiting_for_confirmation", view.task.status
    return task_id, [artifact.artifact_id for artifact in view.artifacts]


async def _register_blend_artifact(api: Api) -> str:
    """A model artifact the opener could legitimately hand to the OS."""
    path = api.runtime.config.data_dir / "artifacts" / "plane.blend"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"BLENDER-fake")
    artifact = await api.runtime.artifacts.register(
        kind=ArtifactKind.MODEL,
        uri=path.resolve().as_uri(),
        media_type="application/x-blender",
        size_bytes=12,
        task_id="task_owner",
    )
    return artifact.artifact_id


# -- POST /tasks/{id}/confirm ------------------------------------------------
async def test_confirm_route_works_with_the_ui_credential(api: Api) -> None:
    """Control: the route is reachable, so a 401 below is about the credential."""
    task_id, selected = await _seed_waiting_task(api)

    status, _, body = await asyncio.to_thread(
        _call,
        f"{api.service.url}/tasks/{task_id}/confirm",
        token=UI_TOKEN,
        method="POST",
        body={"artifact_ids": selected},
    )

    assert status == 200, body
    assert json.loads(body)["task"]["status"] == "succeeded"
    assert api.blender.calls == [selected]


@pytest.mark.parametrize("token", [None, UNRECOGNISED_TOKEN], ids=["missing", "unrecognised"])
async def test_confirm_refuses_a_credential_the_server_does_not_recognise(
    api: Api, token: str | None
) -> None:
    """A credential without an identity gets 401 and changes nothing.

    This is the control that makes the future 403 test meaningful: the request is
    stopped at authentication, never at a permission check.
    """
    task_id, selected = await _seed_waiting_task(api)

    status, _, body = await asyncio.to_thread(
        _call,
        f"{api.service.url}/tasks/{task_id}/confirm",
        token=token,
        method="POST",
        body={"artifact_ids": selected},
    )

    assert status == 401, body
    assert json.loads(body)["error"]["code"] == "unauthorized"

    # Side effects must be absent, not just the status code wrong.
    view = await api.runtime.references.get_view(task_id)
    assert view.task.status.value == "waiting_for_confirmation"
    assert view.task.result.get("confirmation") is None
    assert view.task.result.get("execution") is None
    assert api.blender.calls == [], "the target adapter must not have been invoked"


# -- POST /artifacts/{id}/open -----------------------------------------------
async def test_open_route_works_with_the_ui_credential(api: Api) -> None:
    """Control: the opener is wired, so "no launch" below means "never reached"."""
    artifact_id = await _register_blend_artifact(api)

    status, _, body = await asyncio.to_thread(
        _call,
        f"{api.service.url}/artifacts/{artifact_id}/open",
        token=UI_TOKEN,
        method="POST",
        body={"action": "reveal"},
    )

    assert status == 200, body
    assert json.loads(body)["artifact"]["opened"] is True
    assert len(api.launches) == 1


@pytest.mark.parametrize("token", [None, UNRECOGNISED_TOKEN], ids=["missing", "unrecognised"])
async def test_open_refuses_a_credential_the_server_does_not_recognise(
    api: Api, token: str | None
) -> None:
    artifact_id = await _register_blend_artifact(api)

    status, _, body = await asyncio.to_thread(
        _call,
        f"{api.service.url}/artifacts/{artifact_id}/open",
        token=token,
        method="POST",
        body={"action": "reveal"},
    )

    assert status == 401, body
    assert json.loads(body)["error"]["code"] == "unauthorized"
    assert api.launches == [], "the OS opener must not have been asked to launch anything"


# -- POST /shutdown ----------------------------------------------------------
async def test_shutdown_route_works_with_the_ui_credential(api: Api) -> None:
    """Control: shutdown is observable through the public wait handle."""
    status, _, body = await asyncio.to_thread(
        _call, f"{api.service.url}/shutdown", token=UI_TOKEN, method="POST", body={}
    )

    assert status == 200, body
    assert json.loads(body)["status"] == "shutting_down"
    # Completes instead of timing out: shutdown was actually requested.
    await asyncio.wait_for(api.service.wait_shutdown(), timeout=5)


@pytest.mark.parametrize("token", [None, UNRECOGNISED_TOKEN], ids=["missing", "unrecognised"])
async def test_shutdown_refuses_a_credential_the_server_does_not_recognise(
    api: Api, token: str | None
) -> None:
    status, _, body = await asyncio.to_thread(
        _call, f"{api.service.url}/shutdown", token=token, method="POST", body={}
    )

    assert status == 401, body
    assert json.loads(body)["error"]["code"] == "unauthorized"

    # The runtime is still serving, and shutdown was never requested.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(api.service.wait_shutdown(), timeout=0.3)
    alive, _, health = await asyncio.to_thread(_call, f"{api.service.url}/health", token=UI_TOKEN)
    assert alive == 200, health
    assert api.runtime.config.data_dir.is_dir()
