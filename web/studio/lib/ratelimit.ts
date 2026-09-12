/**
 * The per-identity rate limit for /api/generate.
 *
 * Input is bounded and the gateway key stays server-side, but none of that
 * stops one browser tab in a retry loop from hammering the gateway — every
 * request bills the shared model pool. This caps how often one identity may
 * start a generation.
 *
 * Identity comes from the verified session (the same `sub` the auth gate
 * establishes), never from an IP or header, so the limit follows the account
 * and cannot be shed by rotating proxies. Anonymous requests never reach this
 * module — the auth gate runs first.
 *
 * The store is in-memory on purpose: one Studio process, and losing the
 * counters to a restart merely re-opens the window a few minutes early. A
 * shared store would only earn its keep with more than one replica.
 *
 * Counters are fixed one-minute windows (cheap, allocation-free, trivially
 * explainable in an incident) with a cheap cleanup pass so the map cannot grow
 * with the number of identities ever seen.
 *
 * Two layers decide the limit:
 *
 *   1. the environment — `STUDIO_RATE_LIMIT_PER_MIN` is the deployment default
 *      (`0`/`off` disables, a typo falls back to the default, never to
 *      unlimited);
 *   2. a **runtime override** stored beside the saved apps, which the operator
 *      flips from Studio's Settings panel. The override wins while it exists;
 *      clearing it hands control back to the environment, so the deployment
 *      default is always recoverable from the UI.
 *
 * The override file is read on each evaluation rather than cached. It is a few
 * dozen bytes, it is read at most once per generation attempt, and not caching
 * means the setting cannot go stale in a long-lived process, cannot disagree
 * between two processes, and needs no invalidation hook.
 */

import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { randomBytes } from "node:crypto";
import { join } from "node:path";
import { studioDataDir } from "./projects";

export const RATE_LIMIT_ENV = "STUDIO_RATE_LIMIT_PER_MIN";

/** Generations per minute per identity when nothing is configured. */
export const DEFAULT_RATE_LIMIT_PER_MIN = 20;

/** Hard cap — enough headroom for heavy interactive use, low enough that a runaway loop still gets stopped. */
export const MAX_RATE_LIMIT_PER_MIN = 600;

/** Values that switch the limit off rather than falling back to the default. */
const DISABLED_VALUES = new Set(["0", "off", "false", "disabled", "no", "unlimited"]);

/** Where the operator's runtime override is stored, under the Studio data directory. */
const OVERRIDE_FILE = "rate-limit.json";

/**
 * The configured per-minute limit, or `null` when the limit is disabled.
 * An unparseable value falls back to the default rather than silently meaning
 * "unlimited" — a typo should not remove the only spend cap.
 */
export function readRateLimitConfig(): number | null {
  const raw = process.env[RATE_LIMIT_ENV]?.trim() ?? "";
  if (!raw) return DEFAULT_RATE_LIMIT_PER_MIN;
  if (DISABLED_VALUES.has(raw.toLowerCase())) return null;
  const parsed = Number.parseInt(raw, 10);
  if (Number.isNaN(parsed) || parsed < 1) return DEFAULT_RATE_LIMIT_PER_MIN;
  return Math.min(parsed, MAX_RATE_LIMIT_PER_MIN);
}

/* ---- the runtime override ----------------------------------------------- */

/** Where the override lives — beside the saved apps, so one volume holds all Studio state. */
export function rateLimitOverridePath(): string {
  return join(studioDataDir(), OVERRIDE_FILE);
}

/**
 * The stored override, or `null` when none is stored (the deployment default
 * then applies). An override of `0` means "disabled" — deliberately distinct
 * from `null`, which means "no opinion".
 *
 * An absent, unreadable or malformed file reads as "no override" rather than
 * failing the route: the environment default is a safe answer, and a corrupt
 * settings file must never take generation down.
 */
export function readRateLimitOverride(): number | null {
  let raw: string;
  try {
    raw = readFileSync(rateLimitOverridePath(), "utf8");
  } catch {
    return null;
  }

  try {
    const parsed = JSON.parse(raw) as { perMinute?: unknown };
    const value = parsed?.perMinute;
    if (typeof value !== "number" || !Number.isInteger(value) || value < 0) return null;
    return Math.min(value, MAX_RATE_LIMIT_PER_MIN);
  } catch {
    return null;
  }
}

/**
 * Store (`0` = disabled, `1…MAX` = limit) or clear (`null`) the override.
 * Throws only on a value the caller should have validated.
 */
