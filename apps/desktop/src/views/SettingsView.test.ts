import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import { LOCALE_STORAGE_KEY, currentLocale, resolveInitialLocale } from "../i18n";
import type { RuntimeHealth } from "../lib/runtimeClient";
import SettingsView from "./SettingsView.vue";

const health: RuntimeHealth = {
  state: "ok",
  version: "0.0.1",
  uptime_seconds: 12.5,
  schema_version: 3,
  database_path: "C:/data/deskweave.db",
  components: [],
  adapters: [],
};

describe("SettingsView language picker", () => {
  it("offers both languages by their native names and preselects the active one", () => {
    const wrapper = mount(SettingsView, { props: { health } });
    const select = wrapper.get("[data-test='locale-select']");

    const options = select.findAll("option").map((option) => option.text());
    expect(options).toEqual(["简体中文", "English"]);
    expect((select.element as HTMLSelectElement).value).toBe("en-US");
  });

  it("switches the interface immediately and remembers the choice", async () => {
    const wrapper = mount(SettingsView, { props: { health } });
    expect(wrapper.text()).toContain("Interface language");

    await wrapper.get("[data-test='locale-select']").setValue("zh-CN");

    // Same mounted component, no reload: the labels are already Chinese.
    expect(wrapper.text()).toContain("界面语言");
    expect(wrapper.text()).not.toContain("Interface language");
    expect(wrapper.text()).toContain("数据库");
    expect(currentLocale()).toBe("zh-CN");
    expect(localStorage.getItem(LOCALE_STORAGE_KEY)).toBe("zh-CN");
  });

  it("switches back to English", async () => {
    const wrapper = mount(SettingsView, { props: { health } });
    await wrapper.get("[data-test='locale-select']").setValue("zh-CN");
    await wrapper.get("[data-test='locale-select']").setValue("en-US");

    expect(wrapper.text()).toContain("Interface language");
    expect(currentLocale()).toBe("en-US");
    expect(localStorage.getItem(LOCALE_STORAGE_KEY)).toBe("en-US");
  });

  it("restores the remembered choice on the next start, ahead of the system language", () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "zh-CN");
    expect(resolveInitialLocale({ storage: localStorage, languages: ["en-US"] })).toBe("zh-CN");
  });

  it("falls back safely when the remembered value is corrupt", () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "klingon");
    expect(resolveInitialLocale({ storage: localStorage, languages: ["zh-CN"] })).toBe("zh-CN");

    localStorage.setItem(LOCALE_STORAGE_KEY, "klingon");
    expect(resolveInitialLocale({ storage: localStorage, languages: ["fr-FR"] })).toBe("en-US");
  });

  it("does not touch business state: the runtime facts are still the ones it was given", async () => {
    const wrapper = mount(SettingsView, { props: { health } });
    await wrapper.get("[data-test='locale-select']").setValue("zh-CN");
    expect(wrapper.text()).toContain("0.0.1");
    expect(wrapper.text()).toContain("C:/data/deskweave.db");
  });
});
