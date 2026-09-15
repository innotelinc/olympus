import { describe, expect, it } from "vitest";
import { PLAN_TARGETS, parseTarget } from "@/lib/targets";

/**
 * The target is read in two places that cannot see each other — the packager at
 * build time and the runtime at start time — so what this file pins is that both
 * of them get the same answer for the same input, including for input that is
 * neither target.
 */
describe("parseTarget", () => {
  it("reads the two targets", () => {
    expect(parseTarget("container")).toBe("container");
    expect(parseTarget("convex")).toBe("convex");
  });

  it("treats anything else as the container every built project already was", () => {
    // A plan stored before this field existed has no target at all, and a planner
    // that invents one ("postgres", "Convex", true) has described a target this
    // platform cannot serve. Both have to mean the same thing, and the thing they
    // mean is the default — not an error, and not a third value.
    for (const value of [undefined, null, "", "Convex", "postgres", "CONVEX", 42, true, {}, []]) {
      expect(parseTarget(value)).toBe("container");
    }
  });

  it("lists the default first, which is the order worth reading them in", () => {
    expect(PLAN_TARGETS).toEqual(["container", "convex"]);
  });
});
