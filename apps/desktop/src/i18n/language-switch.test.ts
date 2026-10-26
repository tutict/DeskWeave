import { flushPromises, mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

vi.mock("../lib/runtimeClient", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/runtimeClient")>();
  return {
    ...actual,
    connectionSource: () => "http",
    fetchHealth: vi.fn(async () => ({
      state: "ok",
      version: "0.0.1",
      uptime_seconds: 12.5,
      schema_version: 3,
      database_path: "C:/data/deskweave.db",
      components: [],
      adapters: [],
    })),
    fetchAdapters: vi.fn(async () => ({ adapters: [] })),
    fetchTasks: vi.fn(async () => ({ tasks: [] })),
    fetchTaskViews: vi.fn(),
    startCollection: vi.fn(),
    confirmSelection: vi.fn(),
    cancelTask: vi.fn(),
    openArtifact: vi.fn(),
    fetchArtifactPreview: vi.fn(async () => "data:image/png;base64,AAAA"),
  };
});

import { confirmSelection, fetchTaskViews, RuntimeApiError } from "../lib/runtimeClient";
import { setLocale } from "./index";
import App from "../App.vue";
import type { TaskView } from "../lib/runtimeClient";
import ReferencesView from "../views/ReferencesView.vue";

const fetchTaskViewsMock = fetchTaskViews as Mock;
const confirmSelectionMock = confirmSelection as Mock;

function waitingTask(): TaskView {
  return {
    task: {
      task_id: "task_1",
      run_id: "run_1",
      name: "Collect reference images: anime",
      status: "waiting_for_confirmation",
      created_at: "2026-10-20T10:00:00+00:00",
      updated_at: "2026-10-20T10:00:05+00:00",
      result: { query: "anime character", source: "bing", candidates: ["art_1"], failures: [] },
    },
    artifacts: [
      {
        artifact_id: "art_1",
        kind: "image",
        media_type: "image/png",
        size_bytes: 2048,
        metadata: {
          title: "Candidate art_1",
          source: "bing",
          source_page: "https://example.com/art_1",
          original_url: "https://cdn.example.com/art_1.png",
          sha256: "a".repeat(64),
          width: 640,
          height: 480,
        },
        created_at: "2026-10-20T10:00:00+00:00",
      },
    ],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  confirmSelectionMock.mockResolvedValue(waitingTask());
});

describe("switching language while the app is running", () => {
  it("relabels the shell in place, without a reload or a remount", async () => {
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.text()).toContain("Overview");
    expect(wrapper.text()).toContain("Local-first agent desktop infrastructure");
    expect(wrapper.findAll(".nav-item")).toHaveLength(5);
    expect(document.documentElement.lang).toBe("en-US");

    setLocale("zh-CN", { persist: false });
    await nextTick();

    expect(wrapper.text()).toContain("概览");
    expect(wrapper.text()).toContain("本地优先的智能体桌面基础设施");
    expect(wrapper.text()).not.toContain("Overview");
    expect(wrapper.findAll(".nav-item")).toHaveLength(5);
    expect(document.documentElement.lang).toBe("zh-CN");
  });

  it("translates the reference screen and keeps every test hook and the confirmation gate", async () => {
    fetchTaskViewsMock.mockResolvedValue({ tasks: [waitingTask()] });
    setLocale("zh-CN", { persist: false });

    const wrapper = mount(ReferencesView);
    await flushPromises();

    expect(wrapper.text()).toContain("参考图采集");
    expect(wrapper.text()).toContain("确认并继续");
    expect(wrapper.get("[data-test='card-art_1']").attributes("aria-label")).toBe(
      "选择 Candidate art_1",
    );
    // The state badge shows a Chinese label but keeps the protocol value.
    expect(wrapper.get("[data-status='waiting_for_confirmation']").text()).toBe("等待确认");

    // Business rules do not move with the language: nothing is selected, so the
    // only path to Blender is still closed.
    expect(wrapper.get("[data-test='confirm']").attributes("disabled")).toBeDefined();
    expect(confirmSelectionMock).not.toHaveBeenCalled();
  });

  it("re-renders a visible failure in the other language", async () => {
    fetchTaskViewsMock.mockResolvedValue({ tasks: [waitingTask()] });
    setLocale("zh-CN", { persist: false });

    const wrapper = mount(ReferencesView);
    await flushPromises();

    confirmSelectionMock.mockRejectedValue(
      new RuntimeApiError({ code: "conflict", message: "task is not awaiting confirmation" }, 409),
    );
    await wrapper.get("[data-test='card-art_1']").trigger("click");
    await wrapper.get("[data-test='confirm']").trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("该任务当前状态下无法修改。");

    setLocale("en-US", { persist: false });
    await nextTick();

    expect(wrapper.text()).toContain("The task cannot be changed in its current state.");
    expect(wrapper.text()).toContain("task is not awaiting confirmation");
  });
});
