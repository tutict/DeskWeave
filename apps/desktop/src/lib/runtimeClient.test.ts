import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  confirmSelection,
  fetchHealth,
  openArtifact,
  RuntimeApiError,
  RuntimeUnavailableError,
  startCollection,
} from "./runtimeClient";

function jsonResponse(payload: unknown, status = 200) {
  const text = JSON.stringify(payload);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => text,
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

  it("throws RuntimeUnavailableError when the error body is not structured", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ error: "unauthorized" }, 401)),
    );
    await expect(fetchHealth()).rejects.toBeInstanceOf(RuntimeUnavailableError);
  });

  it("throws RuntimeApiError carrying the server code", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          { error: { code: "conflict", message: "already running", details: { task_id: "t1" } } },
          409,
        ),
      ),
    );

    await expect(confirmSelection("t1", ["art_1"])).rejects.toMatchObject({
      name: "RuntimeApiError",
      code: "conflict",
      status: 409,
    });
  });

  it("posts the collection request as JSON", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ task: { task_id: "t1" }, artifacts: [] }));
    vi.stubGlobal("fetch", fetchMock);

    await startCollection("anime", 3);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url.endsWith("/tasks")).toBe(true);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ query: "anime", limit: 3 });
  });

  it("asks the runtime to open an artifact by id, never by path", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ artifact: { artifact_id: "art_1", action: "open", opened: true } }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await openArtifact("art_1", "open");

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url.endsWith("/artifacts/art_1/open")).toBe(true);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ action: "open" });
    expect(result.opened).toBe(true);
  });

  it("does not leak the token into an error message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({ error: { code: "permission_denied", message: "not yours" } }, 403),
      ),
    );

    const caught = await confirmSelection("t1", ["art_1"]).catch((error: unknown) => error);
    expect(caught).toBeInstanceOf(RuntimeApiError);
    expect((caught as RuntimeApiError).message).not.toContain("Bearer");
  });
});
