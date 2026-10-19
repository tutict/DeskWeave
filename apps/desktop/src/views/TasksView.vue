<script setup lang="ts">
import EmptyState from "../components/EmptyState.vue";
import StatusBadge from "../components/StatusBadge.vue";
import type { TaskRecord } from "../lib/runtimeClient";

defineProps<{ tasks: TaskRecord[] }>();
</script>

<template>
  <section>
    <EmptyState
      v-if="tasks.length === 0"
      title="No workflow tasks yet"
      description="Tasks created by a workflow run will be listed here. Phase 0 does not run workflows yet."
    />
    <table v-else class="tasks">
      <thead>
        <tr>
          <th>Task</th>
          <th>Status</th>
          <th>Run</th>
          <th>Updated</th>
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
