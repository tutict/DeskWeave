<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";

import EmptyState from "../components/EmptyState.vue";
import ErrorState from "../components/ErrorState.vue";
import LoadingState from "../components/LoadingState.vue";
import ReferenceCard from "../components/ReferenceCard.vue";
import StatusBadge from "../components/StatusBadge.vue";
import type { ArtifactView, CandidateFailure, TaskView } from "../lib/runtimeClient";
import { DEFAULT_LIMIT, useReferences } from "../lib/useReferences";

const {
  state,
  error,
  offline,
  views,
  submitting,
  busyTaskId,
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
} = useReferences();

const query = ref("");
const limit = ref(DEFAULT_LIMIT);
const planeNames = reactive<Record<string, string>>({});

const canSubmit = computed(() => query.value.trim().length > 0 && !submitting.value);

onMounted(() => void refresh());

// Previews are loaded for every card that is on screen, including the ones kept
// after a confirmation (that is how a reloaded task still shows its images).
watch(
  views,
  (current) => {
    for (const view of current) {
      if (!showsCandidates(view)) continue;
      for (const artifact of imageCandidatesOf(view)) {
        void loadPreview(artifact.artifact_id);
      }
    }
  },
  { immediate: true },
);

async function submit(): Promise<void> {
  if (!canSubmit.value) return;
  await start(query.value, limit.value);
}

function queryOf(view: TaskView): string {
  const value = view.task.result?.query;
  return typeof value === "string" ? value : view.task.name;
}

function failuresOf(view: TaskView): CandidateFailure[] {
  const value = view.task.result?.failures;
  return Array.isArray(value) ? (value as CandidateFailure[]) : [];
}

function executionOf(view: TaskView): Record<string, unknown> | null {
  const value = view.task.result?.execution;
  return value && typeof value === "object" ? (value as Record<string, unknown>) : null;
}

function planeOf(view: TaskView): { name?: string; image_count?: number } {
  const execution = executionOf(view);
  const plane = execution?.plane;
  return plane && typeof plane === "object"
    ? (plane as { name?: string; image_count?: number })
    : {};
}

function blendArtifactOf(view: TaskView): string | null {
  const execution = executionOf(view);
  const value = execution?.blend_artifact_id;
  return typeof value === "string" ? value : null;
}

/** The registered artifact behind ``blend_artifact_id``, or null when it is gone. */
function blendFileOf(view: TaskView): ArtifactView | null {
  const id = blendArtifactOf(view);
  if (!id) return null;
  return view.artifacts.find((artifact) => artifact.artifact_id === id) ?? null;
}

/** Reported when Blender wrote the file but the store refused to address it. */
function blendWarningOf(view: TaskView): string | null {
  const execution = executionOf(view);
  const value = execution?.blend_artifact_error;
  return typeof value === "string" ? value : null;
}

function objectNamesOf(view: TaskView): string[] {
  const plane = executionOf(view)?.plane;
  if (!plane || typeof plane !== "object") return [];
  const objects = (plane as { objects?: unknown }).objects;
  return Array.isArray(objects)
    ? objects.filter((name): name is string => typeof name === "string")
    : [];
}

