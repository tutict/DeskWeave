import { computed, onScopeDispose, reactive, ref, shallowRef } from "vue";

import {
  cancelTask,
  confirmSelection,
  fetchArtifactPreview,
  fetchTaskViews,
  openArtifact,
  RuntimeUnavailableError,
  startCollection,
  type ArtifactAction,
  type TaskView,
} from "./runtimeClient";

export type LoadState = "loading" | "ready" | "error";

export const DEFAULT_LIMIT = 6;
const AUTO_REFRESH_MS = 2000;

function isRunning(view: TaskView): boolean {
  return view.task.status === "running" || view.task.status === "pending";
}

/**
 * State for the reference-collection screen.
 *
 * Everything that describes a task comes from the runtime; the only client-side
 * state is the user's *selection*, which is intentionally not persisted — a stale
 * page must not be able to confirm anything the user cannot see.
 */
export function useReferences() {
  const state = ref<LoadState>("loading");
  const error = ref<string | null>(null);
  const offline = ref(false);
  const views = shallowRef<TaskView[]>([]);
  const selection = reactive<Record<string, string[]>>({});
  const previews = reactive<Record<string, string>>({});
  const previewErrors = reactive<Record<string, string>>({});
  const loadingPreviews = reactive<Record<string, boolean>>({});
  const openErrors = reactive<Record<string, string>>({});
  const openingTaskId = ref<string | null>(null);
  const submitting = ref(false);
  const busyTaskId = ref<string | null>(null);

  let timer: ReturnType<typeof setInterval> | null = null;

  const hasRunningTask = computed(() => views.value.some(isRunning));

  function stopPolling(): void {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  function syncPolling(): void {
    if (hasRunningTask.value) {
      if (timer === null) {
        timer = setInterval(() => {
          void refresh(true);
        }, AUTO_REFRESH_MS);
      }
    } else {
      stopPolling();
    }
  }

  async function refresh(silent = false): Promise<void> {
    if (!silent) {
      state.value = "loading";
      error.value = null;
    }
    try {
      const response = await fetchTaskViews();
      views.value = response.tasks;
      error.value = null;
      offline.value = false;
      state.value = "ready";
      for (const view of response.tasks) {
        if (!(view.task.task_id in selection)) {
          selection[view.task.task_id] = [];
        }
      }
    } catch (caught) {
      if (silent) {
        // A background poll must not wipe a usable screen; surface it softly.
        error.value = describe(caught);
        return;
      }
      error.value = describe(caught);
      offline.value = caught instanceof RuntimeUnavailableError;
      state.value = "error";
    } finally {
      syncPolling();
    }
  }

  async function start(query: string, limit = DEFAULT_LIMIT): Promise<boolean> {
    const trimmed = query.trim();
    if (!trimmed || submitting.value) return false;
    submitting.value = true;
    error.value = null;
    try {
      const view = await startCollection(trimmed, limit);
      views.value = [
        view,
        ...views.value.filter((item) => item.task.task_id !== view.task.task_id),
      ];
      selection[view.task.task_id] = [];
      state.value = "ready";
      syncPolling();
      return true;
    } catch (caught) {
      error.value = describe(caught);
      offline.value = caught instanceof RuntimeUnavailableError;
      return false;
    } finally {
      submitting.value = false;
    }
  }

  function toggle(taskId: string, artifactId: string): void {
    const current = selection[taskId] ?? [];
    selection[taskId] = current.includes(artifactId)
      ? current.filter((id) => id !== artifactId)
      : [...current, artifactId];
  }

  function selected(taskId: string): string[] {
    return selection[taskId] ?? [];
  }

  function isSelected(taskId: string, artifactId: string): boolean {
    return (selection[taskId] ?? []).includes(artifactId);
  }

  async function confirm(taskId: string, planeName?: string): Promise<boolean> {
    const artifactIds = selected(taskId);
    if (artifactIds.length === 0 || busyTaskId.value !== null) return false;
    busyTaskId.value = taskId;
    submitting.value = true;
    error.value = null;
    try {
      const view = await confirmSelection(taskId, artifactIds, planeName?.trim() || undefined);
      replace(view);
      return true;
    } catch (caught) {
      // Reload authoritative state first: the task may have moved on (a duplicate
      // confirm from a stale page). The error must be reported *after* the
      // refresh, because a successful refresh clears the banner.
      await refresh(true);
      error.value = describe(caught);
      offline.value = caught instanceof RuntimeUnavailableError;
      return false;
    } finally {
      submitting.value = false;
      busyTaskId.value = null;
      syncPolling();
    }
  }

  async function cancel(taskId: string): Promise<boolean> {
    if (busyTaskId.value !== null) return false;
    busyTaskId.value = taskId;
    try {
      const view = await cancelTask(taskId);
      replace(view);
      return true;
    } catch (caught) {
      await refresh(true);
      error.value = describe(caught);
      offline.value = caught instanceof RuntimeUnavailableError;
      return false;
    } finally {
      busyTaskId.value = null;
      syncPolling();
    }
  }

  /**
   * Hand a produced artifact to the OS ("show in folder" / "open").
   *
   * The UI only names the artifact; the runtime resolves and verifies the file.
   * A refusal is shown as text next to the task, never swallowed.
   */
  async function openProduced(
    taskId: string,
    artifactId: string,
    action: ArtifactAction,
  ): Promise<boolean> {
    if (openingTaskId.value !== null) return false;
    openingTaskId.value = taskId;
    delete openErrors[taskId];
    try {
      await openArtifact(artifactId, action);
      return true;
    } catch (caught) {
      openErrors[taskId] = describe(caught);
      return false;
    } finally {
      openingTaskId.value = null;
    }
  }

  async function loadPreview(artifactId: string): Promise<void> {
    if (previews[artifactId] || loadingPreviews[artifactId]) return;
    loadingPreviews[artifactId] = true;
    try {
      previews[artifactId] = await fetchArtifactPreview(artifactId);
      delete previewErrors[artifactId];
    } catch (caught) {
      previewErrors[artifactId] = describe(caught);
    } finally {
      delete loadingPreviews[artifactId];
    }
  }

  function replace(view: TaskView): void {
    const others = views.value.filter((item) => item.task.task_id !== view.task.task_id);
    views.value = [view, ...others];
  }

  onScopeDispose(stopPolling);

  return {
    state,
    error,
    offline,
    views,
    submitting,
    busyTaskId,
    selection,
    previews,
    previewErrors,
    loadingPreviews,
    openErrors,
    openingTaskId,
    refresh,
    start,
    toggle,
    selected,
    isSelected,
    confirm,
    cancel,
    loadPreview,
    openProduced,
    stopPolling,
  };
}

function describe(caught: unknown): string {
  if (caught instanceof Error) return caught.message;
  return String(caught);
}
