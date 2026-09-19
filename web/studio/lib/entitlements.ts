import { loadRepoEnv } from "./env";

/**
 * Does this user pay for the plan that unlocks paid models?
 *
 * Magnate owns the billing decision and Studio never holds a Stripe key: it asks
 * `/api/entitlements?plan=<slug>&user=<email>` and reads `entitled`. That endpoint
 * is the same one Distro, Capstone and Zeus consume, so "is this user paid?" has
 * one answer across the estate rather than one per product.
 *
 * **Unentitled is the default, and that is deliberate.** A paid model spends real
 * money on a provider key somebody loaded into the gateway, so an unreachable
 * Magnate, an unset token or an unset URL must not read as "paid". The failure
 * mode of being wrong here is a surprise on an invoice, and the failure mode of
 * being strict here is a user picking a free model — so this fails closed and the
 * caller only offers paid models when the answer is a definite `true`.
 */

export type EntitlementConfig = {
  baseUrl: string;
  token: string;
  /** The plan whose members may choose a paid model (Magnate plan slug). */
  plan: string;
};

const DEFAULT_PLAN = "olympus";

export function readEntitlementConfig(): EntitlementConfig | null {
  loadRepoEnv();
  const baseUrl = (process.env.MAGNATE_URL?.trim() || "").replace(/\/+$/, "");
  if (!baseUrl) return null;
  return {
    baseUrl,
    token: process.env.ENTITLEMENTS_API_TOKEN?.trim() || "",
    plan: process.env.OLYMPUS_PLAN_SLUG?.trim() || DEFAULT_PLAN,
  };
}

/** The decision, with what justified it, so a page can explain itself. */
export type EntitlementVerdict = {
  paid: boolean;
  /** `unconfigured`, `entitled`, `no_subscription`, `unknown`, `error`. */
  reason: string;
  plan: string | null;
};

const NOT_PAID = (reason: string, plan: string | null = null): EntitlementVerdict => ({
  paid: false,
  reason,
  plan,
});

/**
 * Ask Magnate about one user. Never throws: a billing service that cannot answer
 * must not break a build, it must only cost the caller the paid models.
 */
export async function entitlementFor(
  config: EntitlementConfig | null,
  email: string,
  timeoutMs = 5_000,
): Promise<EntitlementVerdict> {
  if (!config) return NOT_PAID("unconfigured");
  const address = (email || "").trim();
  if (!address) return NOT_PAID("unknown", config.plan);

  const url = new URL(`${config.baseUrl}/api/entitlements`);
  url.searchParams.set("plan", config.plan);
  url.searchParams.set("user", address);

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      headers: config.token ? { authorization: `Bearer ${config.token}` } : {},
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) return NOT_PAID(response.status === 401 ? "error" : "unknown", config.plan);
    const payload = (await response.json()) as {
      entitled?: boolean | null;
      reason?: unknown;
      slug?: unknown;
    };
    if (payload.entitled === true) {
      return {
        paid: true,
        reason: "entitled",
        plan: typeof payload.slug === "string" ? payload.slug : config.plan,
      };
    }
    // `null` is the connectivity probe's answer, not a subscription decision.
    const reason = typeof payload.reason === "string" ? payload.reason : "no_subscription";
    return NOT_PAID(payload.entitled === null ? "unknown" : reason, config.plan);
  } catch {
    return NOT_PAID("error", config.plan);
  } finally {
    clearTimeout(timer);
  }
}
