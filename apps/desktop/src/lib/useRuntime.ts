import { ref, shallowRef } from "vue";

import { toAppError, type AppError } from "../i18n/errors";
import {
  fetchAdapters,
  fetchHealth,
  fetchTasks,
  type AdapterDescriptor,
  type RuntimeHealth,
  type TaskRecord,
} from "./runtimeClient";

export type LoadState = "loading" | "ready" | "error";

/**
 * Loads real runtime state and exposes explicit loading/error states.
 *
 * The failure is kept structured (kind + code + detail) rather than as a string so
 * the view can render it in the active language, including after a language
 * switch that happens while the error is on screen.
 */
export function useRuntime() {
  const state = ref<LoadState>("loading");
  const error = ref<AppError | null>(null);
  const health = shallowRef<RuntimeHealth | null>(null);
  const adapters = shallowRef<AdapterDescriptor[]>([]);
  const tasks = shallowRef<TaskRecord[]>([]);

  async function refresh(): Promise<void> {
    state.value = "loading";
    error.value = null;
    try {
      const [healthResult, adaptersResult, tasksResult] = await Promise.all([
        fetchHealth(),
        fetchAdapters(),
        fetchTasks(),
      ]);
      health.value = healthResult;
      adapters.value = adaptersResult.adapters;
      tasks.value = tasksResult.tasks;
      state.value = "ready";
    } catch (caught) {
      error.value = toAppError(caught);
      state.value = "error";
    }
  }

  return { state, error, health, adapters, tasks, refresh };
}
