<script setup lang="ts">
import { useI18n } from "vue-i18n";

import EmptyState from "../components/EmptyState.vue";
import StatusBadge from "../components/StatusBadge.vue";
import type { TaskRecord } from "../lib/runtimeClient";

defineProps<{ tasks: TaskRecord[] }>();

const { t } = useI18n();
</script>

<template>
  <section>
    <EmptyState
      v-if="tasks.length === 0"
      :title="t('tasks.emptyTitle')"
      :description="t('tasks.emptyDescription')"
    />
    <table v-else class="tasks">
      <thead>
        <tr>
          <th>{{ t("tasks.columnTask") }}</th>
          <th>{{ t("tasks.columnStatus") }}</th>
          <th>{{ t("tasks.columnRun") }}</th>
          <th>{{ t("tasks.columnUpdated") }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="task in tasks" :key="task.task_id">
          <td>
            <div>{{ task.name }}</div>
            <span class="mono muted">{{ task.task_id }}</span>
          </td>
          <td><StatusBadge :status="task.status" /></td>
          <td class="mono muted">{{ task.run_id }}</td>
          <td class="muted">{{ task.updated_at }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<style scoped>
.tasks {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}
.tasks th {
  text-align: left;
  padding: 0.5rem;
  border-bottom: 1px solid var(--dw-border);
  color: var(--dw-muted);
  font-weight: 600;
}
.tasks td {
  padding: 0.6rem 0.5rem;
  border-bottom: 1px solid var(--dw-border);
  vertical-align: top;
}
.mono {
  font-family: var(--dw-mono);
  font-size: 0.75rem;
}
.muted {
  color: var(--dw-muted);
}
</style>
