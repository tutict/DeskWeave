import { describe, expect, it } from "vitest";

import {
  DEFAULT_LOCALE,
  LOCALE_STORAGE_KEY,
  SUPPORTED_LOCALES,
  detectLocale,
  isSupportedLocale,
  readStoredLocale,
  resolveInitialLocale,
  writeStoredLocale,
  type StorageLike,
} from "./locale";

/** Minimal in-memory `Storage`, so these rules are tested without a DOM. */
function memoryStorage(initial?: string): StorageLike & { map: Map<string, string> } {
  const map = new Map<string, string>();
  if (initial !== undefined) map.set(LOCALE_STORAGE_KEY, initial);
  return {
    map,
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => {
      map.set(key, value);
    },
  };
}

function brokenStorage(): StorageLike {
  return {
    getItem: () => {
      throw new Error("storage is not available");
    },
    setItem: () => {
      throw new Error("quota exceeded");
    },
  };
}

describe("supported locales", () => {
  it("exposes exactly the two shipped locales and falls back to a supported one", () => {
    expect([...SUPPORTED_LOCALES]).toEqual(["zh-CN", "en-US"]);
    expect(SUPPORTED_LOCALES).toContain(DEFAULT_LOCALE);
    expect(isSupportedLocale("zh-CN")).toBe(true);
    expect(isSupportedLocale("en-US")).toBe(true);
  });

  it("rejects anything that is not a shipped locale", () => {
    for (const value of ["zh", "zh-cn", "fr-FR", "", null, undefined, 42, {}]) {
      expect(isSupportedLocale(value)).toBe(false);
    }
  });
});

describe("detectLocale", () => {
  it("maps Chinese tags to zh-CN", () => {
    expect(detectLocale(["zh-CN"])).toBe("zh-CN");
    expect(detectLocale(["zh"])).toBe("zh-CN");
    expect(detectLocale(["zh-Hans-CN"])).toBe("zh-CN");
    expect(detectLocale(["zh_TW"])).toBe("zh-CN");
  });

  it("maps English tags to en-US", () => {
    expect(detectLocale(["en"])).toBe("en-US");
    expect(detectLocale(["en-GB"])).toBe("en-US");
    expect(detectLocale(["en-US-x-private"])).toBe("en-US");
  });

  it("skips unsupported languages and keeps looking", () => {
    expect(detectLocale(["fr-FR", "de-DE", "zh-CN"])).toBe("zh-CN");
    expect(detectLocale(["fr-FR", "en-US"])).toBe("en-US");
  });

  it("falls back to en-US when nothing is supported or the list is empty", () => {
    expect(detectLocale(["fr-FR", "de-DE"])).toBe("en-US");
    expect(detectLocale([])).toBe("en-US");
    expect(detectLocale(null)).toBe("en-US");
    expect(detectLocale(undefined)).toBe("en-US");
  });
});

describe("persisted choice", () => {
  it("reads a valid stored locale", () => {
    expect(readStoredLocale(memoryStorage("zh-CN"))).toBe("zh-CN");
  });

  it("treats a corrupt or unsupported stored value as absent", () => {
    expect(readStoredLocale(memoryStorage("klingon"))).toBeNull();
    expect(readStoredLocale(memoryStorage(""))).toBeNull();
    expect(readStoredLocale(memoryStorage("{% broken json %}"))).toBeNull();
  });

  it("survives a storage that throws instead of failing startup", () => {
    expect(readStoredLocale(brokenStorage())).toBeNull();
    expect(() => writeStoredLocale(brokenStorage(), "zh-CN")).not.toThrow();
    expect(readStoredLocale(null)).toBeNull();
    expect(() => writeStoredLocale(null, "zh-CN")).not.toThrow();
  });

  it("writes the choice under the documented key", () => {
    const storage = memoryStorage();
    writeStoredLocale(storage, "zh-CN");
    expect(storage.map.get(LOCALE_STORAGE_KEY)).toBe("zh-CN");
  });
});

describe("resolveInitialLocale", () => {
  it("prefers the user's stored choice over the system language", () => {
    expect(resolveInitialLocale({ storage: memoryStorage("zh-CN"), languages: ["en-US"] })).toBe(
      "zh-CN",
    );
    expect(resolveInitialLocale({ storage: memoryStorage("en-US"), languages: ["zh-CN"] })).toBe(
      "en-US",
    );
  });

  it("uses the system language when nothing valid is stored", () => {
    expect(resolveInitialLocale({ storage: memoryStorage("klingon"), languages: ["zh-CN"] })).toBe(
      "zh-CN",
    );
    expect(resolveInitialLocale({ languages: ["ja-JP"] })).toBe("en-US");
  });

  it("defaults to en-US with no storage and no languages", () => {
    expect(resolveInitialLocale()).toBe("en-US");
  });
});
