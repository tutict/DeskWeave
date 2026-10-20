# Browser connector

The `browser` connector turns a search query into a list of candidate image URLs.
It does **not** download anything: the runtime's artifact store owns fetching, so
all download policy (SSRF guard, size cap, type sniffing) lives in one place.

| Capability | Risk | Scope | Confirmation |
| --- | --- | --- | --- |
| `collect_reference_images` | `moderate` | `browser:control` | not required (reads the web only) |

Nothing else is exposed: there is no `navigate`, `click` or `screenshot`
capability, so the connector cannot be used as a general browsing primitive.

## 1. Install

Playwright is a normal dependency of `deskweave-adapter-browser`, so the Python
side comes with the workspace sync:

```bash
uv sync --all-packages                  # installs playwright (pinned in pyproject)
uv run playwright install chromium      # downloads the browser binary (~150 MB)
```

On Linux CI runners add the system libraries in the same step:

```bash
uv run playwright install --with-deps chromium
```

Verify what the adapter will use:

```bash
uv run python -c "from playwright.sync_api import sync_playwright; \
  p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()"
```

The adapter checks the *reported* executable path on `connect()`, not just the
presence of the Python package — a missing browser is reported at startup as
`adapter_connection_error` instead of failing mid-collection.

## 2. Configure

There is no configuration file and no credential. The only knob exposed to
callers is the `source` argument (currently `bing`) and `limit` (1–20, default 6).

Requirements and constraints, as measured on the development machine:

- `https://cn.bing.com/images/search?q=…` answers without login;
  `duckduckgo.com` and `google.com` time out from this network.
- Results are regional. A query in one language may return images from
  region-specific CDNs.
- The connector never solves captchas or logs in. A blocked page is reported as
  `browser_access_denied` with the source and page title, and the task fails.

## 3. Run

Through the runtime (the only supported path — the adapter is never called
directly by the UI):

```bash
uv run python -m deskweave_runtime.service --announce --port 8787
# then, with the token from .deskweave/runtime.json:
curl -s -X POST http://127.0.0.1:8787/tasks \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"query": "anime character reference sheet", "limit": 6}'
curl -s -H "Authorization: Bearer <token>" http://127.0.0.1:8787/tasks/<task_id>
```

Through MCP: `collect_reference_images` returns a task id immediately, then
`get_reference_task` reports progress (`running` → `waiting_for_confirmation`).

The browser is launched **lazily** on the first collection and closed by
`disconnect()` (runtime shutdown) or after the page is closed in a `finally`
block. An idle runtime holds no Chromium process.

## 4. Test

```bash
uv run pytest tests/unit/test_browser_sources.py -v          # parser, no network
DESKWEAVE_NETWORK_TESTS=1 uv run pytest tests/integration/test_browser_real.py -v
```

The opt-in integration test launches a real Chromium, hits the real search page
and asserts the *shape* of the result (https URL, non-empty, sane dimensions). It
is skipped by default so CI never depends on a third-party page.

## 5. Troubleshooting

| Symptom | Meaning / fix |
| --- | --- |
| `playwright is not installed` | `uv sync --all-packages` |
| `no Chromium build found for Playwright` | `uv run playwright install chromium` |
| `could not open the image search page: TimeoutError…` | Network blocked or DNS failure. Try the URL in a normal browser from the same machine. |
| `the search provider is asking for human verification; open a normal browser, resolve it, then retry` | Captcha / consent wall. A hard stop by design (`browser_access_denied`); the connector does not try to solve it. |
| `image search returned HTTP 4xx` | The provider rejected the request (throttling or region block). |
| `image search page did not return result cards; the layout may have changed or the request was throttled` | Bing markup changed, or the page was throttled. Inspect `a.iusc[m]` in `sources.py`; add a unit test with a captured card first. |
| `image search returned no usable candidates` | The query has no image results, or every card was unparsable. |
| Task fails with `no_candidates` but candidates were listed | Every download was rejected — read `details.failures`; typical causes are AVIF/HTML payloads or geo-blocked hosts. |
| Task stays `waiting_for_confirmation` with a `failures` list | Some images could not be saved. This is expected behaviour, not an error. |
