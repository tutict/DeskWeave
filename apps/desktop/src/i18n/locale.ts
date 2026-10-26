/**
 * Locale detection and persistence.
 *
 * Everything here is a pure function over plain inputs (`navigator.languages`,
 * a `Storage`-like object) so the rules can be tested without a DOM. The module
 * never throws: a browser that denies `localStorage` (private mode, disabled
 * storage, a Tauri webview with a locked-down origin) degrades to detection
 * only, and a corrupt stored value degrades to detection as well.
 */

export const SUPPORTED_LOCALES = ["zh-CN", "en-US"] as const;

export type SupportedLocale = (typeof SUPPORTED_LOCALES)[number];

/** The fallback locale required by the product spec. */
export const DEFAULT_LOCALE: SupportedLocale = "en-US";

/** Where the user's explicit choice is remembered. */
export const LOCALE_STORAGE_KEY = "deskweave.locale";

/** Native names, shown in the language picker exactly as written here. */
export const LOCALE_LABELS: Record<SupportedLocale, string> = {
  "zh-CN": "简体中文",
  "en-US": "English",
};

/** The subset of `Storage` this module needs; keeps the tests DOM-free. */
export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export function isSupportedLocale(value: unknown): value is SupportedLocale {
  return typeof value === "string" && (SUPPORTED_LOCALES as readonly string[]).includes(value);
}

/**
 * Map one BCP 47 tag onto a supported locale.
 *
 * `zh`, `zh-CN`, `zh-Hans`, `zh-Hans-CN`, `zh-TW`… all resolve to `zh-CN` (the
 * only Chinese catalog); `en`, `en-GB`, `en-US-x-foo` resolve to `en-US`.
 * Anything else returns `null` so the caller can keep looking down the list.
 */
function matchLocale(tag: string): SupportedLocale | null {
  const normalised = tag.trim().toLowerCase().replace(/_/g, "-");
  if (!normalised) return null;
  const language = normalised.split("-")[0];
  if (language === "zh") return "zh-CN";
  if (language === "en") return "en-US";
  return null;
}

/**
 * Pick the first supported locale from a `navigator.languages` list.
 *
 * An empty, missing or exhausted list falls back to `DEFAULT_LOCALE`.
 */
export function detectLocale(languages: readonly string[] | null | undefined): SupportedLocale {
  if (!languages) return DEFAULT_LOCALE;
  for (const tag of languages) {
    if (typeof tag !== "string") continue;
    const matched = matchLocale(tag);
    if (matched) return matched;
  }
  return DEFAULT_LOCALE;
}

/** Read the persisted choice; a corrupt or unsupported value reads as `null`. */
export function readStoredLocale(storage: StorageLike | null | undefined): SupportedLocale | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(LOCALE_STORAGE_KEY);
    return isSupportedLocale(raw) ? raw : null;
  } catch {
    // A storage that cannot be read must not break startup.
    return null;
  }
}

/** Persist the choice. Failures are swallowed: the UI keeps the new locale anyway. */
export function writeStoredLocale(
  storage: StorageLike | null | undefined,
  locale: SupportedLocale,
): void {
  if (!storage) return;
  try {
    storage.setItem(LOCALE_STORAGE_KEY, locale);
  } catch {
    // Quota exceeded / storage disabled: the in-memory choice still applies.
  }
}

/**
 * Resolve the locale to start with.
 *
 * Order of precedence: an explicit, valid stored choice → the browser's language
 * list → `en-US`. The user's choice always wins over the system language.
 */
export function resolveInitialLocale(
  options: {
    languages?: readonly string[] | null;
    storage?: StorageLike | null;
  } = {},
): SupportedLocale {
  const stored = readStoredLocale(options.storage);
  if (stored) return stored;
  return detectLocale(options.languages);
}

/** `localStorage` when the platform allows it, otherwise `null`. Never throws. */
export function browserStorage(): StorageLike | null {
  try {
    const storage = (globalThis as { localStorage?: StorageLike }).localStorage;
    return storage ?? null;
  } catch {
    return null;
  }
}

/** `navigator.languages`, falling back to `navigator.language`. Never throws. */
export function browserLanguages(): readonly string[] {
  try {
    const navigator = (
      globalThis as { navigator?: { languages?: readonly string[]; language?: string } }
    ).navigator;
    if (!navigator) return [];
    if (Array.isArray(navigator.languages) && navigator.languages.length > 0) {
      return navigator.languages;
    }
    return navigator.language ? [navigator.language] : [];
  } catch {
    return [];
  }
}

/**
 * Keep `<html lang>` in sync so screen readers and the OS use the right voice
 * and hyphenation rules. Best-effort: a missing document is not an error.
 */
export function applyDocumentLanguage(locale: SupportedLocale): void {
  try {
    const document = (globalThis as { document?: { documentElement?: { lang: string } } }).document;
    if (document?.documentElement) document.documentElement.lang = locale;
  } catch {
    // No DOM (unit tests, SSR-like environments): nothing to do.
  }
}
