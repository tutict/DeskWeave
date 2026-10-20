import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import type { TaskView } from "../lib/runtimeClient";

vi.mock("../lib/runtimeClient", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/runtimeClient")>();
  return {
    ...actual,
    fetchTaskViews: vi.fn(),
    startCollection: vi.fn(),
    confirmSelection: vi.fn(),
    cancelTask: vi.fn(),
    openArtifact: vi.fn(),
    fetchArtifactPreview: vi.fn(async () => "data:image/png;base64,AAAA"),
  };
});

import {
  cancelTask,
  confirmSelection,
  fetchArtifactPreview,
  fetchTaskViews,
  openArtifact,
  RuntimeApiError,
  RuntimeUnavailableError,
  startCollection,
} from "../lib/runtimeClient";
import ReferencesView from "./ReferencesView.vue";

const fetchTaskViewsMock = fetchTaskViews as Mock;
const startCollectionMock = startCollection as Mock;
const confirmSelectionMock = confirmSelection as Mock;
const cancelTaskMock = cancelTask as Mock;
const previewMock = fetchArtifactPreview as Mock;
const openArtifactMock = openArtifact as Mock;

function artifact(id: string, title = `Candidate ${id}`) {
  return {
    artifact_id: id,
    kind: "image",
    media_type: "image/png",
    size_bytes: 2048,
    metadata: {
      title,
      source: "bing",
      source_page: `https://example.com/${id}`,
      original_url: `https://cdn.example.com/${id}.png`,
      sha256: "a".repeat(64),
      width: 640,
      height: 480,
    },
    created_at: "2026-10-20T10:00:00+00:00",
  };
}

function waitingTask(ids = ["art_1", "art_2"]): TaskView {
  return {
    task: {
      task_id: "task_1",
      run_id: "run_1",
      name: "Collect reference images: anime",
      status: "waiting_for_confirmation",
      created_at: "2026-10-20T10:00:00+00:00",
      updated_at: "2026-10-20T10:00:05+00:00",
      result: {
        query: "anime character",
        source: "bing",
        candidates: ids,
        failures: [{ url: "https://cdn.example.com/bad.png", reason: "timed out" }],
      },
    },
    artifacts: ids.map((id) => artifact(id)),
  };
}

function blendArtifact() {
  return {
    artifact_id: "art_blend",
    kind: "model",
    media_type: "application/x-blender",
    size_bytes: 4096,
    metadata: {},
    created_at: "2026-10-20T10:01:01+00:00",
  };
}

function succeededTask(options: { includeBlend?: boolean; redundantId?: boolean } = {}): TaskView {
  const { includeBlend = true, redundantId = false } = options;
  return {
    task: {
      ...waitingTask().task,
      status: "succeeded",
      result: {
        ...waitingTask().task.result,
        confirmation: { artifact_ids: ["art_1"], confirmed_at: "2026-10-20T10:01:00+00:00" },
        execution: {
          ok: true,
          plane: { name: "AnimeRef", image_count: 1, objects: ["AnimeRef_0"] },
          blend_artifact_id: "art_blend",
          ...(redundantId ? { blend_artifact_error: "path outside the allowed roots" } : {}),
        },
      },
    },
    artifacts: includeBlend
      ? [...waitingTask().artifacts, blendArtifact()]
      : waitingTask().artifacts,
  };
}

async function mountWith(views: TaskView[]) {
  fetchTaskViewsMock.mockResolvedValue({ tasks: views });
  const wrapper = mount(ReferencesView);
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  vi.clearAllMocks();
  previewMock.mockResolvedValue("data:image/png;base64,AAAA");
  openArtifactMock.mockResolvedValue({ artifact_id: "art_blend", action: "reveal", opened: true });
});

