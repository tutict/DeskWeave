<script setup lang="ts">
import { computed } from "vue";

import type { ArtifactView } from "../lib/runtimeClient";

const props = defineProps<{
  artifact: ArtifactView;
  selected: boolean;
  preview?: string;
  previewError?: string;
  loading?: boolean;
  disabled?: boolean;
}>();

defineEmits<{ toggle: [] }>();

const title = computed(() => props.artifact.metadata.title?.trim() || "Untitled candidate");

const dimensions = computed(() => {
  const { width, height } = props.artifact.metadata;
  return width && height ? `${width} × ${height}` : null;
});

const sizeLabel = computed(() => {
  const bytes = props.artifact.size_bytes;
  if (bytes === null || bytes === undefined) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
});

const sourceHost = computed(() => {
  const url = props.artifact.metadata.source_page ?? props.artifact.metadata.original_url;
  if (!url) return null;
  try {
    return new URL(url).host;
  } catch {
    return null;
  }
});

const sourceHref = computed(() => props.artifact.metadata.source_page ?? null);
</script>

<template>
  <button
    type="button"
    class="card"
    :class="{ 'card--selected': selected, 'card--disabled': disabled }"
    role="checkbox"
    :data-test="`card-${artifact.artifact_id}`"
    :aria-checked="selected"
    :aria-label="`${selected ? 'Deselect' : 'Select'} ${title}`"
    :disabled="disabled"
    @click="$emit('toggle')"
  >
    <div class="card__frame">
      <img v-if="preview" :src="preview" :alt="title" loading="lazy" />
      <div v-else-if="previewError" class="card__fallback">
        <span aria-hidden="true">⚠</span>
        <p>{{ previewError }}</p>
      </div>
      <div v-else class="card__fallback card__fallback--loading">
        <span class="spinner" aria-hidden="true"></span>
        <p>Loading preview…</p>
      </div>
    </div>

    <div class="card__meta">
      <p class="card__title" :title="title">{{ title }}</p>
      <p class="card__facts">
        <span v-if="dimensions">{{ dimensions }}</span>
        <span v-if="sizeLabel">{{ sizeLabel }}</span>
        <span v-if="sourceHost">{{ sourceHost }}</span>
      </p>
      <a
        v-if="sourceHref"
        class="card__link"
        :href="sourceHref"
        target="_blank"
        rel="noreferrer noopener"
        @click.stop
        >Open source page</a
      >
    </div>

    <span class="card__check" aria-hidden="true">{{ selected ? "✓" : "" }}</span>
  </button>
</template>

<style scoped>
.card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding: 0.5rem;
  text-align: left;
  border: 2px solid var(--dw-border);
  border-radius: 0.5rem;
  background: var(--dw-surface);
  color: inherit;
  cursor: pointer;
}
.card:hover:not(.card--disabled) {
  border-color: color-mix(in srgb, var(--dw-accent) 45%, var(--dw-border));
}
.card--selected {
  border-color: var(--dw-accent);
  box-shadow: 0 0 0 1px var(--dw-accent) inset;
}
.card--disabled {
  opacity: 0.6;
  cursor: default;
}
.card__frame {
  aspect-ratio: 4 / 3;
  overflow: hidden;
  border-radius: 0.375rem;
  background: var(--dw-bg);
}
.card__frame img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.card__fallback {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.35rem;
  height: 100%;
  padding: 0.5rem;
  color: var(--dw-muted);
  font-size: 0.75rem;
  text-align: center;
}
.card__fallback p {
  margin: 0;
}
.card__fallback--loading .spinner {
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
.card__meta {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}
.card__title {
  margin: 0;
  font-size: 0.85rem;
  font-weight: 600;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.card__facts {
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  color: var(--dw-muted);
  font-size: 0.72rem;
}
.card__link {
  font-size: 0.72rem;
  color: var(--dw-accent);
}
.card__check {
  position: absolute;
  top: 0.7rem;
  right: 0.7rem;
  display: grid;
  place-items: center;
  width: 1.4rem;
  height: 1.4rem;
  border-radius: 50%;
  border: 2px solid var(--dw-border);
  background: var(--dw-surface);
  color: white;
  font-size: 0.8rem;
  font-weight: 700;
}
.card--selected .card__check {
  background: var(--dw-accent);
  border-color: var(--dw-accent);
}
</style>