function formatBytes(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "size unknown";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

/** Recovery note written by the runtime when a task survived an interruption. */
function recoveryOf(view: TaskView): { action: string; detail: string } | null {
  const value = view.task.result?.recovery;
  if (!value || typeof value !== "object") return null;
  const note = value as { action?: unknown; detail?: unknown };
  if (typeof note.action !== "string") return null;
  return { action: note.action, detail: typeof note.detail === "string" ? note.detail : "" };
}

function isOpening(taskId: string): boolean {
  return openingTaskId.value === taskId;
}

function confirmedAtOf(view: TaskView): string | null {
  const confirmation = view.task.result?.confirmation;
  if (!confirmation || typeof confirmation !== "object") return null;
  const value = (confirmation as { confirmed_at?: unknown }).confirmed_at;
  return typeof value === "string" ? value : null;
}

function confirmedIdsOf(view: TaskView): string[] {
  const confirmation = view.task.result?.confirmation;
  if (!confirmation || typeof confirmation !== "object") return [];
  const value = (confirmation as { artifact_ids?: unknown }).artifact_ids;
  return Array.isArray(value) ? value.filter((id): id is string => typeof id === "string") : [];
}

/** Candidates stay on screen after confirmation so the user can see what was used. */
function showsCandidates(view: TaskView): boolean {
  return view.task.status === "waiting_for_confirmation" || confirmedIdsOf(view).length > 0;
}

/**
 * Only image artifacts are selectable. A confirmed task also owns the ``.blend``
 * it produced, and that one must never show up as a candidate card.
 */
function imageCandidatesOf(view: TaskView): ArtifactView[] {
  return view.artifacts.filter((artifact) => artifact.kind === "image");
}

function isChosen(view: TaskView, artifactId: string): boolean {
  return isSelected(view.task.task_id, artifactId) || confirmedIdsOf(view).includes(artifactId);
}

function canEditSelection(view: TaskView): boolean {
  return view.task.status === "waiting_for_confirmation" && !busy(view);
}

function busy(view: TaskView): boolean {
  return busyTaskId.value === view.task.task_id;
}

function selectionCount(view: TaskView): number {
  return selected(view.task.task_id).length;
}

function clearSelection(taskId: string): void {
  for (const artifactId of selected(taskId)) {
    toggle(taskId, artifactId);
  }
}
</script>

<template>
  <section class="references">
    <header class="intro">
      <h2>Reference collection</h2>
      <p>
        The browser connector searches for reference images and stores them locally. Nothing is sent
        to Blender until you select the images and confirm.
      </p>
    </header>

    <form class="search" @submit.prevent="submit">
      <label class="search__field">
        <span class="search__label">Search query</span>
        <input
          v-model="query"
          type="search"
          name="query"
          placeholder="e.g. anime character turnaround sheet"
          maxlength="200"
          :disabled="submitting"
        />
      </label>
      <label class="search__field search__field--small">
        <span class="search__label">Candidates</span>
        <input
          v-model.number="limit"
          type="number"
          name="limit"
          min="1"
          max="20"
          :disabled="submitting"
        />
      </label>
      <button type="submit" class="primary" data-test="submit" :disabled="!canSubmit">
        {{ submitting ? "Working…" : "Collect references" }}
      </button>
    </form>

    <p v-if="error" class="banner" :class="{ 'banner--offline': offline }" role="alert">
      <span>{{ error }}</span>
      <button type="button" class="link" @click="refresh()">Retry</button>
    </p>

    <LoadingState v-if="state === 'loading' && views.length === 0" message="Loading tasks…" />

    <ErrorState
      v-else-if="state === 'error' && views.length === 0"
      :title="offline ? 'Runtime unavailable' : 'Could not load tasks'"
      :message="error ?? 'unknown error'"
      @retry="refresh()"
    />

    <EmptyState
      v-else-if="views.length === 0"
      title="No reference tasks yet"
      description="Start a collection above. Candidates appear here for review before anything is sent to Blender."
    />

    <template v-else>
      <article v-for="view in views" :key="view.task.task_id" class="task">
        <header class="task__head">
          <div>
            <h3>{{ queryOf(view) }}</h3>
            <p class="task__meta">
              <span class="mono">{{ view.task.task_id }}</span>
              <span>·</span>
              <span>{{ imageCandidatesOf(view).length }} candidate(s)</span>
              <span>·</span>
              <span>updated {{ view.task.updated_at }}</span>
            </p>
          </div>
          <StatusBadge :status="view.task.status" />
        </header>

        <p v-if="view.task.status === 'running' || view.task.status === 'pending'" class="note">
          Collecting and downloading candidates…
        </p>

        <p v-if="view.task.error" class="error" role="alert">
          <strong>{{ view.task.error.code }}</strong>
          {{ view.task.error.message }}
        </p>

        <p v-if="recoveryOf(view)" class="note note--recovery" data-test="recovery-note">
          Recovered after an interruption ({{ recoveryOf(view)!.action }}) —
          {{ recoveryOf(view)!.detail }}
        </p>

        <ul v-if="failuresOf(view).length" class="failures">
          <li class="failures__head">
            {{ failuresOf(view).length }} candidate(s) could not be saved
          </li>
          <li v-for="failure in failuresOf(view)" :key="failure.url" class="failures__item">
            <span class="mono failures__url">{{ failure.url }}</span>
            <span>{{ failure.reason }}</span>
          </li>
        </ul>

        <template v-if="showsCandidates(view)">
          <p v-if="view.task.status === 'waiting_for_confirmation'" class="note">
            Select the images this task should use. Confirming runs Blender once for this selection.
          </p>
          <div class="grid">
            <ReferenceCard
              v-for="artifact in imageCandidatesOf(view)"
              :key="artifact.artifact_id"
              :artifact="artifact"
              :selected="isChosen(view, artifact.artifact_id)"
              :preview="previews[artifact.artifact_id]"
              :preview-error="previewErrors[artifact.artifact_id]"
              :loading="loadingPreviews[artifact.artifact_id]"
              :disabled="!canEditSelection(view)"
              @toggle="toggle(view.task.task_id, artifact.artifact_id)"
            />
          </div>
        </template>

        <div v-if="view.task.status === 'waiting_for_confirmation'" class="actions">
          <label class="search__field search__field--small">
            <span class="search__label">Plane name (optional)</span>
            <input
              v-model="planeNames[view.task.task_id]"
              data-test="plane-name"
              type="text"
              maxlength="64"
              placeholder="ReferencePlane"
              :disabled="busy(view)"
            />
          </label>
          <p class="actions__count">{{ selectionCount(view) }} selected</p>
          <button
            type="button"
            class="link"
            :disabled="selectionCount(view) === 0 || busy(view)"
            @click="clearSelection(view.task.task_id)"
          >
            Clear
          </button>
          <button
            type="button"
            class="secondary"
            data-test="cancel"
            :disabled="busy(view)"
            @click="cancel(view.task.task_id)"
          >
            Cancel task
          </button>
          <button
            type="button"
            class="primary"
            data-test="confirm"
            :disabled="selectionCount(view) === 0 || busy(view)"
            @click="confirm(view.task.task_id, planeNames[view.task.task_id])"
          >
            {{ busy(view) ? "Confirming…" : "Confirm and continue" }}
          </button>
        </div>

        <template v-if="view.task.status === 'succeeded'">
          <p class="success">
            Confirmed {{ confirmedAtOf(view) ?? "" }} ·
            {{ planeOf(view).name ?? "reference plane" }} with
            {{ planeOf(view).image_count ?? imageCandidatesOf(view).length }} image(s).
          </p>

          <div v-if="blendFileOf(view)" class="produced" data-test="produced">
            <div class="produced__meta">
              <strong>Blender scene</strong>
              <span v-if="blendArtifactOf(view)" class="mono">
                blend artifact {{ blendArtifactOf(view) }}</span
              >
              <span>{{ formatBytes(blendFileOf(view)!.size_bytes) }}</span>
              <span>created {{ blendFileOf(view)!.created_at }}</span>
              <span>{{ blendFileOf(view)!.media_type ?? "application/x-blender" }}</span>
              <span v-if="objectNamesOf(view).length">
                {{ objectNamesOf(view).length }} object(s):
                <span class="mono">{{ objectNamesOf(view).join(", ") }}</span>
              </span>
            </div>
            <p class="note">
              A .blend file cannot be previewed in the browser; open it in Blender or reveal it on
              disk.
            </p>
            <div class="produced__actions">
              <button
                type="button"
                data-test="reveal-artifact"
                :disabled="isOpening(view.task.task_id)"
                @click="openProduced(view.task.task_id, blendFileOf(view)!.artifact_id, 'reveal')"
              >
                Show in folder
              </button>
              <button
                type="button"
                data-test="open-artifact"
                :disabled="isOpening(view.task.task_id)"
                @click="openProduced(view.task.task_id, blendFileOf(view)!.artifact_id, 'open')"
              >
                Open in Blender
              </button>
            </div>
            <p
              v-if="openErrors[view.task.task_id]"
              class="error"
              role="alert"
              data-test="open-error"
            >
              {{ openErrors[view.task.task_id] }}
            </p>
          </div>

          <p
            v-else-if="blendArtifactOf(view)"
            class="error"
            role="alert"
            data-test="missing-artifact"
          >
            The scene was recorded as
            <span class="mono">{{ blendArtifactOf(view) }}</span>
            but that artifact is no longer in the store, so it cannot be opened.
          </p>

          <p v-if="blendWarningOf(view)" class="error" role="alert" data-test="blend-warning">
            The scene was produced but could not be registered: {{ blendWarningOf(view) }}
          </p>
        </template>
      </article>
    </template>
  </section>
</template>

<style scoped>
.references {
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}
.intro h2 {
  margin: 0 0 0.35rem;
  font-size: 1.1rem;
}
.intro p {
  margin: 0;
  color: var(--dw-muted);
  font-size: 0.85rem;
  max-width: 60ch;
}
.search {
  display: flex;
  align-items: flex-end;
  gap: 0.75rem;
  flex-wrap: wrap;
  padding: 1rem;
  border: 1px solid var(--dw-border);
  border-radius: 0.5rem;
  background: var(--dw-surface);
}
.search__field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  flex: 1 1 18rem;
}
.search__field--small {
  flex: 0 0 9rem;
}
.search__label {
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--dw-muted);
}
input {
  padding: 0.5rem 0.65rem;
  border: 1px solid var(--dw-border);
  border-radius: 0.375rem;
  background: var(--dw-bg);
  color: var(--dw-text);
  font: inherit;
}
input:disabled {
  opacity: 0.6;
}
button {
  font: inherit;
  cursor: pointer;
  border-radius: 0.375rem;
  padding: 0.5rem 1rem;
  border: 1px solid var(--dw-border);
  background: transparent;
  color: var(--dw-text);
}
button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.primary {
  background: var(--dw-accent);
  border-color: var(--dw-accent);
  color: white;
  font-weight: 600;
}
.secondary {
  border-color: var(--dw-danger);
  color: var(--dw-danger);
}
.link {
  border: none;
  color: var(--dw-accent);
  padding: 0.25rem 0.35rem;
  background: transparent;
}
.banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  margin: 0;
  padding: 0.6rem 0.9rem;
  border-radius: 0.375rem;
  border: 1px solid var(--dw-danger);
  color: var(--dw-danger);
  background: color-mix(in srgb, var(--dw-danger) 8%, transparent);
  font-size: 0.85rem;
}
.task {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  padding: 1rem;
  border: 1px solid var(--dw-border);
  border-radius: 0.5rem;
  background: var(--dw-surface);
}
.task__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
}
.task__head h3 {
  margin: 0 0 0.2rem;
  font-size: 0.95rem;
}
.task__meta {
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  color: var(--dw-muted);
  font-size: 0.72rem;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(11rem, 1fr));
  gap: 0.75rem;
}
.actions {
  display: flex;
  align-items: flex-end;
  gap: 0.75rem;
  flex-wrap: wrap;
  padding-top: 0.5rem;
  border-top: 1px solid var(--dw-border);
}
.actions__count {
  margin: 0;
  font-size: 0.8rem;
  color: var(--dw-muted);
  flex: 1;
}
.note {
  margin: 0;
  color: var(--dw-muted);
  font-size: 0.8rem;
}
.error {
  margin: 0;
  padding: 0.5rem 0.75rem;
  border-left: 3px solid var(--dw-danger);
  background: color-mix(in srgb, var(--dw-danger) 8%, transparent);
  color: var(--dw-danger);
  font-size: 0.8rem;
}
.success {
  margin: 0;
  padding: 0.5rem 0.75rem;
  border-left: 3px solid var(--dw-ok);
  color: var(--dw-ok);
  font-size: 0.8rem;
}
.failures {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  color: var(--dw-muted);
  font-size: 0.72rem;
}
.failures__head {
  color: var(--dw-text);
  font-weight: 600;
}
.failures__item {
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
  padding-left: 0.6rem;
  border-left: 2px solid var(--dw-border);
  min-width: 0;
}
.failures__url {
  overflow-wrap: anywhere;
  opacity: 0.85;
}
.note--recovery {
  padding: 0.4rem 0.6rem;
  border-left: 3px solid var(--dw-accent);
  background: color-mix(in srgb, var(--dw-accent) 8%, transparent);
  color: var(--dw-text);
}
.produced {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding: 0.6rem 0.75rem;
  border: 1px solid var(--dw-border);
  border-radius: 0.375rem;
  background: var(--dw-bg);
}
.produced__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem 0.75rem;
  font-size: 0.75rem;
  color: var(--dw-muted);
}
.produced__meta strong {
  color: var(--dw-text);
}
.produced__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}
.produced__actions button {
  padding: 0.35rem 0.7rem;
  font-size: 0.8rem;
}
.mono {
  font-family: var(--dw-mono);
}
</style>
