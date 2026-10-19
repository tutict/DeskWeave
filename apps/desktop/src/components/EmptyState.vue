<script setup lang="ts">
defineProps<{ title: string; description?: string }>();
</script>

<template>
  <div class="state state--empty">
    <h3>{{ title }}</h3>
    <p v-if="description">{{ description }}</p>
    <slot />
  </div>
</template>

<style scoped>
.state {
  padding: 2rem;
  border: 1px dashed var(--dw-border);
  border-radius: 0.5rem;
  text-align: center;
  color: var(--dw-muted);
}
.state h3 {
  margin: 0 0 0.5rem;
  color: var(--dw-text);
}
.state p {
  margin: 0;
}
</style>
