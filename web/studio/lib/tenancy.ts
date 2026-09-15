import {
  ControlPlaneError,
  checkTurnQuota,
  readControlPlaneConfig,
  recordAudit,
  reportTurnUsage,
  type AuditAction,
  type ControlPlaneConfig,
} from "./controlplane";
import { resolveCaller, type Caller, type Gate } from "./identities";
import { isPlaceholderSecret, readConfig, type OmniRouteConfig } from "./omniroute";

/**
 * The per-turn tenancy gate: whose key pays, whether they may spend, and what
 * gets recorded (convergence plan §5.2).
 *
 * Before this, every turn spent the one `OMNIROUTE_API_KEY` in `.env`: no
 * attribution, no per-user quota, no way to tell two users apart in the gateway's
 * ledger. Now a turn resolves the caller to a control-plane account and spends
 * **that user's** key, which is what makes quota and usage real.
 *
 * What is strict and what is not, deliberately:
 *
 *   * **The key is strict.** With a control plane configured, a turn without one
 *     is refused — no quiet fallback to the shared `.env` key. A fallback would
 *     silently move one user's spend onto the operator's key, which is the exact
 *     problem this replaces.
 *   * **The quota check is fail-open.** A control plane that cannot answer does
 *     not stop a user from building; the gateway key's own hard caps remain the
 *     backstop (the same posture Distro's web app takes).
 *   * **Accounting is best-effort.** A turn that has already been paid for must
 *     not fail because the ledger write did.
 *
 * With no control plane configured at all, none of this applies and Studio is the
 * single-operator tool it was — see `readControlPlaneConfig`.
 */

export type Turn = {
  /** Gateway config for this turn, carrying the resolved per-user key. */
  config: OmniRouteConfig;
  /** The control-plane account paying for it, or null in single-operator mode. */
  caller: Caller | null;
};

export type TurnStarted = { ok: true; turn: Turn } | { ok: false; response: Response };

function fail(message: string, status: number): Response {
  return Response.json({ error: message }, { status, headers: { "cache-control": "no-store" } });
}

/**
 * Resolve the caller and the key that pays for this turn.
 *
 * Called at the top of both model routes, after `authorizeRequest` and before
 * anything is spent, so identity and money are decided in one place instead of
 * two that drift.
 */
export async function beginTurn(gate: Gate): Promise<TurnStarted> {
  const plane = readControlPlaneConfig();

  if (!plane) {
    // Single-operator: there is no account to attribute to, so the shared key
    // stays the credential and Studio behaves exactly as it did before.
    const config = readConfig();
    if (isPlaceholderSecret(config.apiKey)) {
      return {
        ok: false,
        response: fail(
          "No gateway key is configured. Set OMNIROUTE_API_KEY in the repo .env and restart Studio.",
          503,
        ),
      };
    }
    return { ok: true, turn: { config, caller: null } };
  }

  let caller: Caller | null;
  try {
    caller = await resolveCaller(gate, plane);
  } catch (error) {
    const detail = error instanceof Error ? error.message : "unknown error";
    return {
      ok: false,
      response: fail(
        `The tenancy service could not identify this account, so the model pool will not be spent: ${detail}`,
        error instanceof ControlPlaneError && error.status >= 400 && error.status < 500
          ? error.status
          : 503,
      ),
    };
  }

  if (!caller) {
    return {
      ok: false,
      response: fail(
        "Studio cannot tell which account this is. Sign in, so the model pool is spent on your own key.",
        401,
      ),
    };
  }

  const quota = await checkQuota(plane, caller.gatewayKey);
  if (!quota.allowed) {
    const reasons = quota.reasons.length > 0 ? quota.reasons.join(", ") : "quota exhausted";
    return {
      ok: false,
      response: fail(`Your account cannot start another turn right now — ${reasons}.`, 429),
    };
  }

  return { ok: true, turn: { config: { ...readConfig(), apiKey: caller.gatewayKey }, caller } };
}

/**
 * The quota decision, fail-open.
 *
 * Exported for its own test: the interesting behaviour is the catch, not the call.
 */
export async function checkQuota(
  plane: ControlPlaneConfig,
  gatewayKey: string,
): Promise<{ allowed: boolean; reasons: string[] }> {
  try {
    return await checkTurnQuota(plane, gatewayKey);
  } catch {
    // The gateway key's own cap is the backstop; a read-only hiccup on the
    // tenancy service must not become an outage for the builder.
    return { allowed: true, reasons: [] };
  }
}

/**
 * Record what the turn cost. Best-effort, and never awaited by a user-facing
 * response path that could fail because of it.
 */
export async function finishTurn(
  turn: Turn,
  usage: { tokensIn: number; tokensOut: number; requests: number; model?: string },
): Promise<void> {
  if (!turn.caller) return;
  const plane = readControlPlaneConfig();
  if (!plane) return;
  await reportTurnUsage(plane, turn.caller.gatewayKey, usage);
}

/**
 * Audit rows for the actions that touch a public name or the repository — build,
 * publish, export. The subject identifies the actor; the control plane resolves
 * it to an account.
 */
export async function auditBuild(
  gate: Gate,
  action: AuditAction,
  details: { targetId?: string; targetEmail?: string; meta?: Record<string, unknown> } = {},
): Promise<void> {
  const plane = readControlPlaneConfig();
  if (!plane) return;

  await recordAudit(plane, {
    action,
    sub: gate.session?.sub,
    actorEmail: gate.session?.email,
    targetId: details.targetId,
    targetEmail: details.targetEmail,
    meta: details.meta,
  });
}
