# ADR 0006 — Browser reference source: one HTML source, parsed conservatively

- Status: accepted
- Date: 2026-10-20
- Phase: 1

## Context

`browser.collect_reference_images` must return real image candidates, not
hardcoded URLs. That requires (a) a search surface that answers without an API
key, (b) a parser that survives being wrong, and (c) a failure story for the common
case where a search page is not what we expected.

Reality on the machine used for Phase 1 (measured, not assumed):

| Host | Result |
| --- | --- |
| `cn.bing.com/images/search` | reachable, returns result cards |
| `duckduckgo.com` | connection timeout |
| `google.com` | connection timeout |

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **Bing image search HTML, one parser** | No key, no login, works on this network; card metadata (`murl`, `purl`, `t`, `mw`, `mh`) is embedded as JSON | Markup can change; scraping sits in a grey zone; results are regional |
| Official search APIs (Bing/Brave/Google CSE) | Stable contract, rate limits, ToS-clean | Requires the user to obtain and store an API key; adds a network credential to a local-first product |
| Headless driver with a stealth plugin | Higher yield on protected pages | Explicitly out of bounds: no captcha circumvention, no anti-bot evasion |
| Dozens of sources behind a plugin | Resilience | Each parser is a maintenance liability with no test that can pin it |

## Decision

Ship **one** source, `bing`, behind an `ImageSource` protocol
(`adapters/browser/src/deskweave_adapter_browser/sources.py`):

- `build_url(query)` uses `urlencode` — the query **must** appear in the URL
  (`?q=…&form=HDRSC2&first=1`). An early build navigated the bare search URL and
  returned unrelated trending images; only a live probe caught it.
- Results are read from `a.iusc[m]` cards and their JSON payload; a card without
  a parsable payload is skipped, not guessed.
- The source is selected by a `source` argument (enum `ReferenceSource`), so
  adding a second provider means adding a class, not branching inside the adapter.
- Blocked pages are **detected and reported** (`browser_access_denied`) using
  title/body markers (captcha, 验证码, 人机验证, "just a moment", "access denied").
  There is no attempt to solve or bypass them.
- HTTP status ≥ 400, missing result cards (layout change) and empty results are
  three distinct errors with the URL in the message.

## Reasons

1. The workflow's value is the *pipeline* (collect → store → confirm → act), not
   the search engine. One well-instrumented source proves the pipeline; five
   flaky parsers would hide its bugs.
2. No API key keeps the local-first promise and keeps secrets out of the repo and
   the runtime data directory.
3. An honest `browser_access_denied` is better than a silent empty list: the user
   learns why, and the agent stops instead of inventing an alternative path.
4. The source is behind a protocol so a keyed provider can be added without
   touching the adapter, the contracts or the UI.

## Consequences

- Tests cannot pin third-party markup. The parser has unit tests against captured
  card JSON, and one opt-in integration test (`DESKWEAVE_NETWORK_TESTS=1`) that
  asserts *shape* (scheme, non-empty URL, sane dimensions), not specific images.
- Results are regional and may include hosts that block this country or return
  AVIF. Both failure modes surface as per-candidate `failures`, not as a failed
  task.
- `Accept` advertises only the formats we can sniff and Blender can load
  (`webp, png, jpeg, gif, bmp`). Advertising `image/avif` made some CDNs return
  AVIF, which the sniffer rejects — a whole class of false "download failed".

## Revisit if

- The user supplies an API key (then add a keyed `ImageSource` and prefer it), or
- a second source is genuinely needed, which would require a per-source
  integration test and a documented fallback order.
