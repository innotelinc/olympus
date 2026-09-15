import { existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { resetCallerCache, libraryNamespace, resolveCaller, type Gate } from "@/lib/identities";
import { ANONYMOUS_NAMESPACE, namespaceFor } from "@/lib/projects";
import { startFakeControlPlane, type FakeControlPlane } from "./helpers/mock-control-plane";

/**
 * Re-keying the library from the OIDC subject to the control-plane user id is
 * only safe if an existing library is *adopted* rather than orphaned, so these
 * tests are mostly about the three-way choice in `namespaceForCaller`:
 * a recorded mapping, the directory the subject was already using, or a new
 * directory keyed on the account.
 */

const TMP = join(process.cwd(), "tests", ".tmp", "identities");
const SUB = "sub-1";
const USER_ID = "cp-user-1";

const GATE: Gate = { ok: true, session: { sub: SUB, email: "dev@example.com" } };

let plane: FakeControlPlane | null = null;

beforeEach(() => {
  rmSync(TMP, { recursive: true, force: true });
  process.env.STUDIO_DATA_DIR = TMP;
  process.env.CONTROL_PLANE_INTERNAL_URL = "";
  process.env.CONTROL_INTERNAL_TOKEN = "";
  resetCallerCache();
});

afterEach(async () => {
  if (plane) await plane.close();
  plane = null;
  resetCallerCache();
  rmSync(TMP, { recursive: true, force: true });
  delete process.env.STUDIO_DATA_DIR;
});

async function enablePlane(): Promise<FakeControlPlane> {
  plane = await startFakeControlPlane({ userId: USER_ID });
  process.env.CONTROL_PLANE_INTERNAL_URL = plane.url;
  process.env.CONTROL_INTERNAL_TOKEN = "internal-token";
  return plane;
}

function index(): Record<string, { sub?: string; email?: string; namespace?: string }> {
  const file = join(TMP, "identities.json");
  if (!existsSync(file)) return {};
  return (JSON.parse(readFileSync(file, "utf8")) as { users: Record<string, never> }).users ?? {};
}

describe("with no control plane", () => {
  it("keeps the subject-keyed library and writes no index", async () => {
    expect(await libraryNamespace(GATE)).toBe(namespaceFor(SUB));
    expect(existsSync(join(TMP, "identities.json"))).toBe(false);
  });

  it("resolves no caller at all", async () => {
    expect(await resolveCaller(GATE)).toBeNull();
  });

  it("uses the anonymous library when the session has no subject", async () => {
    expect(await libraryNamespace({ ok: true, session: null })).toBe(ANONYMOUS_NAMESPACE);
  });
});

describe("with a control plane", () => {
  it("keys a new library on the account and records the subject beside it", async () => {
    await enablePlane();

    expect(await libraryNamespace(GATE)).toBe(namespaceFor(USER_ID));
    expect(index()[USER_ID]).toMatchObject({ sub: SUB, namespace: namespaceFor(USER_ID) });
  });

  it("adopts the directory the subject was already using", async () => {
    // The library that exists today, from before the control plane was turned on.
    const legacy = namespaceFor(SUB);
    mkdirSync(join(TMP, legacy), { recursive: true });

    await enablePlane();

    expect(await libraryNamespace(GATE)).toBe(legacy);
    expect(index()[USER_ID]).toMatchObject({ sub: SUB, namespace: legacy });
  });

  it("returns the gateway key and the created flag to the caller", async () => {
    await enablePlane();

    const caller = await resolveCaller(GATE);

    expect(caller).toMatchObject({
      userId: USER_ID,
      sub: SUB,
      email: "dev@example.com",
      gatewayKey: "sk-per-user",
      created: true,
      namespace: namespaceFor(USER_ID),
    });
  });

  it("keeps serving a recorded mapping when the plane goes away", async () => {
    const fake = await enablePlane();
    const mapped = await libraryNamespace(GATE);
    await fake.close();
    plane = null;

    // A fresh process, with the plane now unreachable: the mapping on disk is
    // what keeps the user's apps where they are instead of "disappearing".
    resetCallerCache();
    process.env.CONTROL_PLANE_INTERNAL_URL = "http://127.0.0.1:1";

    expect(await libraryNamespace(GATE)).toBe(mapped);
  });

  it("falls back to the subject's own directory when nothing is recorded", async () => {
    resetCallerCache();
    process.env.CONTROL_PLANE_INTERNAL_URL = "http://127.0.0.1:1";
    process.env.CONTROL_INTERNAL_TOKEN = "internal-token";

    // Nothing mapped yet and the plane is unreachable: the pre-convergence
    // namespace is where this user's files are, so that is the answer.
    expect(await libraryNamespace(GATE)).toBe(namespaceFor(SUB));
  });

  it("uses the anonymous library when the session has no subject", async () => {
    await enablePlane();

    expect(await libraryNamespace({ ok: true, session: null })).toBe(ANONYMOUS_NAMESPACE);
  });
});
