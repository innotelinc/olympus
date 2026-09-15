import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import type { GateResult } from "./auth";
import {
  provisionIdentity,
  readControlPlaneConfig,
  type ControlPlaneConfig,
  type Identity,
} from "./controlplane";
import { ANONYMOUS_NAMESPACE, namespaceFor, studioDataDir } from "./projects";

/**
 * Who the caller is, and where their library lives.
 *
 * Studio's library used to be keyed on the OIDC `sub` alone, which was the right
 * answer when Studio was the only thing that knew about a user. It is the wrong
 * answer once a control plane owns accounts: identity, quota and the per-user
 * gateway key live there, so the library is keyed on the **control-plane user
 * id** and the `sub` is recorded beside it (convergence plan §5.2).
 *
 * The `sub` is what makes the change safe. A library that already exists under
 * `u-<sha256(sub)>` is **adopted** — the mapping is written pointing at the
 * directory the files are already in — so a deployment that turns the control
 * plane on keeps its saved apps instead of appearing to lose every one of them.
 * New libraries get `u-<sha256(userId)>`.
 *
 * The mapping is a file, not memory, because it is a decision that must survive
 * a restart: `data/studio/identities.json`. It holds no secret — a user id, a
 * subject, an email and a directory name.
 */

export type Caller = {
  /** The control-plane user id: the key for everything Studio stores per user. */
  userId: string;
  sub: string;
  email: string;
  /** The library directory this user's apps live in. */
  namespace: string;
  /** This user's own gateway key. Server-side only; never sent to the browser. */
  gatewayKey: string;
  /** True when this call created the account in the control plane. */
  created: boolean;
};

type IndexEntry = {
  sub: string;
  email: string;
  namespace: string;
};

type Index = {
  version: 1;
  users: Record<string, IndexEntry>;
};

export type Gate = Extract<GateResult, { ok: true }>;

const INDEX_FILE = "identities.json";
const INDEX_VERSION = 1;
const CACHE_TTL_MS = 5 * 60 * 1000;

/**
 * In-process cache, so a page that lists, opens and builds does not re-provision
 * on every request. Short enough that a revoked account stops working promptly:
 * the identity answer is a routing decision, but the *quota* decision is re-checked
 * per turn and is never cached.
 */
let cache: { at: number; sub: string; caller: Caller } | null = null;

/** Only for tests: a cached caller would leak between cases. */
export function resetCallerCache(): void {
  cache = null;
}

function indexPath(): string {
  return join(studioDataDir(), INDEX_FILE);
}

function emptyIndex(): Index {
  return { version: INDEX_VERSION, users: {} };
}

function readIndex(): Index {
  const file = indexPath();
  if (!existsSync(file)) return emptyIndex();

  try {
    const parsed = JSON.parse(readFileSync(file, "utf8")) as Partial<Index>;
    const users = parsed.users;
    if (!users || typeof users !== "object") return emptyIndex();

    const clean: Record<string, IndexEntry> = {};
    for (const [userId, entry] of Object.entries(users)) {
      if (!entry || typeof entry !== "object") continue;
      const { sub, email, namespace } = entry as Partial<IndexEntry>;
      if (typeof namespace !== "string" || !namespace) continue;
      clean[userId] = {
        sub: typeof sub === "string" ? sub : "",
        email: typeof email === "string" ? email : "",
        namespace,
      };
    }
    return { version: INDEX_VERSION, users: clean };
  } catch {
    // A hand-edited or truncated index is one lost mapping, not a broken Studio:
    // the worst case is a library that has to be re-adopted from its sub.
    return emptyIndex();
  }
}

function writeIndex(index: Index): void {
  const dir = studioDataDir();
  mkdirSync(dir, { recursive: true, mode: 0o700 });

  const target = indexPath();
  // Write beside the target and rename, so a crash mid-write cannot leave a
  // half-index that reads as "this user has no library".
  const temporary = join(dir, `.${INDEX_FILE}.${randomBytes(4).toString("hex")}.tmp`);
  try {
    writeFileSync(temporary, `${JSON.stringify(index, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
    renameSync(temporary, target);
  } catch {
    // Not fatal: the caller still gets a namespace, it just is not remembered.
  }
}

function remember(userId: string, entry: IndexEntry): void {
  const index = readIndex();
  index.users[userId] = entry;
  writeIndex(index);
}

function entryForUser(userId: string): IndexEntry | null {
  return readIndex().users[userId] ?? null;
}

function entryForSub(sub: string): IndexEntry | null {
  for (const entry of Object.values(readIndex().users)) {
    if (entry.sub && entry.sub === sub) return entry;
  }
  return null;
}

/**
 * The library directory for this user.
 *
 * In order:
 *   1. the mapping already recorded for this user id — stable across restarts;
 *   2. the directory this user's `sub` was already using — adoption, so turning
 *      the control plane on does not orphan a library;
 *   3. `namespaceFor(userId)` — a new library, keyed on the account.
 */
function namespaceForCaller(userId: string, sub: string, email: string): string {
  const known = entryForUser(userId);
  if (known) return known.namespace;

  const legacy = namespaceFor(sub);
  const namespace = existsSync(join(studioDataDir(), legacy)) ? legacy : namespaceFor(userId);
  remember(userId, { sub, email, namespace });
  return namespace;
}

/**
 * Resolve the caller to an account, or null when Studio has no control plane.
 *
 * Throws (`ControlPlaneError`) when the plane is configured and cannot answer:
 * a configured-but-broken tenancy service must be loud, not silently replaced by
 * an unattributed shared key. Callers that only need a *directory* — listing
 * projects — use `libraryNamespace`, which degrades instead.
 */
export async function resolveCaller(gate: Gate, config?: ControlPlaneConfig | null): Promise<Caller | null> {
  const sub = gate.session?.sub?.trim();
  const plane = config ?? readControlPlaneConfig();
  if (!plane) return null;
  if (!sub) return null;

  if (cache && cache.sub === sub && Date.now() - cache.at < CACHE_TTL_MS) return cache.caller;

  const identity: Identity = await provisionIdentity(plane, {
    sub,
    email: gate.session?.email?.trim() ?? "",
  });

  const caller: Caller = {
    userId: identity.userId,
    sub,
    email: identity.email,
    namespace: namespaceForCaller(identity.userId, sub, identity.email),
    gatewayKey: identity.gatewayKey,
    created: identity.created,
  };

  cache = { at: Date.now(), sub, caller };
  return caller;
}

/**
 * The namespace a route should read and write this caller's apps under.
 *
 * Every saved-app route goes through this rather than `namespaceFor`, so there is
 * one answer to "where does this user's library live" and no route can key on
 * something the others do not. It degrades rather than throwing — a library that
 * cannot reach the control plane should still open:
 *
 *   * no control plane configured → the `sub` namespace, which is where the
 *     library already is (the single-operator and pre-convergence behaviour);
 *   * plane configured but unreachable → the mapping already recorded for this
 *     `sub`, which is the same directory the files are in;
 *   * no session at all → the anonymous namespace, exactly as before.
 */
export async function libraryNamespace(gate: Gate): Promise<string> {
  const sub = gate.session?.sub?.trim();
  if (!sub) return ANONYMOUS_NAMESPACE;

  try {
    const caller = await resolveCaller(gate);
    if (caller) return caller.namespace;
  } catch {
    // Fall through: the mapping (or the legacy namespace) is where the files are.
  }

  return entryForSub(sub)?.namespace ?? namespaceFor(sub);
}
