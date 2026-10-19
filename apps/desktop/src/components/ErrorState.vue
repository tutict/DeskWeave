<script setup lang="ts">
defineProps<{ title?: string; message: string }>();
defineEmits<{ retry: [] }>();
</script>

<template>
  <div class="state state--error" role="alert">
    <h3>{{ title ?? "Something went wrong" }}</h3>
    <p>{{ message }}</p>
    <button type="button" @click="$emit('retry')">Retry</button>
  </div>
</template>

<style scoped>
.state {
  padding: 1.5rem;
  border: 1px solid var(--dw-danger);
  border-radius: 0.5rem;
  background: color-mix(in srgb, var(--dw-danger) 8%, transparent);
}
.state h3 {
  margin: 0 0 0.5rem;
  color: var(--dw-danger);
}
.state p {
  margin: 0 0 1rem;
  color: var(--dw-muted);
}
button {
  border: 1px solid var(--dw-danger);
  background: transparent;
  color: var(--dw-danger);
  padding: 0.4rem 1rem;
  border-radius: 0.375rem;
  cursor: pointer;
}
button:hover {
  background: color-mix(in srgb, var(--dw-danger) 15%, transparent);
}
</style>
