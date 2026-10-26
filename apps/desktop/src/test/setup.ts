/**
 * Global test setup.
 *
 * Every mounted component resolves `t()` from the shared i18n instance, exactly
 * like the real app does through `app.use(i18n)`. Tests that care about a specific
 * language set it themselves; the default is pinned to `en-US` so assertions on
 * English copy stay meaningful and do not depend on the host machine's locale.
 */
import { config } from "@vue/test-utils";
import { beforeEach } from "vitest";

import { i18n, setLocale } from "../i18n";

config.global.plugins = [i18n];

beforeEach(() => {
  setLocale("en-US", { persist: false });
  localStorage.clear();
});