export function writeRateLimitOverride(value: number | null): void {
  const target = rateLimitOverridePath();

  if (value === null) {
    rmSync(target, { force: true });
    return;
  }

  if (!Number.isInteger(value) || value < 0) {
    throw new RangeError("A rate limit must be a whole number of generations per minute, or null.");
  }

  const dir = studioDataDir();
  mkdirSync(dir, { recursive: true, mode: 0o700 });

  // Write beside the target and rename, so a crash mid-write cannot leave a
  // truncated settings file that reads as something other than what was set.
  const temporary = join(dir, `.rate-limit.${randomBytes(4).toString("hex")}.tmp`);
  const clamped = Math.min(value, MAX_RATE_LIMIT_PER_MIN);

  try {
    writeFileSync(temporary, `${JSON.stringify({ perMinute: clamped }, null, 2)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
    renameSync(temporary, target);
  } catch (error) {
    rmSync(temporary, { force: true });
    throw error;
  }
}

/* ---- the effective limit ------------------------------------------------ */

export type RateLimitSource = "override" | "env" | "default";

export type RateLimitState = {
  /** Effective limit; `null` means the limit is switched off. */
  limit: number | null;
  /** Which layer decided it. */
  source: RateLimitSource;
  /** What the environment asks for, ignoring any override (`null` = disabled). */
  envLimit: number | null;
  /** The raw environment value, for display — empty means "not set". */
  envRaw: string;
  /** The stored override: `null` = none, `0` = disabled, `n` = limit. */
  override: number | null;
  /** Upper bound the API accepts, so the UI can label its input honestly. */
  max: number;
};

/** The effective limit, with provenance, for the settings endpoint and the UI. */
export function readRateLimitState(): RateLimitState {
  const envRaw = process.env[RATE_LIMIT_ENV]?.trim() ?? "";
  const envLimit = readRateLimitConfig();
  const override = readRateLimitOverride();

  if (override === null) {
    return {
      limit: envLimit,
      // An env value that parses to "disabled" is still the env deciding.
      source: envRaw ? "env" : "default",
      envLimit,
      envRaw,
      override,
      max: MAX_RATE_LIMIT_PER_MIN,
    };
  }

  return {
    limit: override === 0 ? null : override,
    source: "override",
    envLimit,
    envRaw,
    override,
    max: MAX_RATE_LIMIT_PER_MIN,
  };
}

/** The limit the counter enforces — `null` when switched off. */
function effectiveLimit(): number | null {
  const override = readRateLimitOverride();
  if (override === null) return readRateLimitConfig();
  return override === 0 ? null : override;
}

/** Drop counters not touched within this window; identity namespaces are tiny, but unbounded is unbounded. */
const IDLE_SWEEP_MS = 15 * 60_000;
const SWEEP_EVERY_MS = 60_000;

const WINDOW_MS = 60_000;

type Bucket = { windowStart: number; count: number; lastSeen: number };

const buckets = new Map<string, Bucket>();
let lastSweep = 0;

export type RateLimitResult = {
  ok: boolean;
  /** The active per-minute limit; 0 means the limit is disabled. */
  limit: number;
  remaining: number;
  /** Seconds until the current window ends; 0 when the request is allowed. */
  retryAfterSeconds: number;
};

function sweep(now: number): void {
  if (now - lastSweep < SWEEP_EVERY_MS) return;
  lastSweep = now;
  for (const [key, bucket] of buckets) {
    if (now - bucket.lastSeen > IDLE_SWEEP_MS) buckets.delete(key);
  }
}

/**
 * Account one generation attempt for `identity`. Called after the auth gate has
 * verified the caller, so the identity is trusted.
 */
export function checkRateLimit(identity: string, now: number = Date.now()): RateLimitResult {
  const limit = effectiveLimit();

  // Disabled: never touch a counter, so re-enabling later starts everyone at a
  // clean window instead of one their pre-disable activity filled.
  if (limit === null) {
    return { ok: true, limit: 0, remaining: -1, retryAfterSeconds: 0 };
  }

  const windowStart = Math.floor(now / WINDOW_MS) * WINDOW_MS;
  sweep(now);

  const bucket = buckets.get(identity);
  const count = bucket && bucket.windowStart === windowStart ? bucket.count : 0;

  if (count >= limit) {
    return {
      ok: false,
      limit,
      remaining: 0,
      retryAfterSeconds: Math.max(1, Math.ceil((windowStart + WINDOW_MS - now) / 1000)),
    };
  }

  buckets.set(identity, { windowStart, count: count + 1, lastSeen: now });
  return { ok: true, limit, remaining: limit - count - 1, retryAfterSeconds: 0 };
}

/** Test hook: drop every counter (not the stored override — tests own that file). */
export function resetRateLimits(): void {
  buckets.clear();
  lastSweep = 0;
}

/** Test hook: whether an override file exists on disk right now. */
export function rateLimitOverrideExists(): boolean {
  return existsSync(rateLimitOverridePath());
}
