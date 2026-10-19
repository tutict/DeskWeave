import { ref, shallowRef } from "vue";

import {
  fetchAdapters,
  fetchHealth,
  fetchTasks,
  RuntimeUnavailableError,
  type AdapterDescriptor,
  type RuntimeHealth,
  type TaskRecord,
} from "./runtimeClient";

export type LoadState = "loading" | "ready" | "error";

/** Loads real runtime state and exposes explicit loading/error states. */
export function useRuntime() {
  const state = ref<LoadState>("loading");
  const error = ref<string | null>(null);
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
      error.value = caught instanceof RuntimeUnavailableError ? caught.message : String(caught);
      state.value = "error";
    }
  }

  return { state, error, health, adapters, tasks, refresh };
}
