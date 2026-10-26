import { beforeEach, describe, expect, it } from "vitest";

import { RuntimeApiError, RuntimeUnavailableError } from "../lib/runtimeClient";
import { appErrorMessage, taskErrorText, toAppError } from "./errors";
import { setLocale } from "./index";

beforeEach(() => {
  setLocale("en-US", { persist: false });
});

describe("toAppError", () => {
  it("classifies a transport failure as unavailable", () => {
    const error = toAppError(new RuntimeUnavailableError("could not reach the runtime"));
    expect(error).toMatchObject({ kind: "unavailable", code: null });
    expect(error.detail).toContain("could not reach the runtime");
  });

  it("keeps the stable code of a runtime rejection", () => {
    const error = toAppError(
      new RuntimeApiError({ code: "permission_denied", message: "not yours" }, 403),
    );
    expect(error).toMatchObject({ kind: "api", code: "permission_denied" });
  });

  it("wraps a plain Error and a non-Error without losing the text", () => {
    expect(toAppError(new Error("boom"))).toMatchObject({ kind: "unknown", detail: "boom" });
    expect(toAppError("a string failure")).toMatchObject({
      kind: "unknown",
      detail: "a string failure",
    });
  });
});

describe("appErrorMessage", () => {
  it("maps a known error code to an English sentence and keeps the detail", () => {
    const message = appErrorMessage(
      toAppError(
        new RuntimeApiError(
          { code: "conflict", message: "task is not awaiting confirmation" },
          409,
        ),
      ),
    );
    expect(message).toContain("The task cannot be changed in its current state.");
    expect(message).toContain("task is not awaiting confirmation");
  });

  it("maps the same code to Chinese, still keeping the detail", () => {
    setLocale("zh-CN", { persist: false });
    const message = appErrorMessage(
      toAppError(
        new RuntimeApiError(
          { code: "conflict", message: "task is not awaiting confirmation" },
          409,
        ),
      ),
    );
    expect(message).toContain("该任务当前状态下无法修改。");
    expect(message).toContain("task is not awaiting confirmation");
    expect(message).not.toContain("The task cannot be changed");
  });

  it("names an unknown code instead of inventing an explanation", () => {
    const message = appErrorMessage(
      toAppError(new RuntimeApiError({ code: "some_new_code", message: "nope" })),
    );
    expect(message).toContain("some_new_code");
    expect(message).toContain("nope");
  });

  it("localises an unavailable runtime in both languages", () => {
    const error = toAppError(new RuntimeUnavailableError("runtime is not running"));
    expect(appErrorMessage(error)).toContain("The local runtime is unavailable.");
    setLocale("zh-CN", { persist: false });
    expect(appErrorMessage(error)).toContain("本地运行时不可用。");
    expect(appErrorMessage(error)).toContain("runtime is not running");
  });

  it("never swallows an unexpected failure", () => {
    const message = appErrorMessage(toAppError(new Error("artifact file is missing")));
    expect(message).toContain("artifact file is missing");
    expect(appErrorMessage(null)).toBe("unknown error");
  });
});

describe("taskErrorText", () => {
  it("explains a task-level code from the runtime", () => {
    expect(taskErrorText("no_candidates", "the runtime said: nothing found")).toBe(
      "No usable candidates were found.",
    );
    setLocale("zh-CN", { persist: false });
    expect(taskErrorText("no_candidates", "whatever")).toBe("没有找到可用的候选图片。");
  });

  it("falls back to the runtime's own message for an unknown code", () => {
    expect(taskErrorText("brand_new_code", "the runtime said: something specific")).toBe(
      "the runtime said: something specific",
    );
  });
});
