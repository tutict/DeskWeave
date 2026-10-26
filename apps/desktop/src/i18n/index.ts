import { createI18n } from "vue-i18n";

import {
  applyDocumentLanguage,
  browserLanguages,
  browserStorage,
  DEFAULT_LOCALE,
  isSupportedLocale,
  resolveInitialLocale,
  writeStoredLocale,
  type SupportedLocale,
} from "./locale";
import enUS, { type MessageSchema } from "./locales/en-US";
import zhCN from "./locales/zh-CN";

export type { MessageSchema };
export {
  DEFAULT_LOCALE,
  LOCALE_LABELS,
  LOCALE_STORAGE_KEY,
  SUPPORTED_LOCALES,
  detectLocale,
  isSupportedLocale,
  readStoredLocale,
  resolveInitialLocale,
  writeStoredLocale,
} from "./locale";
export type { StorageLike, SupportedLocale } from "./locale";

/**
 * The single i18n instance for the whole application.
 *
 * `legacy: false` gives the Composition API (`useI18n()`); `fallbackLocale` is
 * `en-US` so a key that exists in English but not (yet) in Chinese still renders
 * English text instead of a raw key.
 *
 * Nothing here needs `eval`: vue-i18n 11 compiles messages to an AST and
 * interprets it, so the strict CSP in `src-tauri/tauri.conf.json` stays intact.
 */
export const i18n = createI18n({
  legacy: false,
  locale: resolveInitialLocale({ languages: browserLanguages(), storage: browserStorage() }),
  fallbackLocale: DEFAULT_LOCALE,
  messages: { "en-US": enUS, "zh-CN": zhCN },
  numberFormats: {
    "en-US": {
      integer: { style: "decimal", maximumFractionDigits: 0 },
      decimal1: { style: "decimal", minimumFractionDigits: 1, maximumFractionDigits: 1 },
    },
    "zh-CN": {
      integer: { style: "decimal", maximumFractionDigits: 0 },
      decimal1: { style: "decimal", minimumFractionDigits: 1, maximumFractionDigits: 1 },
    },
  },
  datetimeFormats: {
    "en-US": {
      short: {
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: true,
      },
    },
    "zh-CN": {
      short: {
        year: "numeric",
        month: "long",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      },
    },
  },
  // A missing key already falls back to en-US; keep the console clean in tests
  // and in the shipped webview.
  missingWarn: false,
  fallbackWarn: false,
});

type UntypedTranslate = (key: string, named?: Record<string, unknown>) => string;

/**
 * Untyped entry point for *dynamic* keys.
 *
 * Components translate through `useI18n().t`, which is checked at compile time.
 * Helpers that build keys from runtime values (error codes, task states) cannot
 * be checked statically; they go through here and always check `te()` first.
 */
export const translateMessage = i18n.global.t as unknown as UntypedTranslate;

/** The locale currently in effect, narrowed to a supported value. */
export function currentLocale(): SupportedLocale {
  const value: unknown = i18n.global.locale.value;
  return isSupportedLocale(value) ? value : DEFAULT_LOCALE;
}

/**
 * Switch locale and (by default) remember the choice.
 *
 * Takes effect immediately: the locale is reactive, so every rendered `t()`
 * re-evaluates without a reload. Pass `{ persist: false }` in tests.
 */
export function setLocale(locale: SupportedLocale, options: { persist?: boolean } = {}): void {
  if (!isSupportedLocale(locale)) return;
  i18n.global.locale.value = locale;
  applyDocumentLanguage(locale);
  if (options.persist !== false) writeStoredLocale(browserStorage(), locale);
}

/**
 * Label for a runtime vocabulary value (task state, health state, risk level).
 *
 * Unknown values are returned verbatim: a state added to the runtime must show
 * up as itself, never as a missing-key string.
 */
export function statusLabel(status: string): string {
  const key = `status.${status}`;
  return i18n.global.te(key) ? translateMessage(key) : status;
}

/**
 * Format a user-facing *event* time in the active locale.
 *
 * Record timestamps (`created_at`, `updated_at`) are deliberately left as ISO
 * 8601 in the UI: they are cross-referenced with logs and the database, where a
 * localised "Oct 20, 10:01" would be less precise, not more. Event times the user
 * acted on (a confirmation) are rendered for humans.
 */
export function formatEventTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return i18n.global.d(date, "short") as string;
}

applyDocumentLanguage(currentLocale());
