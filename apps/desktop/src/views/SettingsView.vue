<script setup lang="ts">
import type { RuntimeHealth } from "../lib/runtimeClient";

defineProps<{ health: RuntimeHealth | null }>();
</script>

<template>
  <section class="settings">
    <article class="card">
      <h2>Runtime</h2>
      <p class="muted">
        These values are reported by the local runtime. Editing settings is not implemented in Phase
        0.
      </p>
      <dl v-if="health" class="facts">
        <div>
          <dt>Version</dt>
          <dd class="mono">{{ health.version }}</dd>
        </div>
        <div>
          <dt>Schema version</dt>
          <dd class="mono">{{ health.schema_version }}</dd>
        </div>
        <div>
          <dt>Database</dt>
          <dd class="mono">{{ health.database_path }}</dd>
        </div>
      </dl>
    </article>
  </section>
</template>

<style scoped>
.card {
  border: 1px solid var(--dw-border);
  border-radius: 0.75rem;
  padding: 1.25rem;
  background: var(--dw-surface);
}
.card h2 {
  margin: 0 0 0.5rem;
  font-size: 1rem;
}
.facts {
  display: grid;
  gap: 0.75rem;
  margin: 1rem 0 0;
}
.facts dt {
  color: var(--dw-muted);
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.facts dd {
  margin: 0.2rem 0 0;
}
.mono {
  font-family: var(--dw-mono);
  font-size: 0.8rem;
}
.muted {
  color: var(--dw-muted);
  font-size: 0.85rem;
}
</style>
