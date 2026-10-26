<script setup lang="ts">
import { useI18n } from "vue-i18n";

import EmptyState from "../components/EmptyState.vue";

const { t } = useI18n();
</script>

<template>
  <section>
    <EmptyState :title="t('logs.title')" :description="t('logs.description')" />
  </section>
</template>
