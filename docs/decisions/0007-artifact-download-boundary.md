# ADR 0007 — Downloading untrusted bytes into the artifact store

- Status: accepted
- Date: 2026-10-20
- Phase: 1

## Context

A search result is an arbitrary URL chosen by a third party. Turning one into a
local file is the highest-risk operation in Phase 1: it is an SSRF primitive, a
disk-filling primitive and a content-type confusion primitive at the same time.
The agent controls the query; the *page* controls the URLs; neither may control
where bytes land.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **stdlib `http.client` + explicit guards** (`ImageDownloader`) | No new dependency; full control over redirects, headers, byte cap and file writes | Hand-rolled redirect handling; no HTTP/2 |
| `httpx` client | Redirects, timeouts, connection pooling | Its defaults (follow redirects, trust `Content-Type`, decompress) must all be overridden anyway; one more dependency |
| `requests` | Familiar | Sync, same defaults problem, adds `urllib3` |
| Ask the agent for a local path | No download at all | Breaks the trust boundary: a path is not an authorisation; the agent could point at any file |

## Decision

Implement `ImageDownloader` in `runtime/src/deskweave_runtime/artifacts/downloader.py`
with these hard rules:

1. **URL validation before any connection** (`validate_remote_url`): only
   `http`/`https`; no `user:pass@`; only port 80/443 (the scheme default); every
   resolved IP must be public (rejects loopback, private ranges, link-local, and
   cloud metadata addresses such as `169.254.169.254`).
2. **Manual redirects**, at most 3, re-validating each hop with the same rules.
   No redirect is followed to a different scheme or to a host that fails the guard.
3. **Byte cap 8 MiB, deadline 20 s, streaming read.** The cap is enforced while
   reading, not from `Content-Length` (which is a claim).
4. **`Content-Type` is not trusted.** The bytes are sniffed for PNG/JPEG/GIF/BMP/
   WebP signatures; SVG and AVIF are rejected deliberately (script-carrying XML /
   no Blender support in this path). Non-image bytes → `unsafe_resource`.
5. **The destination is chosen by the runtime**, never by the caller:
   `artifacts/<artifact_id>.<ext>` under the configured artifact root, extension
   taken from the sniffed format. A caller can pass a URL; it cannot pass a path.
6. **Atomic write**: `.<id>.part` → `fsync` → `os.replace`. A crash leaves no
   half-file that looks like an artifact.
7. **Containment on read**: `ArtifactStore.resolve_path` re-checks that the
   resolved path is inside the root, so a symlink or a manually edited row cannot
   exfiltrate an unrelated file.
8. **Provenance, not access**: `original_url` / `source_page` are stored to explain
   where a file came from. Nothing re-fetches them automatically, and possessing a
   URL grants nothing.

## Reasons

1. There is no generic "download this URL" capability anywhere in the system —
   only `import_image`, which applies the guard. The narrow surface is the point.
2. Sniffing bytes rather than trusting the header is the difference between
   "an image" and "an HTML page with an image extension".
3. The redirect loop is the part most often bolted on incorrectly; keeping it
   explicit (and unit-tested per hop) is cheaper than auditing a library's
   defaults.
4. Rejecting AVIF was a deliberate capability trade: Blender's image loader in
   this path does not take AVIF, and a file the target cannot open would surface
   as a confusing later failure.

## Consequences

- Slower than a pooled client for 20 small images (a fresh connection each time).
  Acceptable: the whole collection is dominated by the browser session.
- No HTTP/2, no automatic decompression, no proxy support. A corporate proxy would
  need an explicit feature.
- Partial failures are normal and expected: a task with 4 of 6 candidates saved is
  `waiting_for_confirmation` with a `failures` list, not a failed task.
- Tests use a local `ThreadingHTTPServer` (`tests/unit/test_artifact_downloader.py`)
  to cover the cap, redirects, per-hop re-validation, non-image payloads and
  declared-size lies — without touching the network.

## Revisit if

- Users need SVGs or AVIF (then: rasterise in a sandbox, or add an explicit
  sanitisation step — never just allow the type),
- or downloads must go through a proxy, which is a configuration feature and a new
  SSRF surface to reason about.
