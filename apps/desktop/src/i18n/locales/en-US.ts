/**
 * English (en-US) message catalog.
 *
 * This file is the *schema*: `zh-CN.ts` is annotated with `MessageSchema`, so a
 * key that is missing, renamed or misspelled in the Chinese catalog fails
 * `vue-tsc --noEmit` instead of silently falling back at runtime.
 *
 * Conventions
 * -----------
 * - Pluralised strings use vue-i18n's `one | other` form and are called as
 *   `t(key, { count }, count)`.
 * - Machine vocabulary coming from the runtime (task states, risks, error codes)
 *   is kept verbatim in `en-US`: those words appear in logs and the database, so
 *   translating them in the English UI would make cross-referencing harder.
 * - Interpolated values (`{timestamp}`, `{id}`, `{detail}`) are never translated.
 */
const enUS = {
  common: {
    refresh: "Refresh",
    retry: "Retry",
    clear: "Clear",
    loading: "Loading…",
    somethingWentWrong: "Something went wrong",
    unknownError: "unknown error",
    units: {
      bytes: "{value} B",
      kilobytes: "{value} KB",
      megabytes: "{value} MB",
    },
  },

  app: {
    tagline: "Local-first agent desktop infrastructure",
    runtime: "runtime",
    via: "via {source}",
    sections: "Sections",
    contactingRuntime: "Contacting the local runtime…",
    runtimeUnavailable: "Runtime unavailable",
    tabs: {
      overview: "Overview",
      tasks: "Tasks",
      references: "References",
      logs: "Logs & preview",
      settings: "Settings",
    },
  },

  overview: {
    healthTitle: "Runtime health",
    healthMeta: "v{version} · schema {schema}",
    database: "Database",
    uptime: "Uptime",
    uptimeValue: "{seconds}s",
    adaptersTitle: "Adapters",
    adaptersRegistered: "{count} registered",
  },

  adapters: {
    emptyTitle: "No adapters registered",
    emptyDescription:
      "Adapters appear here once the runtime registers them. Nothing is connected yet.",
  },

  tasks: {
    emptyTitle: "No workflow tasks yet",
    emptyDescription:
      "Tasks created by a workflow run will be listed here. Phase 0 does not run workflows yet.",
    columnTask: "Task",
    columnStatus: "Status",
    columnRun: "Run",
    columnUpdated: "Updated",
  },

  logs: {
    title: "Execution logs and preview (placeholder)",
    description:
      "Execution records and the Blender preview will be shown here. It is not implemented in Phase 0.",
  },

  settings: {
    runtimeTitle: "Runtime",
    runtimeDescription:
      "These values are reported by the local runtime. They are read-only in Phase 0.",
    version: "Version",
    schemaVersion: "Schema version",
    database: "Database",
    languageTitle: "Language",
    languageDescription:
      "Interface language. A change takes effect immediately and is remembered the next time the app starts.",
    languageLabel: "Interface language",
  },

  references: {
    title: "Reference collection",
    intro:
      "The browser connector searches for reference images and stores them locally. Nothing is sent to Blender until you select the images and confirm.",
    searchQuery: "Search query",
    searchPlaceholder: "e.g. anime character turnaround sheet",
    candidatesLabel: "Candidates",
    collect: "Collect references",
    working: "Working…",
    loadingTasks: "Loading tasks…",
    loadFailed: "Could not load tasks",
    emptyTitle: "No reference tasks yet",
    emptyDescription:
      "Start a collection above. Candidates appear here for review before anything is sent to Blender.",
    candidateCount: "{count} candidate | {count} candidates",
    updatedAt: "updated {timestamp}",
    collecting: "Collecting and downloading candidates…",
    recoveryNote: "Recovered after an interruption ({action}) — {detail}",
    failuresHead: "{count} candidate could not be saved | {count} candidates could not be saved",
    selectHint:
      "Select the images this task should use. Confirming runs Blender once for this selection.",
    planeName: "Plane name (optional)",
    planeNamePlaceholder: "ReferencePlane",
    selectedCount: "{count} selected",
    cancelTask: "Cancel task",
    confirm: "Confirm and continue",
    confirming: "Confirming…",
    confirmed:
      "Confirmed {timestamp} · {plane} with {count} image. | Confirmed {timestamp} · {plane} with {count} images.",
    planeFallback: "reference plane",
    blendScene: "Blender scene",
    blendArtifact: "blend artifact {id}",
    createdAt: "created {timestamp}",
    sizeUnknown: "size unknown",
    objectCount: "{count} object: | {count} objects:",
    blendNote:
      "A .blend file cannot be previewed in the browser; open it in Blender or reveal it on disk.",
    showInFolder: "Show in folder",
    openInBlender: "Open in Blender",
    missingArtifact:
      "The scene was recorded as {id} but that artifact is no longer in the store, so it cannot be opened.",
    blendWarning: "The scene was produced but could not be registered: {detail}",
    untitledCandidate: "Untitled candidate",
    select: "Select {title}",
    deselect: "Deselect {title}",
    loadingPreview: "Loading preview…",
    openSourcePage: "Open source page",
  },

  /**
   * Runtime vocabulary (task states, health states, adapter states, risk
   * levels). `en-US` deliberately maps every key to itself; `zh-CN` supplies a
   * readable label. Unknown values are rendered verbatim by `statusLabel()`.
   */
  status: {
    ok: "ok",
    degraded: "degraded",
    error: "error",
    connected: "connected",
    connecting: "connecting",
    disconnected: "disconnected",
    running: "running",
    pending: "pending",
    waiting_for_confirmation: "waiting_for_confirmation",
    succeeded: "succeeded",
    failed: "failed",
    cancelled: "cancelled",
    unknown: "unknown",
    not_configured: "not_configured",
    read_only: "read_only",
    low: "low",
    moderate: "moderate",
    high: "high",
    critical: "critical",
  },

  errors: {
    unavailable: "The local runtime is unavailable.",
    unknown: "Unexpected error: {detail}",
    detail: "({detail})",
    codeUnknown: "The request failed with an unrecognised error code ({code}).",
    codes: {
      deskweave_error: "The runtime reported an internal error.",
      config_error: "The runtime is misconfigured.",
      validation_error: "The request was rejected because its payload is invalid.",
      not_found: "The requested item does not exist.",
      conflict: "The task cannot be changed in its current state.",
      lifecycle_error: "The operation is not allowed in the current lifecycle state.",
      persistence_error: "A storage operation failed.",
      adapter_error: "An adapter failed.",
      adapter_connection_error: "An adapter could not be connected.",
      adapter_execution_error: "An adapter failed while executing.",
      adapter_timeout_error: "An adapter operation timed out.",
      permission_denied: "This operation is not permitted.",
      unsafe_resource: "The resource was rejected by the safety checks.",
      browser_access_denied: "The site refused automated access. Open it manually and try again.",
      not_implemented: "This feature is not implemented yet.",
      artifact_open_failed: "The operating system could not open or reveal the file.",
      unauthorized: "The runtime rejected the request token.",
      internal_error: "The runtime hit an internal error.",
      preview_failed: "The preview could not be loaded.",
      interrupted: "The task was interrupted and did not finish.",
      no_candidates: "No usable candidates were found.",
      collection_failed: "Collecting reference images failed.",
      target_execution_failed: "Running Blender failed.",
    },
  },
};

/** Shape every locale catalog must implement. */
export type MessageSchema = typeof enUS;

export default enUS;
