import { loadRepoEnv } from "./env";
import { isPlaceholderSecret } from "./omniroute";

/**
 * Distro's control plane, as Studio consumes it (build-plane convergence plan
 * §5.2).
 *
 * Studio is single-tenant in the ways that cost money: it holds **one** gateway
 * key in `.env`, resolves it server-side, and has no per-identity attribution or
 * quota. Distro's control plane already solved exactly that, and OmniRoute
 * accounts per **API key**, so mapping `user ↔ gateway_key` gives attribution and
 * enforcement without the gateway being touched.
 *
 * Three calls, and each has one job:
 *
 *   identity   → `sub` + email in, account + that user's gateway key out
 *                (service-to-service; `x-control-internal-token`)
 *   quota      → the user's gateway key in, allow/deny out (bearer = gateway key,
 *                which is how the control plane identifies the account)
 *   audit      → build/publish/export rows for the actions that touch a public
 *                name or the repository
 *
 * Nothing here ever returns the gateway key to the browser: Studio calls these
 * from route handlers only, and its README makes "the key never reaches the
 * client bundle" a hard rule.
 *
 * Configuration is `CONTROL_PLANE_INTERNAL_URL` + `CONTROL_INTERNAL_TOKEN`. With
 * neither set the whole module is inert and Studio behaves as the single-operator
 * tool it has been — see `lib/tenancy.ts` for what that means per turn.
 */

export type ControlPlaneConfig = {
  url: string;
  token: string;
};

export type QuotaSnapshot = {
  plan: string;
  requestsPerDay: number | null;
  tokensPerDay: number | null;
  spendCapUsd: number | null;
};

export type UsageSnapshot = {
  tokensIn: number;
  tokensOut: number;
  requests: number;
  costUsd: number;
  date: string;
};

export type Identity = {
  /** The control-plane user id — what Studio's library is keyed on. */
  userId: string;
  sub: string;
  email: string;
  /** This user's own gateway key. Server-side only. */
  gatewayKey: string;
  /** True when this call created the account (it did not exist before). */
  created: boolean;
  quota: QuotaSnapshot | null;
  usageToday: UsageSnapshot | null;
};

export type QuotaDecision = {
  allowed: boolean;
  reasons: string[];
};

/** A control-plane call that failed. `status` is the plane's when it answered. */
export class ControlPlaneError extends Error {
  constructor(
    message: string,
    readonly status: number = 502,
  ) {
    super(message);
    this.name = "ControlPlaneError";
  }
}

const REQUEST_TIMEOUT_MS = 10_000;

/**
 * The configured control plane, or null when Studio has none.
 *
 * A placeholder token is treated as unconfigured: `.env.example` ships
 * `change-me…`, and reading that as a credential would turn a fresh checkout
 * into a service that answers 401 to every user for a reason no log explains.
 */
export function readControlPlaneConfig(): ControlPlaneConfig | null {
  loadRepoEnv();

  const url = process.env.CONTROL_PLANE_INTERNAL_URL?.trim() ?? "";
  const token = process.env.CONTROL_INTERNAL_TOKEN?.trim() ?? "";
  if (!url || isPlaceholderSecret(token)) return null;

  return { url: url.replace(/\/+$/, ""), token };
}

