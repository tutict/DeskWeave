<script setup lang="ts">
import { useI18n } from "vue-i18n";

import { isSupportedLocale, LOCALE_LABELS, setLocale, SUPPORTED_LOCALES } from "../i18n";
import type { RuntimeHealth } from "../lib/runtimeClient";

defineProps<{ health: RuntimeHealth | null }>();

const { t, locale } = useI18n();

/**
 * Persist the choice and apply it.
 *
 * The option values are locale codes (`zh-CN`/`en-US`); what the user reads is the
 * native name, so the picker is usable even when the current UI language is the
 * one being replaced. The value is re-validated here: a DOM value is untrusted
 * input like any other.
 */
function onLanguageChange(event: Event): void {
  const value = (event.target as HTMLSelectElement).value;
  if (isSupportedLocale(value)) setLocale(value);
}
</script>

<template>
  <section class="settings">
    <article class="card">
      <h2>{{ t("settings.languageTitle") }}</h2>
      <p class="muted">{{ t("settings.languageDescription") }}</p>
      <label class="field">
        <span class="field__label">{{ t("settings.languageLabel") }}</span>
        <select data-test="locale-select" :value="locale" @change="onLanguageChange">
          <option v-for="code in SUPPORTED_LOCALES" :key="code" :value="code">
            {{ LOCALE_LABELS[code] }}
          </option>
        </select>
      </label>
    </article>

    <article class="card">
      <h2>{{ t("settings.runtimeTitle") }}</h2>
      <p class="muted">{{ t("settings.runtimeDescription") }}</p>
      <dl v-if="health" class="facts">
        <div>
          <dt>{{ t("settings.version") }}</dt>
          <dd class="mono">{{ health.version }}</dd>
        </div>
        <div>
          <dt>{{ t("settings.schemaVersion") }}</dt>
          <dd class="mono">{{ health.schema_version }}</dd>
        </div>
        <div>
          <dt>{{ t("settings.database") }}</dt>
          <dd class="mono">{{ health.database_path }}</dd>
        </div>
      </dl>
    </article>
  </section>
</template>

<style scoped>
.settings {
  display: grid;
  gap: 1.25rem;
}
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
.field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin-top: 1rem;
  max-width: 16rem;
}
.field__label {
  color: var(--dw-muted);
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
select {
  padding: 0.45rem 0.6rem;
  border: 1px solid var(--dw-border);
  border-radius: 0.375rem;
  background: var(--dw-bg);
  color: var(--dw-text);
  font: inherit;
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
