# ADR 0001 — Desktop framework: Tauri 2

- Status: accepted
- Date: 2026-10-19
- Phase: 0

## Context

DeskWeave needs a desktop shell that owns windows, system integration and the
lifetime of a local Python process. The UI must feel native enough for a tool that
sits next to Blender and a browser all day, and it must be shippable by a small
team without maintaining three platform stacks.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **Tauri 2** | Rust core, ~10–20 MB bundles, uses the OS WebView, first-class sidecar/child-process support, strict capability model for the webview | WebView differences across platforms; Rust in the critical path; smaller ecosystem than Electron |
| Electron | Largest ecosystem, predictable Chromium everywhere | 100 MB+ per app, high baseline memory, Node in the renderer is a large attack surface |
| Native (Qt / Swift / WinUI) | Best performance and integration | Three codebases or a heavyweight C++ toolchain; slow iteration for a Phase 0 skeleton |
| Browser-only PWA | No install step | Cannot supervise a local process, cannot bundle the runtime, weakest local permissions story |

## Decision

Use **Tauri 2** with a **Vue 3 + TypeScript + Vite** renderer.

The Rust side is intentionally thin: window creation, child-process supervision,
and one whitelisted command (`runtime_request`). All business logic stays in
Python. The webview is granted only `core:default`; no shell, filesystem or HTTP
plugin permissions are exposed.

## Reasons

1. The process model matches the architecture: Tauri already supervises sidecars.
2. Bundle size and memory matter for an always-on developer tool.
3. A capability-scoped webview is a better security default than a Node-enabled
   renderer — the UI cannot read files or spawn processes on its own.
4. Vue is the team's existing strength; Vite keeps the dev loop fast.

## Consequences

- The UI is limited to what the OS WebView supports (WebView2 on Windows,
  WKWebView on macOS, WebKitGTK on Linux). Rendering must be tested on each target.
- Rust must be installed to build the desktop binary; a pure-frontend dev loop is
  still possible via `pnpm dev`.
- Packaging, signing and notarisation are unsolved in Phase 0 (no installers are
  produced or verified yet).

## Revisit if

- A required OS integration is unavailable in Tauri 2 (e.g. deep Blender/tray
  integration that would force a plugin fork), or
- the team cannot staff Rust maintenance, or
- WebView inconsistencies cause user-visible defects that cannot be worked around.
