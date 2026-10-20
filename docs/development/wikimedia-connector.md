# Wikimedia connector

`adapters/wikimedia` — a second reference-image source. It talks to the public
MediaWiki API of Wikimedia Commons over HTTPS, with no key, no login and no
scraping.

Why this source: every result arrives with a **licence** and an **author** from
`extmetadata`, so a reference carries its attribution into the artifact store
instead of losing it. It is also strictly a read-only GET, which makes it a good
second data point for the adapter contract — a different transport, the same
capability.

## 1. Configuration

None. There is nothing to set, no token to store, and no browser binary to install.

```
https://commons.wikimedia.org/w/api.php
  ?action=query&format=json&formatversion=2
  &generator=search&gsrsearch=<query> filetype:bitmap&gsrnamespace=6&gsrlimit=<n>
  &prop=imageinfo&iiprop=url|size|mime|extmetadata&iiurlwidth=1600
```

`gsrnamespace=6` restricts the search to the `File:` namespace; `filetype:bitmap`
asks the server to drop PDFs and SVGs before they reach the client. If a query
returns nothing with the filter, the client retries **once** without it and reports
which strategy produced the result (`strategy: "filetype" | "plain"`). The filter is
a narrowing, not a promise: every candidate is validated locally anyway.

The connector sends a descriptive `User-Agent`
(`DeskWeave/0.0.1 (local reference collector; +<repo>)`) because the Wikimedia API
policy asks for one. Do not change it to a browser string: identify the tool.

## 2. Run

```bash
uv sync --all-packages          # adapters/* are workspace members
uv run python -m deskweave_runtime.service --announce --port 8787

# then, with the token from .deskweave/runtime.json
curl -s -X POST http://127.0.0.1:8787/tasks \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"query": "elf armor concept art", "limit": 4, "source": "wikimedia"}'
```

The rest of the loop — polling, previews, human confirmation, Blender — is
unchanged; see [reference-workflow.md](../architecture/reference-workflow.md).

## 3. What comes back

| Field | Source | Note |
| --- | --- | --- |
| `image_url` | `imageinfo.thumburl` (falling back to `url`) | 1600 px wide when the server can produce a thumbnail; always `https` on a `*.wikimedia.org` host |
| `page_url` | `descriptionurl` | the human page, for attribution |
| `title` | `title` | `File:` prefix stripped |
| `width` / `height` | `imageinfo` | integers, as reported by the server |
| `media_type` | `imageinfo.mime` | only `image/jpeg`, `image/png`, `image/gif`, `image/webp` |
| `license` | `extmetadata.LicenseShortName` | e.g. `CC BY-SA 4.0`; HTML stripped, 200 chars max |
| `artist` | `extmetadata.Artist` | HTML stripped — Commons stores markup here |

`license` and `artist` are written into the artifact's `metadata.extra`, so they
survive into the task view and the audit record.

## 4. Failure modes

| Situation | Result |
| --- | --- |
| unknown source name (`source="duckduckgo"`) | `400 validation_error` at the edge; no task created |
| `wikimedia` source but connector not installed | `503 adapter_connection_error`, `details.source`; no task created |
| another connector asked for `source="wikimedia"` | `validation_error` — a connector refuses to answer for a source it does not serve |
| DNS / TLS / connection refused / read timeout | `adapter_connection_error`, `details.reason` |
| HTTP error status | `adapter_connection_error`, `details.status` |
| non-JSON body, or a JSON array | `adapter_execution_error` |
| API returned `{"error": {...}}` | `adapter_execution_error`, `details.api_code` |
| every hit filtered locally (PDF, SVG, foreign host) | empty candidate list + `skipped` count — not an error |
| a result list longer than `limit` | truncated; the server may ignore `gsrlimit` |

A nonsense query returns an empty list rather than an exception: "no images match"
is an answer, not a failure.

## 5. Test

```bash
uv run pytest tests/unit/test_wikimedia_api.py tests/unit/test_wikimedia_adapter.py -q
uv run pytest tests/contract/test_collection_connector_contract.py -q

# talks to the real API
DESKWEAVE_NETWORK_TESTS=1 uv run pytest tests/integration/test_wikimedia_real.py -v
```

The unit tests never touch the network: the transport is a callable
(`Callable[[str, float], bytes]`) injected into `WikimediaClient`. The real suite
downloads one candidate through `ImageDownloader` and re-checks the magic bytes, so
"the source and the store agree" is a tested property, not an assumption.

## 6. Troubleshooting

| Symptom | Cause |
| --- | --- |
| `adapter_connection_error` with `TimeoutError` | proxy or firewall. Measured locally: behind an intercepting HTTPS proxy roughly one handshake in three times out, same URL and headers. The product does not retry; the real test does (bounded, 4 attempts) |
| `strategy: "plain"` on a query that should have hits | the server-side `filetype:bitmap` filter is conservative; the local MIME filter still applies |
| empty list, `skipped: 3` | the top hits were PDF/SVG or off-host; try a more specific query |
| `400 validation_error` for a `source` that exists | that source has no connector in this environment — check `GET /adapters` |
