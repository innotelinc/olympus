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
 * The limit can be disabled entirely — `STUDIO_RATE_LIMIT_PER_MIN=0` (or
 * `off`) means no cap. For a single-operator deployment behind an IdP that is a
 * legitimate choice; the default exists for the shared case.
 */

export const RATE_LIMIT_ENV = "STUDIO_RATE_LIMIT_PER_MIN";

/** Generations per minute per identity when nothing is configured. */
export const DEFAULT_RATE_LIMIT_PER_MIN = 20;

/** Hard cap — enough headroom for heavy interactive use, low enough that a runaway loop still gets stopped. */
export const MAX_RATE_LIMIT_PER_MIN = 600;

/** Values that switch the limit off rather than falling back to the default. */
const DISABLED_VALUES = new Set(["0", "off", "false", "disabled", "no", "unlimited"]);

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
  const limit = readRateLimitConfig();

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

/** Test hook: drop every counter. */
export function resetRateLimits(): void {
  buckets.clear();
  lastSweep = 0;
}
