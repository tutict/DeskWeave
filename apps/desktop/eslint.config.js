import { defineConfigWithVueTs, vueTsConfigs } from "@vue/eslint-config-typescript";
import pluginVue from "eslint-plugin-vue";

/**
 * Flat ESLint config for the desktop app.
 *
 * Formatting is owned by Prettier (`format:check`), so no stylistic ESLint rules
 * are enabled here — two tools fighting over whitespace produces noise, not value.
 */
export default defineConfigWithVueTs(
  { ignores: ["dist/**", "node_modules/**", "src-tauri/**"] },
  pluginVue.configs["flat/essential"],
  vueTsConfigs.recommended,
);
