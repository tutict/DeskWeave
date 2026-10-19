<script setup lang="ts">
import AdapterList from "../components/AdapterList.vue";
import type { AdapterDescriptor, RuntimeHealth } from "../lib/runtimeClient";

defineProps<{ health: RuntimeHealth | null; adapters: AdapterDescriptor[] }>();
</script>

<template>
  <section class="overview">
    <article v-if="health" class="card">
      <header class="card__header">
        <h2>Runtime health</h2>
        <span class="card__meta">v{{ health.version }} · schema {{ health.schema_version }}</span>
      </header>
      <dl class="facts">
        <div>
          <dt>Database</dt>
          <dd class="mono">{{ health.database_path }}</dd>
        </div>
        <div>
          <dt>Uptime</dt>
          <dd>{{ health.uptime_seconds.toFixed(1) }}s</dd>
        </div>
      </dl>
      <ul class="components">
        <li v-for="component in health.components" :key="component.name">
          <span>{{ component.name }}</span>
          <span class="mono muted">{{ component.detail }}</span>
        </li>
      </ul>
    </article>

    <article class="card">
      <header class="card__header">
        <h2>Adapters</h2>
        <span class="card__meta">{{ adapters.length }} registered</span>
      </header>
      <AdapterList :adapters="adapters" />
    </article>
  </section>
</template>

<style scoped>
.overview {
  display: grid;
  gap: 1.25rem;
}
.card {
  border: 1px solid var(--dw-border);
  border-radius: 0.75rem;
  padding: 1.25rem;
  background: var(--dw-surface);
}
.card__header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 1rem;
}
.card__header h2 {
  margin: 0;
  font-size: 1rem;
}
.card__meta {
  color: var(--dw-muted);
  font-size: 0.8rem;
}
.facts {
  display: flex;
  gap: 2rem;
  margin: 0 0 1rem;
}
.facts dt {
  color: var(--dw-muted);
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.facts dd {
  margin: 0.25rem 0 0;
}
.components {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.5rem;
}
.components li {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  font-size: 0.85rem;
}
.mono {
  font-family: var(--dw-mono);
  font-size: 0.8rem;
}
.muted {
  color: var(--dw-muted);
}
</style>
