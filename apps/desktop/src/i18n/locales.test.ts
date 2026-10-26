import { beforeEach, describe, expect, it } from "vitest";

import { currentLocale, i18n, setLocale, statusLabel, translateMessage } from "./index";
import enUS from "./locales/en-US";
import zhCN from "./locales/zh-CN";

type UntypedTranslate = (key: string, named?: Record<string, unknown>, plural?: number) => string;

const t = i18n.global.t as unknown as UntypedTranslate;
const n = i18n.global.n as unknown as (value: number, format: string) => string;
const d = i18n.global.d as unknown as (value: Date, format: string) => string;

/** Every leaf key, flattened, so the two catalogs can be compared exactly. */
function flatten(value: unknown, prefix = ""): string[] {
  if (typeof value === "string") return prefix ? [prefix] : [];
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>).flatMap(([key, child]) =>
      flatten(child, prefix ? `${prefix}.${key}` : key),
    );
  }
  return [];
}

beforeEach(() => {
  setLocale("en-US", { persist: false });
});

describe("catalog parity", () => {
  it("declares en-US as the fallback locale", () => {
    expect(i18n.global.fallbackLocale.value).toBe("en-US");
  });

  it("has exactly the same keys in zh-CN and en-US", () => {
    const english = flatten(enUS).sort();
    const chinese = flatten(zhCN).sort();

    expect(english.length).toBeGreaterThan(100);
    expect(chinese.filter((key) => !english.includes(key))).toEqual([]);
    expect(english.filter((key) => !chinese.includes(key))).toEqual([]);
  });

  it("never ships an empty Chinese string", () => {
    const empties = flatten(zhCN).filter((key) => {
      const value = key.split(".").reduce<unknown>((node, part) => {
        return node && typeof node === "object"
          ? (node as Record<string, unknown>)[part]
          : undefined;
      }, zhCN);
      return typeof value === "string" && value.trim() === "";
    });
    expect(empties).toEqual([]);
  });
});

describe("translation", () => {
  it("switches every subsequent lookup immediately", () => {
    expect(t("app.tabs.references")).toBe("References");
    setLocale("zh-CN", { persist: false });
    expect(currentLocale()).toBe("zh-CN");
    expect(t("app.tabs.references")).toBe("参考图");
  });

  it("keeps interpolated values verbatim", () => {
    const timestamp = "2026-10-20T10:00:05+00:00";
    expect(t("references.updatedAt", { timestamp })).toBe(`updated ${timestamp}`);
    setLocale("zh-CN", { persist: false });
    expect(t("references.updatedAt", { timestamp })).toContain(timestamp);
  });
});

describe("plurals", () => {
  it("uses the English one/other forms", () => {
    expect(t("references.candidateCount", { count: 1 }, 1)).toBe("1 candidate");
    expect(t("references.candidateCount", { count: 2 }, 2)).toBe("2 candidates");
    expect(t("references.failuresHead", { count: 1 }, 1)).toBe("1 candidate could not be saved");
    expect(t("references.objectCount", { count: 2 }, 2)).toBe("2 objects:");
  });

  it("uses the single Chinese form for every count", () => {
    setLocale("zh-CN", { persist: false });
    expect(t("references.candidateCount", { count: 1 }, 1)).toBe("1 个候选");
    expect(t("references.candidateCount", { count: 5 }, 5)).toBe("5 个候选");
  });
});

describe("number and date formats", () => {
  it("formats numbers per locale", () => {
    expect(n(4, "decimal1")).toBe("4.0");
    expect(n(1234.5, "decimal1")).toBe("1,234.5");
    setLocale("zh-CN", { persist: false });
    expect(n(4, "decimal1")).toBe("4.0");
  });

  it("formats dates per locale", () => {
    const date = new Date("2026-10-20T10:01:00Z");
    const english = d(date, "short");
    setLocale("zh-CN", { persist: false });
    const chinese = d(date, "short");

    expect(english).toContain("2026");
    expect(chinese).toContain("2026");
    expect(chinese).not.toBe(english);
  });
});

describe("safe fallbacks", () => {
  it("renders an unknown status verbatim instead of a missing-key path", () => {
    expect(statusLabel("waiting_for_confirmation")).toBe("waiting_for_confirmation");
    expect(statusLabel("a_state_the_runtime_added_later")).toBe("a_state_the_runtime_added_later");
  });

  it("localises a known status without touching the protocol value", () => {
    setLocale("zh-CN", { persist: false });
    expect(statusLabel("waiting_for_confirmation")).toBe("等待确认");
    expect(statusLabel("cancelled")).toBe("已取消");
  });

  it("falls back to the key itself for a key that does not exist anywhere", () => {
    expect(translateMessage("references.doesNotExist")).toBe("references.doesNotExist");
  });
});