export function controlPlaneEnabled(): boolean {
  return readControlPlaneConfig() !== null;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function parseQuota(value: unknown): QuotaSnapshot | null {
  if (typeof value !== "object" || value === null) return null;
  const record = asRecord(value);
  return {
    plan: asString(record.plan) || "free",
    requestsPerDay: asNumber(record.requests_per_day),
    tokensPerDay: asNumber(record.tokens_per_day),
    spendCapUsd: asNumber(record.spend_cap_usd),
  };
}

function parseUsage(value: unknown): UsageSnapshot | null {
  if (typeof value !== "object" || value === null) return null;
  const record = asRecord(value);
  return {
    tokensIn: asNumber(record.tokens_in) ?? 0,
    tokensOut: asNumber(record.tokens_out) ?? 0,
    requests: asNumber(record.requests) ?? 0,
    costUsd: asNumber(record.cost_usd) ?? 0,
    date: asString(record.date),
  };
}

/**
 * One JSON call to the control plane.
 *
 * The error message never echoes the request headers: one of them is a
 * credential (either the service token or the user's gateway key), and a message
 * that carries it ends up in a log line and eventually in a bug report.
 */
async function call(
  config: ControlPlaneConfig,
  path: string,
  init: { method: string; headers?: Record<string, string>; body?: unknown },
): Promise<Record<string, unknown>> {
  let response: Response;
  try {
    response = await fetch(`${config.url}${path}`, {
      method: init.method,
      headers: {
        "content-type": "application/json",
        ...(init.headers ?? {}),
      },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      cache: "no-store",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : "unknown error";
    throw new ControlPlaneError(`Could not reach the tenancy service at ${config.url} (${detail}).`);
  }

  const text = await response.text().catch(() => "");
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const reason = asString(asRecord(payload).error) || `HTTP ${response.status}`;
    throw new ControlPlaneError(`The tenancy service refused that call: ${reason}.`, response.status);
  }

  return asRecord(payload);
}

/**
 * Resolve an Authentik identity to an account and that account's gateway key.
 *
 * Idempotent on the control-plane side: the account is created (with its own
 * gateway key) the first time a subject is seen and returned unchanged after
 * that. A `409` means the email is already bound to a *different* subject — a
 * conflict an operator has to resolve, never something to paper over by
 * creating a second account for one human.
 */
export async function provisionIdentity(
  config: ControlPlaneConfig,
  input: { sub: string; email: string; name?: string },
): Promise<Identity> {
  const payload = await call(config, "/api/internal/identity", {
    method: "POST",
    headers: { "x-control-internal-token": config.token },
    body: { sub: input.sub, email: input.email, name: input.name },
  });

  const user = asRecord(payload.user);
  const gatewayKey = asString(payload.gatewayKey);
  if (!gatewayKey) {
    throw new ControlPlaneError("The tenancy service did not return a gateway key for this account.");
  }

  return {
    userId: asString(user.id),
    sub: asString(payload.oidcSub) || input.sub,
    email: asString(user.email) || input.email,
    gatewayKey,
    created: payload.created === true,
    quota: parseQuota(payload.quota),
    usageToday: parseUsage(payload.usageToday),
  };
}

/**
 * Pre-flight quota gate, called once per model turn before anything is spent.
 *
 * The control plane identifies the account by the gateway key itself, so the
 * user's own key is the credential here — no session token leaves Studio.
 */
export async function checkTurnQuota(
  config: ControlPlaneConfig,
  gatewayKey: string,
): Promise<QuotaDecision> {
  const payload = await call(config, "/api/internal/quota-check", {
    method: "GET",
    headers: { authorization: `Bearer ${gatewayKey}` },
  });

  const reasons = Array.isArray(payload.reasons)
    ? payload.reasons.filter((reason): reason is string => typeof reason === "string")
    : [];

  return { allowed: payload.allowed === true, reasons };
}

/**
 * Record a finished turn.
 *
 * Best-effort on purpose: the turn has already been paid for, and failing it
 * after the fact would trade a small accounting gap for a broken answer. The
 * gateway key's own hard caps remain the backstop.
 */
export async function reportTurnUsage(
  config: ControlPlaneConfig,
  gatewayKey: string,
  usage: { tokensIn: number; tokensOut: number; requests: number; model?: string },
): Promise<void> {
  try {
    await call(config, "/api/internal/usage-report", {
      method: "POST",
      headers: { authorization: `Bearer ${gatewayKey}` },
      body: {
        tokensIn: Math.max(0, Math.round(usage.tokensIn) || 0),
        tokensOut: Math.max(0, Math.round(usage.tokensOut) || 0),
        requests: Math.max(1, Math.round(usage.requests) || 1),
        model: usage.model ? usage.model.slice(0, 200) : undefined,
      },
    });
  } catch {
    // Deliberately swallowed (and not logged with the key): see above.
  }
}

/** The three actions worth an audit row — they touch a public name or the repo. */
export type AuditAction =
  | "build.start"
  | "build.publish"
  | "build.export"
  | "build.cancel"
  | "app.delete";

/**
 * Write one audit row.
 *
 * Best-effort for the same reason as usage: an audit row that could not be
 * written must not be the thing that fails a publish the user already made.
 */
export async function recordAudit(
  config: ControlPlaneConfig,
  event: {
    action: AuditAction;
    sub?: string;
    actorEmail?: string;
    targetId?: string;
    targetEmail?: string;
    meta?: Record<string, unknown>;
  },
): Promise<void> {
  try {
    await call(config, "/api/internal/audit", {
      method: "POST",
      headers: { "x-control-internal-token": config.token },
      body: {
        action: event.action,
        sub: event.sub,
        actorEmail: event.actorEmail,
        targetId: event.targetId,
        targetEmail: event.targetEmail,
        meta: event.meta,
      },
    });
  } catch {
    // See reportTurnUsage.
  }
}
