/**
 * The only place the UI talks to the local runtime.
 *
 * Two transports are supported:
 *  - inside Tauri, requests go through the `runtime_request` Rust command, so the
 *    shared token never reaches the webview;
 *  - in a plain browser (dev server), requests go directly to the loopback
 *    control API using `VITE_DESKWEAVE_RUNTIME_URL` / `VITE_DESKWEAVE_RUNTIME_TOKEN`.
 *
 * The UI never fabricates a connection: failures surface as
 * `RuntimeUnavailableError` and are rendered as an explicit error state.
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
}

export type ConnectionSource = "tauri" | "http";

export class RuntimeUnavailableError extends Error {
  override readonly cause?: unknown;

  constructor(message: string, cause?: unknown) {
    super(message);
    this.name = "RuntimeUnavailableError";
    this.cause = cause;
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

async function requestJson<T>(path: string): Promise<T> {
  const invoke = tauriInvoke();
  if (invoke) {
    try {
      return (await invoke("runtime_request", { path })) as T;
    } catch (error) {
      throw new RuntimeUnavailableError(
        `the desktop shell could not reach the local runtime: ${String(error)}`,
        error,
      );
    }
  }

  const baseUrl = runtimeBaseUrl();
  const token = import.meta.env.VITE_DESKWEAVE_RUNTIME_TOKEN ?? "";
  try {
    const response = await fetch(`${baseUrl}${path}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return (await response.json()) as T;
  } catch (error) {
    throw new RuntimeUnavailableError(
      `could not reach the local runtime at ${baseUrl} (is it running?)`,
      error,
    );
  }
}

export function fetchHealth(): Promise<RuntimeHealth> {
  return requestJson<RuntimeHealth>("/health");
}

export function fetchAdapters(): Promise<{ adapters: AdapterDescriptor[] }> {
  return requestJson<{ adapters: AdapterDescriptor[] }>("/adapters");
}

export function fetchTasks(): Promise<{ tasks: TaskRecord[] }> {
  return requestJson<{ tasks: TaskRecord[] }>("/tasks");
}