describe("ReferencesView", () => {
  it("shows an empty state when there are no tasks", async () => {
    const wrapper = await mountWith([]);
    expect(wrapper.text()).toContain("No reference tasks yet");
  });

  it("renders candidates with metadata and reported failures", async () => {
    const wrapper = await mountWith([waitingTask()]);

    expect(wrapper.findAll("[role='checkbox']")).toHaveLength(2);
    expect(wrapper.text()).toContain("Candidate art_1");
    expect(wrapper.text()).toContain("640 × 480");
    expect(wrapper.text()).toContain("2 KB");
    expect(wrapper.text()).toContain("timed out");
    expect(wrapper.findAll("img")).toHaveLength(2);
  });

  it("does not enable confirmation until something is selected", async () => {
    const wrapper = await mountWith([waitingTask()]);
    const confirm = wrapper.get("[data-test='confirm']");

    expect(confirm.attributes("disabled")).toBeDefined();
    await wrapper.get("[data-test='card-art_1']").trigger("click");

    expect(wrapper.get("[data-test='card-art_1']").attributes("aria-checked")).toBe("true");
    expect(wrapper.get("[data-test='confirm']").attributes("disabled")).toBeUndefined();
  });

  it("sends the real task id and artifact ids when confirming", async () => {
    confirmSelectionMock.mockResolvedValue({
      task: { ...waitingTask().task, status: "succeeded" },
      artifacts: waitingTask().artifacts,
    });
    const wrapper = await mountWith([waitingTask()]);

    await wrapper.get("[data-test='card-art_1']").trigger("click");
    await wrapper.get("[data-test='card-art_2']").trigger("click");
    await wrapper.get("[data-test='card-art_1']").trigger("click");
    await wrapper.get("[data-test='plane-name']").setValue("AnimeRef");
    await wrapper.get("[data-test='confirm']").trigger("click");
    await flushPromises();

    expect(confirmSelectionMock).toHaveBeenCalledTimes(1);
    expect(confirmSelectionMock).toHaveBeenCalledWith("task_1", ["art_2"], "AnimeRef");
  });

  it("blocks a second confirmation while the first is in flight", async () => {
    let release: (value: unknown) => void = () => {};
    confirmSelectionMock.mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    const wrapper = await mountWith([waitingTask()]);

    await wrapper.get("[data-test='card-art_1']").trigger("click");
    await wrapper.get("[data-test='confirm']").trigger("click");

    expect(wrapper.get("[data-test='confirm']").attributes("disabled")).toBeDefined();
    await wrapper.get("[data-test='confirm']").trigger("click");
    expect(confirmSelectionMock).toHaveBeenCalledTimes(1);

    release({ task: { ...waitingTask().task, status: "succeeded" }, artifacts: [] });
    await flushPromises();
  });

  it("shows the execution outcome after a successful confirmation", async () => {
    confirmSelectionMock.mockResolvedValue(succeededTask());
    const wrapper = await mountWith([waitingTask()]);

    await wrapper.get("[data-test='card-art_1']").trigger("click");
    await wrapper.get("[data-test='confirm']").trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("AnimeRef");
    expect(wrapper.text()).toContain("blend artifact art_blend");
    expect(wrapper.text()).toContain("Confirmed");
  });

  it("keeps the confirmed candidates visible and marked after a success", async () => {
    confirmSelectionMock.mockResolvedValue({
      task: {
        ...waitingTask().task,
        status: "succeeded",
        result: {
          ...waitingTask().task.result,
          confirmation: { artifact_ids: ["art_1"], confirmed_at: "2026-10-20T10:01:00+00:00" },
          execution: { ok: true, plane: { name: "AnimeRef", image_count: 1 } },
        },
      },
      artifacts: waitingTask().artifacts,
    });
    const wrapper = await mountWith([waitingTask()]);

    await wrapper.get("[data-test='card-art_1']").trigger("click");
    await wrapper.get("[data-test='confirm']").trigger("click");
    await flushPromises();

    expect(wrapper.findAll("[role='checkbox']")).toHaveLength(2);
    expect(wrapper.get("[data-test='card-art_1']").attributes("aria-checked")).toBe("true");
    // Nothing may be re-run from a finished task.
    expect(wrapper.get("[data-test='card-art_1']").attributes("disabled")).toBeDefined();
    expect(wrapper.find("[data-test='confirm']").exists()).toBe(false);
  });

  it("restores the confirmed selection from the server after a reload", async () => {
    const succeeded: TaskView = {
      task: {
        ...waitingTask().task,
        status: "succeeded",
        result: {
          ...waitingTask().task.result,
          confirmation: { artifact_ids: ["art_2"], confirmed_at: "2026-10-20T10:01:00+00:00" },
          execution: { ok: true, plane: { name: "AnimeRef", image_count: 1 } },
        },
      },
      artifacts: waitingTask().artifacts,
    };
    const wrapper = await mountWith([succeeded]);

    expect(wrapper.get("[data-test='card-art_1']").attributes("aria-checked")).toBe("false");
    expect(wrapper.get("[data-test='card-art_2']").attributes("aria-checked")).toBe("true");
    expect(wrapper.get("[data-test='card-art_2']").attributes("disabled")).toBeDefined();
  });

  it("never offers the produced blend file as a selectable candidate", async () => {
    const succeeded: TaskView = {
      task: {
        ...waitingTask().task,
        status: "succeeded",
        result: {
          ...waitingTask().task.result,
          confirmation: { artifact_ids: ["art_1"], confirmed_at: "2026-10-20T10:01:00+00:00" },
          execution: { ok: true, plane: { name: "AnimeRef", image_count: 1 } },
        },
      },
      artifacts: [
        ...waitingTask().artifacts,
        {
          artifact_id: "art_blend",
          kind: "model",
          media_type: "application/x-blender",
          size_bytes: 4096,
          metadata: { title: "AnimeRef.blend" },
          created_at: "2026-10-20T10:01:01+00:00",
        },
      ],
    };
    const wrapper = await mountWith([succeeded]);

    expect(wrapper.findAll("[role='checkbox']")).toHaveLength(2);
    expect(wrapper.find("[data-test='card-art_blend']").exists()).toBe(false);
    // The header must not count the blend file as a collected candidate either.
    expect(wrapper.get(".task__meta").text()).toContain("2 candidate(s)");
  });

  it("describes the produced blend file with verifiable metadata", async () => {
    const wrapper = await mountWith([succeededTask()]);

    const produced = wrapper.get("[data-test='produced']");
    expect(produced.text()).toContain("blend artifact art_blend");
    expect(produced.text()).toContain("4.0 KB");
    expect(produced.text()).toContain("created 2026-10-20T10:01:01+00:00");
    expect(produced.text()).toContain("application/x-blender");
    expect(produced.text()).toContain("1 object(s): AnimeRef_0");
    // A .blend is never offered as an inline image preview.
    expect(produced.find("img").exists()).toBe(false);
  });

  it("asks the runtime to reveal or open the produced file by id only", async () => {
    const wrapper = await mountWith([succeededTask()]);

    await wrapper.get("[data-test='reveal-artifact']").trigger("click");
    await flushPromises();
    expect(openArtifactMock).toHaveBeenCalledWith("art_blend", "reveal");

    await wrapper.get("[data-test='open-artifact']").trigger("click");
    await flushPromises();
    expect(openArtifactMock).toHaveBeenLastCalledWith("art_blend", "open");
    // No path is ever passed from the webview.
    for (const call of openArtifactMock.mock.calls) {
      expect(JSON.stringify(call)).not.toContain(":\\");
    }
  });

  it("reports a refused open instead of pretending it worked", async () => {
    openArtifactMock.mockRejectedValue(
      new RuntimeApiError(
        { code: "artifact_open_failed", message: "could not open the artifact with explorer" },
        500,
      ),
    );
    const wrapper = await mountWith([succeededTask()]);

    await wrapper.get("[data-test='reveal-artifact']").trigger("click");
    await flushPromises();

    const error = wrapper.get("[data-test='open-error']");
    expect(error.text()).toContain("could not open the artifact with explorer");
  });

  it("warns when the recorded scene artifact is missing instead of showing actions", async () => {
    const wrapper = await mountWith([succeededTask({ includeBlend: false })]);

    expect(wrapper.find("[data-test='produced']").exists()).toBe(false);
    expect(wrapper.get("[data-test='missing-artifact']").text()).toContain("art_blend");
    expect(wrapper.find("[data-test='open-artifact']").exists()).toBe(false);
  });

  it("reports a scene that was produced but could not be registered", async () => {
    const wrapper = await mountWith([succeededTask({ includeBlend: false, redundantId: true })]);

    expect(wrapper.get("[data-test='blend-warning']").text()).toContain(
      "path outside the allowed roots",
    );
  });

  it("shows the recovery note of a task that survived an interruption", async () => {
    const recovered: TaskView = {
      ...waitingTask(),
      task: {
        ...waitingTask().task,
        result: {
          ...waitingTask().task.result,
          recovery: {
            action: "resumed_partial_collection",
            detail: "2 downloaded image(s) survived the interruption",
          },
        },
      },
    };
    const wrapper = await mountWith([recovered]);

    const note = wrapper.get("[data-test='recovery-note']");
    expect(note.text()).toContain("resumed_partial_collection");
    expect(note.text()).toContain("2 downloaded image(s) survived");
  });

  it("labels the failures block instead of printing a bare list", async () => {
    const wrapper = await mountWith([waitingTask()]);

    expect(wrapper.get(".failures").text()).toContain("1 candidate(s) could not be saved");
    expect(wrapper.get(".failures").text()).toContain("https://cdn.example.com/bad.png");
  });

  it("reports a structured runtime error and refreshes after a conflict", async () => {
    confirmSelectionMock.mockRejectedValue(
      new RuntimeApiError({ code: "conflict", message: "task is not awaiting confirmation" }, 409),
    );
    const wrapper = await mountWith([waitingTask()]);

    await wrapper.get("[data-test='card-art_1']").trigger("click");
    await wrapper.get("[data-test='confirm']").trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("task is not awaiting confirmation");
    expect(fetchTaskViewsMock).toHaveBeenCalledTimes(2);
  });

  it("surfaces an unavailable runtime instead of pretending it worked", async () => {
    fetchTaskViewsMock.mockRejectedValue(new RuntimeUnavailableError("runtime is not running"));
    const wrapper = mount(ReferencesView);
    await flushPromises();

    expect(wrapper.text()).toContain("Runtime unavailable");
    expect(wrapper.text()).toContain("runtime is not running");
  });

  it("keeps a failed preview visible as an explicit state", async () => {
    previewMock.mockRejectedValue(new Error("artifact file is missing"));
    const wrapper = await mountWith([waitingTask(["art_1"])]);

    expect(wrapper.text()).toContain("artifact file is missing");
    expect(wrapper.findAll("img")).toHaveLength(0);
  });

  it("starts a collection with the trimmed query", async () => {
    startCollectionMock.mockResolvedValue({
      task: { ...waitingTask().task, status: "running" },
      artifacts: [],
    });
    const wrapper = await mountWith([]);

    await wrapper.get("input[type='search']").setValue("  anime girl  ");
    await wrapper.get("[data-test='submit']").trigger("submit");
    await flushPromises();

    expect(startCollectionMock).toHaveBeenCalledWith("anime girl", 6);
  });

  it("does not start a collection for a blank query", async () => {
    const wrapper = await mountWith([]);

    await wrapper.get("input[type='search']").setValue("   ");
    await wrapper.get("[data-test='submit']").trigger("submit");
    await flushPromises();

    expect(startCollectionMock).not.toHaveBeenCalled();
  });

  it("cancels a waiting task through the runtime", async () => {
    cancelTaskMock.mockResolvedValue({
      task: { ...waitingTask().task, status: "cancelled" },
      artifacts: waitingTask().artifacts,
    });
    const wrapper = await mountWith([waitingTask()]);

    await wrapper.get("[data-test='cancel']").trigger("click");
    await flushPromises();

    expect(cancelTaskMock).toHaveBeenCalledWith("task_1");
    expect(wrapper.text()).toContain("cancelled");
  });
});
