/**
 * The admin panel's data, collected server-side.
 *
 * WHY THIS EXISTS
 * ---------------
 * Olympus is a factory: a gateway it does not own, a build runner that lives on
 * the host, a queue neither of them can see from the browser, and a set of
 * posture rules that only show up as a failure much later. Every one of those has
 * a script (`factory/doctor.py`, `gateway-edge-check.py`, `build-model-check.py`,
 * `build-runner-list`, `prune-builds`) and none of them is reachable from the UI,
 * so the person who notices "generation does nothing" is not the person who can
 * run them.
 *
 * This module is what `/admin` renders: the same facts, read in one pass, each
 * with the state that makes it actionable. It is **read-only** — nothing here
 * writes settings, starts a container or spends a turn except the one probe in
 * `probeGateway`, which asks the gateway for its own model list (the cheapest
 * request it answers, and the one that says whether generation can work at all).
 *
 * The rules that are *policy* rather than configuration are checked here in code
 * (`classifyGatewayUrl`), because the whole estate documents them once and a
 * deployment drifts by copying an address: the gateway's own port `20128` answers
 * on its host's loopback and bridge alone, and the routable door is the SSO proxy
 * on `20129`.
 */

import { existsSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import {
  RUNNER_STALE_SECONDS,
  buildQueueDir,
  readRunnerState,
  recentBuildStatuses,
  type BuildStatus,
  type RunnerState,
} from "./build-queue";
import { readAuthConfig, type Session } from "./auth";
import { loadRepoEnv, repoRoot } from "./env";
import { isPlaceholderSecret, listModels, readConfig, resolveModel, type OmniRouteConfig } from "./omniroute";

export type CheckState = "ok" | "warn" | "fail";

export type AdminCheck = {
  id: string;
  title: string;
  state: CheckState;
  detail: string;
  /** What to do about it, when the detail is a symptom rather than a fix. */
  hint?: string;
};

export type BuildSummary = {
  slug: string;
  bytes: number;
  modifiedAt: string | null;
  /** `site.zip`: a website packaged into something a publish can stage. */
  site: boolean;
  /** `project.zip`: an app packaged into a runnable image (with its manifest). */
  project: boolean;
  /** `plan.json`: the plan the build was manufactured from. */
  plan: boolean;
};

export type GatewayProbe = {
  ok: boolean;
  latencyMs: number;
  models: number | null;
  combos: number | null;
  providers: number | null;
  error: string | null;
};

export type AdminStatus = {
  generatedAt: string;
  gateway: {
    baseUrl: string;
    model: string;
    door: boolean;
    doorDetail: string;
    keyConfigured: boolean;
    probe: GatewayProbe;
    resolvedModel: string | null;
    resolution: string | null;
  };
  runner: RunnerState;
  queue: {
    /** Jobs the runner has claimed and not finished. There is no "queued" state on disk: a request becomes a status when the runner claims it. */
    running: number;
    finished: number;
    recent: Array<
      Pick<
        BuildStatus,
        "job" | "state" | "action" | "slug" | "title" | "updatedAt" | "message" | "publishedUrl" | "previewUrl"
      >
    >;
  };
  builds: BuildSummary[];
  paths: { queue: string; builds: string; data: string; siteSuffix: string };
  auth: {
    oidc: boolean;
    issuer: string;
    allowedGroups: string[];
    adminGroups: string[];
    /** True when the panel is visible to every Studio user. */
    adminOpen: boolean;
    accessTokenRequired: boolean;
    viewer: string | null;
  };
  tenancy: { controlPlane: boolean; internalToken: boolean };
  checks: AdminCheck[];
};

/* ---- policy: which gateway address is the door -------------------------- */

/**
 * The estate's rule, applied to whatever `OMNIROUTE_BASE_URL` says.
 *
 * `20129` is the SSO proxy in front of the gateway, which exempts `/v1` for API
 * clients; `20128` is the gateway itself, published on its host's loopback and
 * bridge alone, so from anywhere else it resolves to nothing — and *inside a
 * container* to the caller itself, which is how a rebuild once turned every
 * generation into ECONNREFUSED with an empty gateway log.
 *
 * Loopback on `20129` is accepted as well as the LAN address: on the gateway's
 * own host that names the same door. What is never accepted is loopback on
 * `20128`, because that is the caller.
 */
export function classifyGatewayUrl(baseUrl: string): { door: boolean; detail: string } {
  let url: URL;
  try {
    url = new URL(baseUrl);
  } catch {
    return {
      door: false,
      detail: `"${baseUrl}" is not a URL Studio can dial.`,
    };
  }

  const host = url.hostname.toLowerCase();
  const port = url.port || (url.protocol === "https:" ? "443" : "80");

  if (port === "20129") {
    return {
      door: true,
      detail: `${host}:${port} is the SSO proxy in front of the gateway — the routable door, and the one that exempts /v1.`,
    };
  }

  if (port === "20128") {
    const loopback = host === "127.0.0.1" || host === "localhost" || host === "::1";
    return {
      door: false,
      detail: loopback
        ? `${host}:${port} is this container's own loopback: the gateway's own port is not published here, so every call would reach Studio itself.`
        : `${host}:${port} is the gateway's own port, published on the gateway host's loopback and bridge alone. Nothing off that host can dial it.`,
    };
  }

  return {
    door: false,
    detail: `${host}:${port} is neither the gateway's own port (20128) nor the SSO proxy in front of it (20129).`,
  };
}

/* ---- reads -------------------------------------------------------------- */

/** `STUDIO_BUILDS_DIR`, the packaged factory output. Read-only in compose. */
export function buildsDir(): string {
  loadRepoEnv();
  const configured = process.env.STUDIO_BUILDS_DIR?.trim();
  return configured || join(repoRoot(), "builds");
}

export function dataDir(): string {
  loadRepoEnv();
  const configured = process.env.STUDIO_DATA_DIR?.trim();
  return configured || join(repoRoot(), "data", "studio");
}

/** Directory bytes, computed rather than asked for: `du` is not in this image. */
function directoryBytes(dir: string, depth = 2): number {
  let total = 0;
  try {
    // turbopackIgnore: a runtime path (`STUDIO_BUILDS_DIR`, a bind mount), so the
    // tracer cannot scope it statically — see `lib/env.ts` for the same annotation.
    for (const entry of readdirSync(/* turbopackIgnore: true */ dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      try {
        if (entry.isDirectory()) {
          if (depth > 0) total += directoryBytes(path, depth - 1);
        } else {
          total += statSync(path).size;
        }
      } catch {
        // A file that vanished between readdir and stat is not an error here.
      }
    }
  } catch {
    return 0;
  }
  return total;
}

/**
 * The factory's output, newest first. One unreadable directory is skipped rather
 * than failing the panel: the list is a convenience, and a build tree the
 * container cannot stat is a fact worth showing with everything else, not a 500.
 */
export function listBuilds(dir: string = buildsDir(), limit = 50): BuildSummary[] {
  let entries;
  try {
    entries = readdirSync(/* turbopackIgnore: true */ dir, { withFileTypes: true });
  } catch {
    return [];
  }

  const builds: BuildSummary[] = [];
  for (const entry of entries) {
    if (!entry.isDirectory() || entry.name.startsWith(".")) continue;
    const path = join(dir, entry.name);
    let modifiedAt: string | null = null;
    try {
      modifiedAt = statSync(path).mtime.toISOString();
    } catch {
      modifiedAt = null;
    }

    // The artefacts are named per kind by the packagers, and their presence is
    // the whole difference between "files the agent wrote" and "something that
    // can be served or run" — so it is read from disk rather than inferred from
    // a status that may have been pruned.
    const present = (name: string): boolean => existsSync(join(path, name));

    builds.push({
      slug: entry.name,
      bytes: directoryBytes(path),
      modifiedAt,
      site: present("site.zip"),
      project: present("project.zip"),
      plan: present("plan.json"),
    });
  }

  builds.sort((left, right) => (right.modifiedAt ?? "").localeCompare(left.modifiedAt ?? ""));
  return builds.slice(0, Math.max(1, limit));
}

/**
 * One cheap request to the gateway: its own model list.
 *
 * `/v1/models` is the smallest thing the gateway answers that still proves the
 * path end to end — key accepted, provider pool reachable enough to enumerate.
 * It is not a *generation* probe (`scripts/build-model-check.py` does that, and
 * it costs a turn); it is the check that makes a red panel explain itself.
 *
 * It goes through `listModels` rather than fetching separately, so the catalog it
 * reads is the same one the model picker will use and this costs one request.
 * `fresh: true` because the panel's whole job is to report *now*: a cached list
 * from a minute ago is exactly the answer that hides a gateway that just went
 * down.
 */
export async function probeGateway(
  config: OmniRouteConfig,
  timeoutMs = 8_000,
): Promise<GatewayProbe> {
  const started = Date.now();
  const empty: GatewayProbe = { ok: false, latencyMs: 0, models: null, combos: null, providers: null, error: null };

  if (isPlaceholderSecret(config.apiKey)) {
    return { ...empty, error: "No gateway key is configured (OMNIROUTE_API_KEY)." };
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const models = await listModels(config, { fresh: true, signal: controller.signal });
    return {
      ok: true,
      latencyMs: Date.now() - started,
      models: models.length,
      combos: models.filter((model) => model.combo).length,
      providers: new Set(models.map((model) => model.provider)).size,
      error: null,
    };
  } catch (error) {
    const detail = error instanceof Error ? error.message : "unknown error";
    return {
      ...empty,
      latencyMs: Date.now() - started,
      error:
        controller.signal.aborted || detail.includes("aborted")
          ? `No answer within ${timeoutMs / 1000}s from ${config.baseUrl}.`
          : detail,
    };
  } finally {
    clearTimeout(timer);
  }
}

/* ---- the panel's own checks --------------------------------------------- */

function gatewayCheck(baseUrl: string): AdminCheck {
  const { door, detail } = classifyGatewayUrl(baseUrl);
  return {
    id: "gateway-door",
    title: "OMNIROUTE_BASE_URL names the door",
    state: door ? "ok" : "fail",
    detail,
    hint: door
      ? undefined
      : "Point it at the SSO proxy in front of the gateway — on another host http://192.168.1.46:20129/v1, on the gateway's host http://host.docker.internal:20129/v1. See .env.example and docs/gateway-sso.md.",
  };
}

function runnerCheck(runner: RunnerState): AdminCheck {
  if (runner.live) {
    return {
      id: "runner",
      title: "A build runner is alive",
      state: "ok",
      detail: runner.busyWith
        ? `Heartbeat ${runner.ageSeconds}s old; building ${runner.busyWith}.`
        : `Heartbeat ${runner.ageSeconds}s old; idle and ready.`,
    };
  }
  return {
    id: "runner",
    title: "A build runner is alive",
    state: "warn",
    detail:
      runner.ageSeconds === null
        ? "No heartbeat file, so nothing is watching the queue."
        : `Last heartbeat ${runner.ageSeconds}s ago (stale after ${RUNNER_STALE_SECONDS}s).`,
    hint: "Install it with `scripts/install-build-runner.sh` and start it with `sudo systemctl start olympus-build-runner`. Queueing a build is refused while no runner is alive.",
  };
}

function buildsCheck(builds: BuildSummary[]): AdminCheck {
  if (builds.length === 0) {
    return {
      id: "builds",
      title: "Factory output",
      state: "warn",
      detail: "No builds are visible from here.",
      hint: `Check that ${buildsDir()} is the directory the runner writes to (STUDIO_BUILDS_DIR).`,
    };
  }
  const newest = builds[0];
  const megabytes = builds.reduce((total, build) => total + build.bytes, 0) / (1024 * 1024);
  return {
    id: "builds",
    title: "Factory output",
    state: "ok",
    detail: `${builds.length} build(s), ${megabytes.toFixed(1)} MB; newest is ${newest.slug}${newest.modifiedAt ? ` (${newest.modifiedAt})` : ""}.`,
  };
}

function authCheck(
  config: ReturnType<typeof readAuthConfig>,
  session: Session | null,
): AdminCheck {
  if (!config) {
    return {
      id: "auth",
      title: "Studio sign-in",
      state: "warn",
      detail: "OIDC is not configured, so Studio runs as a single-operator tool.",
      hint: "Set OIDC_ISSUER_URL, OIDC_CLIENT_ID and OIDC_CLIENT_SECRET, then `make studio-oidc` to register the callback.",
    };
  }
  const groups = config.allowedGroups.length ? config.allowedGroups.join(", ") : "any authenticated user";
  return {
    id: "auth",
    title: "Studio sign-in",
    state: "ok",
    detail: `Cerulean Authentik at ${config.issuer}; Studio is open to ${groups}. Signed in as ${session?.email ?? session?.name ?? "an unknown identity"}.`,
  };
}

function adminPolicyCheck(config: ReturnType<typeof readAuthConfig>): AdminCheck {
  if (!config) {
    return {
      id: "admin-policy",
      title: "Who can see this panel",
      state: "warn",
      detail: "OIDC is off, so every visitor is an operator and this panel is open to them.",
    };
  }
  if (config.adminGroups.length === 0) {
    return {
      id: "admin-policy",
      title: "Who can see this panel",
      state: "warn",
      detail: "OLYMPUS_ADMIN_GROUPS is empty, so every Studio user can read this panel.",
      hint: "Set OLYMPUS_ADMIN_GROUPS to the Authentik group(s) that should administer the deployment, and re-sign-in.",
    };
  }
  return {
    id: "admin-policy",
    title: "Who can see this panel",
    state: "ok",
    detail: `Restricted to ${config.adminGroups.join(", ")}.`,
  };
}

function tenancyCheck(): AdminCheck {
  const url = process.env.CONTROL_PLANE_INTERNAL_URL?.trim() ?? "";
  const token = process.env.CONTROL_INTERNAL_TOKEN?.trim() ?? "";
  if (url && token) {
    return {
      id: "tenancy",
      title: "Per-user gateway keys",
      state: "ok",
      detail: `Each turn is spent on the signed-in user's own key; quota is checked before it and usage recorded after (control plane at ${url}).`,
    };
  }
  return {
    id: "tenancy",
    title: "Per-user gateway keys",
    state: "warn",
    detail: "Studio spends the one shared OMNIROUTE_API_KEY for every user.",
    hint: "Set CONTROL_PLANE_INTERNAL_URL and CONTROL_INTERNAL_TOKEN to bill and quota per user (Distro's control plane).",
  };
}

/* ---- the collection ----------------------------------------------------- */

export async function collectStatus(session: Session | null = null): Promise<AdminStatus> {
  const config = readAuthConfig();
  const gatewayConfig = readConfig();
  const probe = await probeGateway(gatewayConfig);

  const runner = readRunnerState();
  const statuses = recentBuildStatuses(25);
  const builds = listBuilds();

  let resolvedModel: string | null = null;
  let resolution: string | null = null;
  if (probe.ok) {
    try {
      // Cached by the probe's own fresh read, so this is not a second request.
      const models = await listModels(gatewayConfig);
      const resolved = resolveModel(gatewayConfig, models, "");
      resolvedModel = resolved.model;
      resolution = resolved.reason ?? null;
    } catch {
      resolvedModel = null;
    }
  }

  const gateway = {
    baseUrl: gatewayConfig.baseUrl,
    model: gatewayConfig.model,
    door: classifyGatewayUrl(gatewayConfig.baseUrl).door,
    doorDetail: classifyGatewayUrl(gatewayConfig.baseUrl).detail,
    keyConfigured: !isPlaceholderSecret(gatewayConfig.apiKey),
    probe,
    resolvedModel,
    resolution,
  };

  const queue = {
    running: statuses.filter((status) => status.state === "running").length,
    finished: statuses.filter((status) => status.state !== "running").length,
    recent: statuses.slice(0, 10).map((status) => ({
      job: status.job,
      state: status.state,
      action: status.action,
      slug: status.slug,
      title: status.title,
      updatedAt: status.updatedAt ?? status.finishedAt ?? status.startedAt ?? status.requestedAt,
      message: status.message,
      publishedUrl: status.publishedUrl,
      previewUrl: status.previewUrl,
    })),
  };

  const auth = {
    oidc: config !== null,
    issuer: config?.issuer ?? "",
    allowedGroups: config?.allowedGroups ?? [],
    adminGroups: config?.adminGroups ?? [],
    adminOpen: !config || config.adminGroups.length === 0,
    accessTokenRequired: !isPlaceholderSecret(process.env.STUDIO_ACCESS_TOKEN?.trim() ?? ""),
    viewer: session?.email ?? session?.name ?? null,
  };

  const checks: AdminCheck[] = [
    gatewayCheck(gatewayConfig.baseUrl),
    {
      id: "gateway-reachable",
      title: "The gateway answers",
      state: probe.ok ? "ok" : "fail",
      detail: probe.ok
        ? `${probe.models} model(s) across ${probe.providers} provider(s), ${probe.combos} auto combo(s), answered in ${probe.latencyMs} ms.`
        : (probe.error ?? "The gateway did not answer."),
      hint: probe.ok
        ? undefined
        : "A gateway that is up but refuses the key is a credential problem; one that does not answer is a routing one. `make gateway-edge-check` walks the name link by link.",
    },
    {
      id: "gateway-key",
      title: "A gateway key is configured",
      state: gateway.keyConfigured ? "ok" : "fail",
      detail: gateway.keyConfigured
        ? "OMNIROUTE_API_KEY is set (the value is never sent to the browser)."
        : "OMNIROUTE_API_KEY is empty or still a placeholder.",
      hint: gateway.keyConfigured ? undefined : "Copy the gateway's own key from its dashboard (behind Authentik) into .env and restart Studio.",
    },
    {
      id: "model",
      title: "The configured model is linked in",
      state: !probe.ok ? "warn" : resolution ? "warn" : "ok",
      detail: !probe.ok
        ? `Cannot tell while the gateway is unreachable (configured: ${gatewayConfig.model}).`
        : resolution
          ? resolution
          : `${gatewayConfig.model} is linked in; a turn that asks for nothing else uses ${resolvedModel}.`,
      hint: resolution
        ? "`auto/coding` is OmniRoute's own router and survives a provider being unlinked; a provider-specific id does not."
        : undefined,
    },
    runnerCheck(runner),
    buildsCheck(builds),
    authCheck(config, session),
    adminPolicyCheck(config),
    tenancyCheck(),
  ];

  return {
    generatedAt: new Date().toISOString(),
    gateway,
    runner,
    queue,
    builds,
    paths: {
      queue: buildQueueDir(),
      builds: buildsDir(),
      data: dataDir(),
      siteSuffix: (process.env.SITE_HOST_SUFFIX ?? "").trim(),
    },
    auth,
    tenancy: {
      controlPlane: Boolean(process.env.CONTROL_PLANE_INTERNAL_URL?.trim()),
      internalToken: Boolean(process.env.CONTROL_INTERNAL_TOKEN?.trim()),
    },
    checks,
  };
}

/** The panel's summary line: the worst state among the checks. */
export function worstState(checks: AdminCheck[]): CheckState {
  if (checks.some((check) => check.state === "fail")) return "fail";
  if (checks.some((check) => check.state === "warn")) return "warn";
  return "ok";
}
