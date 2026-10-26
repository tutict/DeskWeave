<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";

import ErrorState from "./components/ErrorState.vue";
import LoadingState from "./components/LoadingState.vue";
import StatusBadge from "./components/StatusBadge.vue";
import { appErrorMessage } from "./i18n/errors";
import { connectionSource } from "./lib/runtimeClient";
import { useRuntime } from "./lib/useRuntime";
import LogsView from "./views/LogsView.vue";
import OverviewView from "./views/OverviewView.vue";
import ReferencesView from "./views/ReferencesView.vue";
import SettingsView from "./views/SettingsView.vue";
import TasksView from "./views/TasksView.vue";

type TabId = "overview" | "tasks" | "references" | "logs" | "settings";

const { t } = useI18n();

/**
 * Labels are resolved inside a `computed`, not once at setup: `t()` reads the
 * locale, so a language switch relabels the navigation without a remount, and the
 * keys stay literal (checked at compile time) instead of being built by hand.
 */
const tabs = computed<readonly { id: TabId; label: string }[]>(() => [
  { id: "overview", label: t("app.tabs.overview") },
  { id: "tasks", label: t("app.tabs.tasks") },
  { id: "references", label: t("app.tabs.references") },
  { id: "logs", label: t("app.tabs.logs") },
  { id: "settings", label: t("app.tabs.settings") },
]);

const active = ref<TabId>("overview");
const { state, error, health, adapters, tasks, refresh } = useRuntime();
const source = connectionSource();

const runtimeState = computed(() => health.value?.state ?? "unknown");

onMounted(refresh);
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand">
        <span class="brand__mark" aria-hidden="true">DW</span>
        <div>
          <h1>DeskWeave</h1>
          <p class="brand__tagline">{{ t("app.tagline") }}</p>
        </div>
      </div>
      <div class="topbar__status">
        <span class="muted">{{ t("app.runtime") }}</span>
        <StatusBadge :status="runtimeState" />
        <span class="muted">{{ t("app.via", { source }) }}</span>
        <button type="button" class="refresh" :disabled="state === 'loading'" @click="refresh">
          {{ t("common.refresh") }}
        </button>
      </div>
    </header>

    <div class="body">
      <nav class="sidebar" :aria-label="t('app.sections')">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          type="button"
          class="nav-item"
          :class="{ 'nav-item--active': active === tab.id }"
          @click="active = tab.id"
        >
          {{ tab.label }}
        </button>
      </nav>

      <main class="content">
        <LoadingState v-if="state === 'loading'" :message="t('app.contactingRuntime')" />
        <ErrorState
          v-else-if="state === 'error'"
          :title="t('app.runtimeUnavailable')"
          :message="appErrorMessage(error)"
          @retry="refresh"
        />
        <template v-else>
          <OverviewView v-if="active === 'overview'" :health="health" :adapters="adapters" />
          <TasksView v-else-if="active === 'tasks'" :tasks="tasks" />
          <ReferencesView v-else-if="active === 'references'" />
          <LogsView v-else-if="active === 'logs'" />
          <SettingsView v-else :health="health" />
        </template>
      </main>
    </div>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 1rem 1.5rem;
  border-bottom: 1px solid var(--dw-border);
  background: var(--dw-surface);
}
.brand {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}
.brand__mark {
  display: grid;
  place-items: center;
  width: 2.25rem;
  height: 2.25rem;
  border-radius: 0.5rem;
  background: var(--dw-accent);
  color: white;
  font-weight: 700;
}
.brand h1 {
  margin: 0;
  font-size: 1.05rem;
}
.brand__tagline {
  margin: 0;
  color: var(--dw-muted);
  font-size: 0.75rem;
}
.topbar__status {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  font-size: 0.8rem;
}
.refresh {
  border: 1px solid var(--dw-border);
  background: transparent;
  color: var(--dw-text);
  padding: 0.35rem 0.9rem;
  border-radius: 0.375rem;
  cursor: pointer;
}
.refresh:disabled {
  opacity: 0.5;
  cursor: progress;
}
.body {
  display: grid;
  grid-template-columns: 200px 1fr;
  flex: 1;
}
.sidebar {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  padding: 1rem;
  border-right: 1px solid var(--dw-border);
}
.nav-item {
  text-align: left;
  border: none;
  background: transparent;
  color: var(--dw-muted);
  padding: 0.5rem 0.75rem;
  border-radius: 0.375rem;
  cursor: pointer;
  font-size: 0.9rem;
}
.nav-item:hover {
  background: var(--dw-surface);
  color: var(--dw-text);
}
.nav-item--active {
  background: var(--dw-surface);
  color: var(--dw-text);
  font-weight: 600;
}
.content {
  padding: 1.5rem;
  overflow: auto;
}
.muted {
  color: var(--dw-muted);
}
</style>
