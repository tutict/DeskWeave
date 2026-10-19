import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchHealth, RuntimeUnavailableError } from "./runtimeClient";

function jsonResponse(payload: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  };
}

describe("runtimeClient", () => {
  beforeEach(() => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns the parsed health payload on success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ state: "ok", schema_version: 1 })),
    );
    const health = await fetchHealth();
    expect(health.state).toBe("ok");
    expect(health.schema_version).toBe(1);
  });

  it("throws RuntimeUnavailableError when the runtime is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("ECONNREFUSED");
      }),
    );
    await expect(fetchHealth()).rejects.toBeInstanceOf(RuntimeUnavailableError);
  });

  it("throws RuntimeUnavailableError on a non-2xx response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ error: "unauthorized" }, 401)),
    );
    await expect(fetchHealth()).rejects.toBeInstanceOf(RuntimeUnavailableError);
  });
});
