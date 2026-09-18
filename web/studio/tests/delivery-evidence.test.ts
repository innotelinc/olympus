import { describe, expect, it } from "vitest";
import { readBuildStatus } from "@/lib/build-queue";

/**
 * The panel does not re-derive the delivery verdict — it renders what
 * `scripts/delivery-evidence.py` recorded — so what this file pins is the one
 * thing the panel *does* decide: how a recorded evidence file becomes a row.
 *
 * Two rules matter, and both are about not inventing a reading:
 *
 *  * a record with no name is not evidence of anything, so it is `null` rather
 *    than an empty check that would render as a site with no verdict;
 *  * a chain that was **not walked** (an older evidence file, or a walker that
 *    could not run) is `null`, which is not the same as `{ broken: [] }`. Showing
 *    the second as the first would report an unchecked name as an all-clear.
 */

// `readBuildStatus` is the reader that maps a recorded job file; the evidence
// mapping is what is under test, so the file is written the way the runner does.
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const JOB = "0123456789abcdef";

function statusFor(evidence: unknown, overrides: Record<string, unknown> = {}) {
  const root = mkdtempSync(join(tmpdir(), "studio-evidence-"));
  mkdirSync(root, { recursive: true });
  writeFileSync(
    join(root, `${JOB}.status.json`),
    JSON.stringify({
      job: JOB,
      action: "publish",
      slug: "todo-list",
      state: "succeeded",
      exit_code: 0,
      delivery_evidence: evidence,
      ...overrides,
    }),
  );
  process.env.STUDIO_BUILD_QUEUE_DIR = root;
  return readBuildStatus(JOB);
}

const RECORDED = {
  v: 2,
  checked_at: "2026-09-18T14:00:00Z",
  host: "todo-list.studio.olympus.innotel.us",
  slug: "todo-list",
  preview: false,
  served: false,
  verdict: "edge-unreachable",
  detail: "the app is running, and Cerulean did not answer, so the name could not be registered",
  retry: "make edge-publish SLUG=todo-list",
  edge: "unreachable",
  app: { state: "running", port: 3011, container: "studio-todo-list" },
  checks: 3,
};

describe("delivery evidence, as the panel reads it", () => {
  it("keeps the verdict, the retry and the runtime's own record", () => {
    const status = statusFor(RECORDED);
    expect(status?.deliveryEvidence).toMatchObject({
      host: "todo-list.studio.olympus.innotel.us",
      slug: "todo-list",
      served: false,
      verdict: "edge-unreachable",
      retry: "make edge-publish SLUG=todo-list",
      edge: "unreachable",
      app: { state: "running", port: 3011, container: "studio-todo-list" },
      checks: 3,
      // v1 evidence carries no chain, and this record has none: nothing was walked.
      chain: null,
    });
  });

  it("reads the chain when the check walked it", () => {
    const status = statusFor({
      ...RECORDED,
      verdict: "name-missing",
      chain: {
        ok: false,
        verdict: "todo-list… does not exist in the zone",
        broken: ["dns"],
        links: { dns: { ok: false, error: "NXDOMAIN" }, tls: { ok: true, error: "" }, edge: { ok: true, error: "" } },
      },
    });
    expect(status?.deliveryEvidence?.chain).toEqual({
      ok: false,
      verdict: "todo-list… does not exist in the zone",
      broken: ["dns"],
      links: { dns: { ok: false, error: "NXDOMAIN" }, tls: { ok: true, error: "" }, edge: { ok: true, error: "" } },
    });
  });

  it("an unwalked chain is null, not an all-clear", () => {
    // A walker that could not run, and a v1 record, both say "not walked" — and
    // neither may render as a chain with nothing broken.
    for (const value of [undefined, null, "dns", 42, {}, { links: {}, broken: [] }]) {
      expect(statusFor({ ...RECORDED, chain: value })?.deliveryEvidence?.chain).toBeNull();
    }
  });

  it("a chain nobody could walk but whose record says so stays readable", () => {
    const status = statusFor({
      ...RECORDED,
      chain: { ok: false, verdict: "", broken: ["edge"], links: { edge: { ok: false } } },
    });
    // `error` is absent in that record; it must read as empty, not as undefined.
    expect(status?.deliveryEvidence?.chain?.links.edge).toEqual({ ok: false, error: "" });
  });

  it("evidence without a name is not evidence", () => {
    // A record the runner wrote before it knew the name (or a truncated one) must not
    // produce a row that says nothing about a site.
    for (const host of ["", "   ", undefined, null, 42]) {
      expect(statusFor({ ...RECORDED, host })?.deliveryEvidence).toBeNull();
    }
  });

  it("a job that was never checked has no evidence, which is not a failure", () => {
    const status = statusFor(undefined);
    expect(status?.deliveryEvidence).toBeNull();
    // The job is still a finished publish — the panel reads "not checked", which is
    // deliberately different from "checked and not serving".
    expect(status?.state).toBe("succeeded");
  });
});
