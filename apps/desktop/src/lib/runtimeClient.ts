/**
 * The only place the UI talks to the local runtime.
 *
 * Two transports are supported:
 *  - inside Tauri, requests go through the `runtime_request` Rust command, so the
 *    shared token never reaches the webview;
 *  - in a plain browser (dev server), requests go directly to the loopback
 *    control API using `VITE_DESKWEAVE_RUNTIME_URL` / `VITE_DESKWEAVE_RUNTIME_TOKEN`.
 *
 * The UI never fabricates a connection: transport failures surface as
 * `RuntimeUnavailableError`, and runtime rejections as `RuntimeApiError` carrying
 * the stable `code` the server sent. Both are rendered as explicit states.
 */

export interface ComponentHealth {
  name: string;
  state: string;
  detail: string;
}

export interface AdapterHealth {
  adapter_id: string;
  status: string;
  capabilities: string[];
}

export interface RuntimeHealth {
  state: string;
  version: string;
  uptime_seconds: number;
  schema_version: number;
  database_path: string;
  components: ComponentHealth[];
  adapters: AdapterHealth[];
}

export interface Capability {
  name: string;
  description: string;
  risk: string;
  scope: string;
  requires_confirmation: boolean;
}

export interface AdapterDescriptor {
  adapter_id: string;
  name: string;
  version: string;
  description: string;
  capabilities: Capability[];
}

export interface TaskRecord {
  task_id: string;
  run_id: string;
  name: string;
  status: string;
  created_at: string;
  updated_at: string;
  error?: ApiErrorInfo | null;
  result?: Record<string, unknown>;
}

export interface ApiErrorInfo {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export interface ArtifactMetadata {
  title?: string | null;
  source?: string | null;
  source_page?: string | null;
  original_url?: string | null;
  sha256?: string | null;
  width?: number | null;
  height?: number | null;
}

export interface ArtifactView {
  artifact_id: string;
  kind: string;
  media_type: string | null;
  size_bytes: number | null;
  metadata: ArtifactMetadata;
  created_at: string;
}

export interface TaskView {
  task: TaskRecord;
  artifacts: ArtifactView[];
}

export interface CandidateFailure {
  url: string;
  reason: string;
}

export type ConnectionSource = "tauri" | "http";

/** The runtime could not be reached at all (not running, wrong port, no IPC). */
export class RuntimeUnavailableError extends Error {
  override readonly cause?: unknown;

  constructor(message: string, cause?: unknown) {
    super(message);
    this.name = "RuntimeUnavailableError";
    this.cause = cause;
  }
}

/** The runtime answered and rejected the request with a stable error code. */
export class RuntimeApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details: Record<string, unknown>;

  constructor(info: ApiErrorInfo, status = 0) {
    super(info.message);
    this.name = "RuntimeApiError";
    this.code = info.code;
    this.status = status;
    this.details = info.details ?? {};
  }
}

type TauriInvoke = (command: string, args?: Record<string, unknown>) => Promise<unknown>;

function tauriInvoke(): TauriInvoke | null {
  const candidate = (window as unknown as { __TAURI_INTERNALS__?: { invoke?: TauriInvoke } })
    .__TAURI_INTERNALS__;
  const invoke = candidate?.invoke;
  return typeof invoke === "function" ? invoke : null;
}

export function connectionSource(): ConnectionSource {
  return tauriInvoke() ? "tauri" : "http";
}

function runtimeBaseUrl(): string {
  return import.meta.env.VITE_DESKWEAVE_RUNTIME_URL ?? "http://127.0.0.1:8787";
}

function isApiErrorShape(value: unknown): value is ApiErrorInfo {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { code?: unknown }).code === "string" &&
    typeof (value as { message?: unknown }).message === "string"
  );
}

