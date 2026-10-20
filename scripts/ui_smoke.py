"""Smoke-test the real desktop UI against a real runtime.

This is the only check that exercises the UI's *real* transport (fetch + data
URLs) instead of mocked modules. It:

1. starts the runtime service on an ephemeral loopback port;
2. builds the web UI with that URL/token baked in;
3. serves the production bundle and drives it with Chromium;
4. runs a real collection, selects a candidate, confirms, waits for Blender;
5. saves screenshots and prints what it observed.

Usage (from the repository root):

    uv run python scripts/ui_smoke.py

Requires `pnpm install` and a Playwright Chromium build.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DESKTOP = REPO_ROOT / "apps" / "desktop"
OUTPUT_DIR = REPO_ROOT / ".tmp-tests"
TOKEN = "ui-smoke-token"
QUERY = "anime character reference sheet"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http(url: str, *, timeout: float = 60.0, auth: bool = False) -> None:
    deadline = time.time() + timeout
    last: str = "no attempt"
    while time.time() < deadline:
        try:
            headers = {"Authorization": f"Bearer {TOKEN}"} if auth else {}
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status < 400:
                    return
                last = f"HTTP {response.status}"
        except OSError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.25)
    raise RuntimeError(f"{url} did not become ready ({last})")


def main() -> int:
    OUTPUT_DIR.mkdir(exist_ok=True)
    data_dir = OUTPUT_DIR / "ui-smoke-data"
    if data_dir.exists():
        shutil.rmtree(data_dir)

    runtime = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "deskweave_runtime.service",
            "--data-dir",
            str(data_dir),
            "--port",
            "0",
            "--token",
            TOKEN,
            "--announce",
            "--log-level",
            "INFO",
        ],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    preview = None
    try:
        assert runtime.stdout is not None
        handshake = json.loads(runtime.stdout.readline())
        runtime_url = f"http://{handshake['host']}:{handshake['port']}"
        print(f"runtime at {runtime_url}")
        _wait_http(f"{runtime_url}/health", auth=True)

        env = dict(os.environ)
        env["VITE_DESKWEAVE_RUNTIME_URL"] = runtime_url
        env["VITE_DESKWEAVE_RUNTIME_TOKEN"] = TOKEN
        # Keep vite from trying to reach npm.
        build = subprocess.run(
            ["node", "node_modules/vite/bin/vite.js", "build", "--logLevel", "warn"],
            cwd=str(DESKTOP),
            env=env,
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            print(build.stdout)
            print(build.stderr)
            return 1

        web_port = _free_port()
        preview = subprocess.Popen(
            [
                "node",
                "node_modules/vite/bin/vite.js",
                "preview",
                "--port",
                str(web_port),
                "--strictPort",
                "--logLevel",
                "warn",
            ],
            cwd=str(DESKTOP),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # vite preview binds to ``localhost``; on this platform 127.0.0.1 may be
        # intercepted by a local proxy, so match the host vite actually listens on.
        web_url = f"http://localhost:{web_port}/"
        _wait_http(web_url)

        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1360, "height": 980})
            page.goto(web_url, wait_until="networkidle")

            page.get_by_role("button", name="References").click()
            page.wait_for_selector("input[type='search']")
            page.fill("input[type='search']", QUERY)
            page.click("[data-test='submit']")

            page.wait_for_selector("[data-test^='card-']", timeout=120_000)
            cards = page.locator("[data-test^='card-']")
            expected_cards = cards.count()
            print(f"candidates rendered: {expected_cards}")
            assert expected_cards > 0, "no candidates rendered"
            page.wait_for_function(
                '() => document.querySelectorAll("img").length > 0', timeout=60_000
            )
            images = page.locator("img").count()
            print(f"previews rendered: {images}")

            page.screenshot(path=str(OUTPUT_DIR / "ui-smoke-candidates.png"), full_page=True)

            cards.first.click()
            assert cards.first.get_attribute("aria-checked") == "true", (
                "clicking a candidate did not select it"
            )
            page.fill("[data-test='plane-name']", "SmokePlane")
            page.click("[data-test='confirm']")

            page.wait_for_function(
                "() => document.body.innerText.includes('Confirmed')", timeout=240_000
            )
            text = page.inner_text("body")
            assert "SmokePlane" in text, text[:2000]
            assert "blend artifact" in text, text[:2000]

            # The user must still be able to see what was sent to Blender.
            kept = cards.count()
            print(f"candidates kept after confirmation: {kept}")
            assert kept == expected_cards, "the confirmed candidates disappeared from the UI"
            assert page.locator("[data-test='confirm']").count() == 0, (
                "a finished task still offers a confirm button"
            )
            page.screenshot(path=str(OUTPUT_DIR / "ui-smoke-confirmed.png"), full_page=True)

            # Reload: the task state must come back from the runtime, not from memory.
            page.reload(wait_until="networkidle")
            page.get_by_role("button", name="References").click()
            page.wait_for_selector("text=succeeded", timeout=30_000)
            restored = page.locator("[data-test^='card-']")
            print(f"candidates restored after reload: {restored.count()}")
            assert restored.count() == expected_cards, "confirmed candidates were not restored"
            assert restored.first.get_attribute("aria-checked") == "true", (
                "the confirmed selection was not restored from the runtime"
            )
            print("state survived a reload: yes")

            browser.close()
        print("UI SMOKE OK")
        return 0
    finally:
        if preview is not None:
            preview.terminate()
            preview.wait(timeout=10)
        runtime.terminate()
        try:
            runtime.wait(timeout=10)
        except subprocess.TimeoutExpired:
            runtime.kill()


if __name__ == "__main__":
    sys.exit(main())
