<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{ status: string }>();

const tone = computed(() => {
  switch (props.status) {
    case "ok":
    case "connected":
    case "succeeded":
      return "ok";
    case "degraded":
    case "connecting":
    case "running":
    case "pending":
    case "waiting_for_confirmation":
      return "warn";
    case "error":
    case "failed":
      return "error";
    default:
      return "neutral";
  }
});
</script>

<template>
  <span class="badge" :class="`badge--${tone}`">{{ status }}</span>
</template>

<style scoped>
.badge {
  display: inline-block;
  padding: 0.15rem 0.5rem;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: lowercase;
  border: 1px solid transparent;
}
.badge--ok {
  color: var(--dw-ok);
  border-color: var(--dw-ok);
}
.badge--warn {
  color: var(--dw-warn);
  border-color: var(--dw-warn);
}
.badge--error {
  color: var(--dw-danger);
  border-color: var(--dw-danger);
}
.badge--neutral {
  color: var(--dw-muted);
  border-color: var(--dw-border);
}
</style>
