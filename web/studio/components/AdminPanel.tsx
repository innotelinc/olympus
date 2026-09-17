"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { AdminStatus, AdminCheck, CheckState } from "@/lib/admin";

/**
 * The deployment panel.
 *
 * Everything on it comes from one call to `/api/admin/status`, and it is read-only:
 * nothing here sets a variable, restarts a container or spends a turn. That is the
 * point — the failures this panel exists for ("generation does nothing", "the
 * queue is empty", "the runner is gone") each have a *script* that already answers
 * them, and the people who hit them are not the people who remember the script
 * names.
 *
 * It polls, because two of the things it shows move on their own: a runner
 * heartbeat ages, and a job finishes. Twenty seconds is chosen so the panel can be
 * left open during a build without asking the gateway for its model list often
 * enough to matter — the probe is one request, not a generation.
 */

const REFRESH_MS = 20_000;

/** The token `Studio` keeps for header-gated deployments; the panel sends the same one. */
const STORAGE_KEY = "studio.token";

function tokenFromStorage(): string {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

/** Fixed-width UTC, so a server render and a client render agree. */
function timestampLabel(value: string | null): string {
  if (!value) return "—";
  return value.slice(0, 19).replace("T", " ");
}

function relativeLabel(value: string | null, now: number): string {
  if (!value) return "—";
  const at = Date.parse(value);
  if (!Number.isFinite(at)) return "—";
  const seconds = Math.max(0, Math.round((now - at) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86_400)}d ago`;
}

function bytesLabel(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const STATE_LABEL: Record<CheckState, string> = {
  ok: "ok",
  warn: "attention",
  fail: "failing",
};

/** Failures first: a panel that lists ten greens before the one red is a scroll, not a report. */
function ordered(checks: AdminCheck[]): AdminCheck[] {
  const rank: Record<CheckState, number> = { fail: 0, warn: 1, ok: 2 };
  return [...checks].sort((left, right) => rank[left.state] - rank[right.state]);
}

export default function AdminPanel({ viewer }: { viewer: string | null }) {
  const [status, setStatus] = useState<AdminStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  // A ref, so the polling effect does not re-subscribe every time a fetch resolves.
  const inFlight = useRef(false);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    try {
      const headers: Record<string, string> = {};
      const token = tokenFromStorage();
      if (token) headers["x-studio-token"] = token;

      const response = await fetch("/api/admin/status", { headers, cache: "no-store" });
      if (!response.ok) {
        let detail = `The panel could not read the deployment state (HTTP ${response.status}).`;
        try {
          const payload = (await response.json()) as { error?: string };
          if (payload.error) detail = payload.error;
        } catch {
          /* a body that is not JSON leaves the status line as the message */
        }
        throw new Error(detail);
      }

      setStatus((await response.json()) as AdminStatus);
      setError(null);
    } catch (thrown) {
      setError(thrown instanceof Error ? thrown.message : "Could not read the deployment state.");
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  // One clock for every relative time on the page, so two rows a second apart
  // cannot disagree about how old they are.
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const checks = status ? ordered(status.checks) : [];
  const worst: CheckState = checks.some((check) => check.state === "fail")
    ? "fail"
    : checks.some((check) => check.state === "warn")
      ? "warn"
      : "ok";

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          Studio <span>Olympus</span>
        </div>
        <span className="chip">Deployment panel</span>
        <span className="topbar-spacer" />
        <span className={`pill dot-${worst}`}>
          <span className={`dot dot-${worst}`} />
          {status ? STATE_LABEL[worst] : busy ? "reading" : "no data"}
        </span>
        <span className="hint">
          {status ? `read ${relativeLabel(status.generatedAt, now)}` : "waiting"} · refreshes every{" "}
          {REFRESH_MS / 1000}s
        </span>
        <button className="ghost" onClick={() => void refresh()} disabled={busy}>
          {busy ? "Reading…" : "Refresh"}
        </button>
        <a className="ghost" href="/">
          Builder
        </a>
      </header>

      <div className="admin-body">
        {error ? (
          <div className="alert danger">
            <span>{error}</span>
          </div>
        ) : null}

        {!status && !error ? <div className="empty">Reading the deployment…</div> : null}

        {status ? (
          <>
            <section className="admin-block">
              <div className="admin-head">
                <h2>Checks</h2>
                <span className="hint">
                  {viewer ? `viewer ${viewer}` : "no identity to attribute reads to"}
                </span>
              </div>
              <ul className="admin-checks">
                {checks.map((check) => (
                  <li key={check.id} className={`admin-check check-${check.state}`}>
                    <span className={`dot dot-${check.state}`} />
                    <div>
                      <div className="admin-check-title">
                        {check.title}
                        <span className="hint"> — {STATE_LABEL[check.state]}</span>
                      </div>
                      <div className="admin-check-detail">{check.detail}</div>
                      {check.hint ? <div className="admin-check-hint">{check.hint}</div> : null}
                    </div>
                  </li>
                ))}
              </ul>
            </section>

            <div className="admin-columns">
              <section className="admin-block">
                <div className="admin-head">
                  <h2>Gateway</h2>
                  <span className={`pill dot-${status.gateway.probe.ok ? "ok" : "fail"}`}>
                    <span className={`dot dot-${status.gateway.probe.ok ? "ok" : "fail"}`} />
                    {status.gateway.probe.ok ? `${status.gateway.probe.latencyMs} ms` : "unreachable"}
                  </span>
                </div>
                <dl className="admin-facts">
                  <dt>Base URL</dt>
                  <dd className="mono">{status.gateway.baseUrl}</dd>
                  <dt>Configured model</dt>
                  <dd className="mono">{status.gateway.model}</dd>
                  <dt>Used when nothing is chosen</dt>
                  <dd className="mono">{status.gateway.resolvedModel ?? "unknown"}</dd>
                  <dt>Key</dt>
                  <dd>{status.gateway.keyConfigured ? "configured" : "missing"}</dd>
                  <dt>Catalogue</dt>
                  <dd>
                    {status.gateway.probe.ok
                      ? `${status.gateway.probe.models} model(s), ${status.gateway.probe.providers} provider(s), ${status.gateway.probe.combos} auto combo(s)`
                      : (status.gateway.probe.error ?? "not read")}
                  </dd>
                </dl>
              </section>

              <section className="admin-block">
                <div className="admin-head">
                  <h2>Runner and queue</h2>
                  <span className={`pill dot-${status.runner.live ? "ok" : "warn"}`}>
                    <span className={`dot dot-${status.runner.live ? "ok" : "warn"}`} />
                    {status.runner.live
                      ? status.runner.busyWith
                        ? `building ${status.runner.busyWith}`
                        : "idle"
                      : "not running"}
                  </span>
                </div>
                <dl className="admin-facts">
                  <dt>Heartbeat</dt>
                  <dd>
                    {status.runner.ageSeconds === null
                      ? "never seen"
                      : `${status.runner.ageSeconds}s ago`}
                    {status.runner.host ? ` on ${status.runner.host}` : ""}
                    {status.runner.pid ? ` (pid ${status.runner.pid})` : ""}
                  </dd>
                  <dt>Queue</dt>
                  <dd>
                    {status.queue.running} running, {status.queue.finished} recently finished
                  </dd>
                  <dt>Queue directory</dt>
                  <dd className="mono">{status.paths.queue}</dd>
                  <dt>Build output</dt>
                  <dd className="mono">{status.paths.builds}</dd>
                  <dt>Site suffix</dt>
                  <dd className="mono">{status.paths.siteSuffix || "publishing not configured"}</dd>
                </dl>
              </section>
            </div>

            <section className="admin-block">
              <div className="admin-head">
                <h2>Recent jobs</h2>
                <span className="hint">
                  newest first, running jobs pinned to the top — the same order the builder shows
                </span>
              </div>
              {status.queue.recent.length === 0 ? (
                <div className="hint">Nothing has been queued on this deployment.</div>
              ) : (
                <div className="admin-table-scroll">
                  <table className="admin-table">
                    <thead>
                      <tr>
                        <th>Job</th>
                        <th>Action</th>
                        <th>State</th>
                        <th>App</th>
                        <th>Updated</th>
                        <th>Where it got to</th>
                      </tr>
                    </thead>
                    <tbody>
                      {status.queue.recent.map((job) => (
                        <tr key={job.job}>
                          <td className="mono">{job.job.slice(0, 8)}</td>
                          <td>{job.action}</td>
                          <td>
                            <span className={`pill dot-${stateClass(job.state)}`}>
                              <span className={`dot dot-${stateClass(job.state)}`} />
                              {job.state}
                            </span>
                          </td>
                          <td className="mono">{job.slug ?? "—"}</td>
                          <td title={timestampLabel(job.updatedAt)}>
                            {relativeLabel(job.updatedAt, now)}
                          </td>
                          <td className="admin-message">
                            {job.publishedUrl ? (
                              <a href={job.publishedUrl} target="_blank" rel="noreferrer">
                                {job.publishedUrl}
                              </a>
                            ) : job.previewUrl ? (
                              <span className="mono">{job.previewUrl}</span>
                            ) : (
                              job.message || "—"
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section className="admin-block">
              <div className="admin-head">
                <h2>Built apps</h2>
                <span className="hint">
                  {status.builds.length} visible from this container ({status.paths.builds})
                </span>
              </div>
              {status.builds.length === 0 ? (
                <div className="hint">
                  No build directories are readable here. If builds exist on the host, this
                  container&apos;s <span className="mono">STUDIO_BUILDS_DIR</span> is pointing
                  elsewhere or the mount is read-restricted.
                </div>
              ) : (
                <div className="admin-table-scroll">
                  <table className="admin-table">
                    <thead>
                      <tr>
                        <th>App</th>
                        <th>Size</th>
                        <th>Plan</th>
                        <th>Packaged</th>
                        <th>Last written</th>
                      </tr>
                    </thead>
                    <tbody>
                      {status.builds.map((build) => (
                        <tr key={build.slug}>
                          <td className="mono">{build.slug}</td>
                          <td>{bytesLabel(build.bytes)}</td>
                          <td>{build.plan ? "yes" : "—"}</td>
                          <td>
                            {build.site
                              ? "site.zip"
                              : build.project
                                ? "project.zip"
                                : "source only"}
                          </td>
                          <td title={timestampLabel(build.modifiedAt)}>
                            {relativeLabel(build.modifiedAt, now)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section className="admin-block">
              <div className="admin-head">
                <h2>Access and tenancy</h2>
              </div>
              <dl className="admin-facts">
                <dt>Sign-in</dt>
                <dd>
                  {status.auth.oidc
                    ? `Cerulean Authentik — ${status.auth.issuer}`
                    : "OIDC off (single-operator deployment)"}
                </dd>
                <dt>Studio allowed groups</dt>
                <dd>
                  {status.auth.allowedGroups.length > 0
                    ? status.auth.allowedGroups.join(", ")
                    : "any authenticated user"}
                </dd>
                <dt>Admin groups</dt>
                <dd>
                  {status.auth.adminGroups.length > 0
                    ? status.auth.adminGroups.join(", ")
                    : "every Studio user — this panel is open"}
                </dd>
                <dt>Access token</dt>
                <dd>
                  {status.auth.accessTokenRequired
                    ? "required (x-studio-token)"
                    : "not required"}
                </dd>
                <dt>Per-user keys</dt>
                <dd>
                  {status.tenancy.controlPlane && status.tenancy.internalToken
                    ? "on — each turn spends the signed-in user's key"
                    : "off — every turn spends the shared OMNIROUTE_API_KEY"}
                </dd>
              </dl>
            </section>
          </>
        ) : null}
      </div>
    </div>
  );
}

/** A build state as a dot colour: a cancelled build is neither a success nor a failure. */
function stateClass(state: string): CheckState | "muted" {
  if (state === "succeeded") return "ok";
  if (state === "failed") return "fail";
  if (state === "running") return "warn";
  return "muted";
}