async function request<T>(
  method: "GET" | "POST",
  path: string,
  body?: Record<string, unknown>,
): Promise<T> {
  const invoke = tauriInvoke();
  if (invoke) {
    try {
      return (await invoke("runtime_request", { method, path, body })) as T;
    } catch (error) {
      if (isApiErrorShape(error)) {
        const status = (error as { status?: number }).status ?? 0;
        if (error.code === "runtime_unreachable" || status === 0) {
          throw new RuntimeUnavailableError(error.message, error);
        }
        throw new RuntimeApiError(error, status);
      }
      throw new RuntimeUnavailableError(
        `the desktop shell could not reach the local runtime: ${String(error)}`,
        error,
      );
    }
  }

  const baseUrl = runtimeBaseUrl();
  const token = import.meta.env.VITE_DESKWEAVE_RUNTIME_TOKEN ?? "";
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response: Response;
  try {
    response = await fetch(`${baseUrl}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    throw new RuntimeUnavailableError(
      `could not reach the local runtime at ${baseUrl} (is it running?)`,
      error,
    );
  }

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const info = (payload as { error?: unknown } | null)?.error;
    if (isApiErrorShape(info)) {
      throw new RuntimeApiError(info, response.status);
    }
    throw new RuntimeUnavailableError(
      `the runtime returned HTTP ${response.status} without a structured error`,
    );
  }
  return payload as T;
}

export function fetchHealth(): Promise<RuntimeHealth> {
  return request<RuntimeHealth>("GET", "/health");
}

export function fetchAdapters(): Promise<{ adapters: AdapterDescriptor[] }> {
  return request<{ adapters: AdapterDescriptor[] }>("GET", "/adapters");
}

export async function fetchTasks(): Promise<{ tasks: TaskRecord[] }> {
  const response = await request<{ tasks: TaskView[] }>("GET", "/tasks");
  return { tasks: response.tasks.map((view) => view.task) };
}

export function fetchTaskViews(status?: string): Promise<{ tasks: TaskView[] }> {
  const path = status ? `/tasks?status=${encodeURIComponent(status)}` : "/tasks";
  return request<{ tasks: TaskView[] }>("GET", path);
}

export function fetchTaskView(taskId: string): Promise<TaskView> {
  return request<TaskView>("GET", `/tasks/${taskId}`);
}

export function startCollection(query: string, limit: number): Promise<TaskView> {
  return request<TaskView>("POST", "/tasks", { query, limit });
}

export function confirmSelection(
  taskId: string,
  artifactIds: string[],
  planeName?: string,
): Promise<TaskView> {
  const body: Record<string, unknown> = { artifact_ids: artifactIds };
  if (planeName) body.plane_name = planeName;
  return request<TaskView>("POST", `/tasks/${taskId}/confirm`, body);
}

export function cancelTask(taskId: string): Promise<TaskView> {
  return request<TaskView>("POST", `/tasks/${taskId}/cancel`, {});
}

export type ArtifactAction = "open" | "reveal";

/**
 * Ask the runtime to hand a produced artifact to the operating system.
 *
 * The webview never learns the path: it names an artifact id and an action, and
 * the runtime resolves the file (re-checking containment) before launching. A
 * failure (missing file, no handler registered, unsupported platform) is a
 * structured error, never a silent no-op.
 */
export async function openArtifact(
  artifactId: string,
  action: ArtifactAction,
): Promise<{ artifact_id: string; action: string; opened: boolean }> {
  const response = await request<{
    artifact: { artifact_id: string; action: string; opened: boolean };
  }>("POST", `/artifacts/${artifactId}/open`, { action });
  return response.artifact;
}

/**
 * Fetch an artifact image as a data URL.
 *
 * The token must never reach the webview, so inside Tauri the bytes are fetched by
 * the Rust bridge; in browser dev mode a plain URL with inline credentials would
 * leak, so the same request path is used and converted here.
 */
export async function fetchArtifactPreview(artifactId: string): Promise<string> {
  const invoke = tauriInvoke();
  if (invoke) {
    try {
      return (await invoke("artifact_preview", { artifactId })) as string;
    } catch (error) {
      if (isApiErrorShape(error)) {
        throw new RuntimeApiError(error, (error as { status?: number }).status ?? 0);
      }
      throw new RuntimeUnavailableError(
        `the desktop shell could not load the preview: ${String(error)}`,
        error,
      );
    }
  }

  const baseUrl = runtimeBaseUrl();
  const token = import.meta.env.VITE_DESKWEAVE_RUNTIME_TOKEN ?? "";
  const response = await fetch(`${baseUrl}/artifacts/${artifactId}/preview`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) {
    throw new RuntimeApiError(
      { code: "preview_failed", message: `HTTP ${response.status}` },
      response.status,
    );
  }
  const blob = await response.blob();
  return await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("could not read the preview"));
    reader.readAsDataURL(blob);
  });
}
