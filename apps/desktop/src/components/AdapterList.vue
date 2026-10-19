<script setup lang="ts">
import EmptyState from "./EmptyState.vue";
import StatusBadge from "./StatusBadge.vue";
import type { AdapterDescriptor } from "../lib/runtimeClient";

defineProps<{ adapters: AdapterDescriptor[] }>();
</script>

<template>
  <EmptyState
    v-if="adapters.length === 0"
    title="No adapters registered"
    description="Adapters appear here once the runtime registers them. Nothing is connected yet."
  />
  <ul v-else class="adapters">
    <li v-for="adapter in adapters" :key="adapter.adapter_id" class="adapter">
      <div class="adapter__head">
        <strong>{{ adapter.name }}</strong>
        <span class="mono muted">{{ adapter.adapter_id }} · v{{ adapter.version }}</span>
      </div>
      <p class="adapter__desc">{{ adapter.description }}</p>
      <ul class="capabilities">
        <li v-for="capability in adapter.capabilities" :key="capability.name">
          <StatusBadge :status="capability.risk" />
          <span class="mono">{{ capability.name }}</span>
          <span class="muted">{{ capability.description }}</span>
        </li>
      </ul>
    </li>
  </ul>
</template>

<style scoped>
.adapters {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 1rem;
}
.adapter {
  border: 1px solid var(--dw-border);
  border-radius: 0.5rem;
  padding: 0.9rem;
}
.adapter__head {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
}
.adapter__desc {
  margin: 0.4rem 0 0.6rem;
  color: var(--dw-muted);
  font-size: 0.85rem;
}
.capabilities {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.35rem;
  font-size: 0.8rem;
}
.capabilities li {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.mono {
  font-family: var(--dw-mono);
}
.muted {
  color: var(--dw-muted);
}
</style>
