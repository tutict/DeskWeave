"""Restart recovery through the real service entry point.

``tests/unit/test_recovery.py`` proves the reconciliation *rules*. This test proves
the *wiring*: a data directory holding the rows a killed process leaves behind is
handed to a real ``python -m deskweave_runtime.service`` process, which must
reconcile before it starts answering requests.

The interrupted state is produced by the production code paths (a collection whose
background coroutine dies mid-download), not by hand-written SQL, so the fixture
cannot drift away from what the runtime actually writes.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from deskweave_contracts import CollectionRequest
from deskweave_runtime import Runtime, RuntimeConfig
from support.doubles import FakeBlender, FakeBrowser, FakeDownloader, candidates

TOKEN = "recovery-e2e-token"


def _get(url: str, token: str = TOKEN, method: str = "GET") -> tuple[int, dict]:
    request = urllib.request.Request(url, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


async def _seed_interrupted_collection(data_dir: Path) -> str:
    """Leave a ``running`` task with two downloaded images behind."""
    config = RuntimeConfig(data_dir=data_dir, port=0)
    runtime = await Runtime.create(config)
    await runtime.start()
    runtime.artifacts._downloader = FakeDownloader(data_dir / "artifacts")  # type: ignore[attr-defined]
    browser = FakeBrowser()
    await runtime.register_adapter(browser)
    await runtime.register_adapter(FakeBlender(data_dir / "artifacts"))
    browser.candidates = candidates(3)

    original = runtime.artifacts.import_image
    seen = {"n": 0}

    async def die_on_third(*args: object, **kwargs: object) -> object:
        seen["n"] += 1
        if seen["n"] == 3:
            # A killed process stops here: two images saved, task still running.
            raise asyncio.CancelledError("simulated kill during the download")
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    runtime.artifacts.import_image = die_on_third  # type: ignore[method-assign]
    task = await runtime.references.start_collection(
        CollectionRequest(query="ghost ship reference", limit=3)
    )
    for _ in range(500):
        if not runtime.references._background:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.01)

    interrupted = await runtime.workflow.get_task(task.task_id)
    assert interrupted.status.value == "running", interrupted.status
    await runtime.close()
    return task.task_id


async def test_service_recovers_interrupted_task_on_start(tmp_path: Path) -> None:
    data_dir = tmp_path / "recovery-data"
    task_id = await _seed_interrupted_collection(data_dir)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "deskweave_runtime.service",
            "--announce",
            "--port",
            "0",
            "--data-dir",
            str(data_dir),
            "--token",
            TOKEN,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    try:
        assert process.stdout is not None
        handshake = json.loads(
            await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), timeout=60)
        )
        assert handshake["event"] == "ready"
        base = f"http://127.0.0.1:{handshake['port']}"

        # 1. Recovery ran before the API started: health reports what it did.
        status, health = await asyncio.to_thread(_get, f"{base}/health")
        assert status == 200
        assert health["state"] == "ok"
        component = next(item for item in health["components"] if item["name"] == "recovery")
        assert "1 task(s) reconciled" in component["detail"], component
        assert f"{task_id}=resumed_partial_collection" in component["detail"], component

        # 2. The task is no longer stuck in `running`; the saved images are kept.
        status, view = await asyncio.to_thread(_get, f"{base}/tasks/{task_id}")
        assert status == 200
        task = view["task"]
        assert task["status"] == "waiting_for_confirmation"
        assert task["result"]["recovery"]["action"] == "resumed_partial_collection"
        assert len(task["result"]["candidates"]) == 2
        assert [artifact["artifact_id"] for artifact in view["artifacts"]] == (
            task["result"]["candidates"]
        )
        assert task["result"]["request"]["query"] == "ghost ship reference"

        status, _ = await asyncio.to_thread(_get, f"{base}/shutdown", TOKEN, "POST")
        assert status == 200
        stdout, stderr = await asyncio.wait_for(asyncio.to_thread(process.communicate), timeout=60)
        assert process.returncode == 0
        # 3. The decision is in the log, with the task id, not just in the database.
        combined = f"{stdout}\n{stderr}"
        assert "recovery: task" in combined
        assert task_id in combined
        assert "resumed_partial_collection" in combined
    finally:
        if process.poll() is None:
            process.kill()


async def test_restart_with_nothing_to_recover_is_clean(tmp_path: Path) -> None:
    """A healthy data directory must not be "recovered" into something else."""
    data_dir = tmp_path / "healthy-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "deskweave_runtime.service",
            "--announce",
            "--port",
            "0",
            "--data-dir",
            str(data_dir),
            "--token",
            TOKEN,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    try:
        assert process.stdout is not None
        handshake = json.loads(
            await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), timeout=60)
        )
        base = f"http://127.0.0.1:{handshake['port']}"
        status, health = await asyncio.to_thread(_get, f"{base}/health")
        assert status == 200
        component = next(item for item in health["components"] if item["name"] == "recovery")
        assert component["detail"] == "no interrupted tasks found"
        assert component["state"] == "ok"
        await asyncio.to_thread(_get, f"{base}/shutdown", TOKEN, "POST")
        await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=60)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
