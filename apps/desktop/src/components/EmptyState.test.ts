import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import EmptyState from "./EmptyState.vue";

describe("EmptyState", () => {
  it("renders the title and description", () => {
    const wrapper = mount(EmptyState, {
      props: { title: "Nothing here", description: "Nothing yet" },
    });
    expect(wrapper.text()).toContain("Nothing here");
    expect(wrapper.text()).toContain("Nothing yet");
  });

  it("renders without a description", () => {
    const wrapper = mount(EmptyState, { props: { title: "Only a title" } });
    expect(wrapper.text()).toContain("Only a title");
  });
});
