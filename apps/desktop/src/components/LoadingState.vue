<script setup lang="ts">
defineProps<{ message?: string }>();
</script>

<template>
  <div class="state state--loading" role="status" aria-live="polite">
    <span class="spinner" aria-hidden="true"></span>
    <p>{{ message ?? "Loading…" }}</p>
  </div>
</template>

<style scoped>
.state {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 1.5rem;
  color: var(--dw-muted);
}
.spinner {
  width: 1rem;
  height: 1rem;
  border: 2px solid var(--dw-border);
  border-top-color: var(--dw-accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
