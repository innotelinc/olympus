"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { BuildLog, BuildStatus, RunnerState } from "@/lib/build-queue";
import { currentFileFrom, parseFiles, type GeneratedFile } from "@/lib/files";
import type { ProjectKind } from "@/lib/projects";
import CodeView from "./CodeView";

// No `Preview` here on purpose. Both kinds are React: an app's client needs a build
// and a server before it renders at all, and a website's needs a build. The
// sandboxed frame could only ever show a blank page, so the Preview tab explains
// what each kind has to go through instead of pretending. `components/Preview.tsx`
// still exists — it is what the frame was, and what it will be again if a preview
// ever has something real to point at.

type Status = "idle" | "streaming" | "error";
type Tab = "preview" | "code";

const EXAMPLES = [
  "A weight-loss tracker: enter a weight each morning and see the trend against a goal.",
  "A recipe box: save recipes with ingredients and steps, and search them by name.",
  "A shift rota where each person's shifts are entered once and the week is shown as a grid.",
  "A habit tracker with one row per habit and a tick for each day of the week.",
];

/**
 * What each kind actually produces, said as the difference rather than as a name.
 *
 * The two are not variations of one thing. An app keeps state: it has a database and
 * an API, so what someone enters today is there tomorrow. A website is static —
 * there is nowhere for an entry to be written down, and a form on one is decoration.
 * An operator picking for the first time has no way to know that from the labels
 * alone, and picking wrong costs a whole generation.
 */
const KIND_HELP: Record<ProjectKind, string> = {
  app: "A full-stack application: React client, its own API and a SQLite database, run as its own container. It saves what people enter. Package it, run it, publish it on a name.",
  website: "A React + TypeScript site (Vite) with no server and no data — a brochure, a landing page, a portfolio. It is packaged into dist/ and served as static files.",
};

const STORAGE_KEY = "studio.token";

type SavedApp = {
  id: string;
  title: string;
  /** Absent on anything saved before the split; the server reports "app" for those. */
  kind: ProjectKind;
  updatedAt: string;
  fileCount: number;
};

/** Fixed-width UTC, so server-rendered markup matches the client hydration pass. */
function timestampLabel(value: string): string {
  return value.slice(0, 16).replace("T", " ");
}

/** A default app title from the instruction that produced it. */
function titleFromPrompt(prompt: string): string {
  const line = prompt.trim().split(/\n/)[0] ?? "";
  return line.trim().slice(0, 60);
}

/**
 * The name a slug would publish under, for the hint that explains a staged build.
 *
 * The slug comes from the runner's own status, not from the title on screen, so
 * the preview names the same host the publish would — a mismatch here is the
 * operator finding out later that `<name>.example` is a different site.
 */
function slugPreview(slug: string | null, suffix: string): string {
  return slug && suffix ? `https://${slug}.${suffix}` : "a name under the site suffix";
}

/** m:ss elapsed label. */
function clock(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

/** Human-readable byte count for the progress line. */
function kilobytes(bytes: number): string {
  return bytes >= 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${bytes} B`;
}

/**
 * "running — 1:04" while a factory build is in flight, else the terminal state.
 *
 * The elapsed clock comes from the runner's own `started_at`, not from when this
 * page noticed the job: a reload halfway through a build should show the build's
 * true age, not the tab's.
 */
function buildStateLabel(build: BuildStatus): string {
  if (build.state !== "running") {
    if (build.state === "succeeded") return "succeeded";
    // A stop the operator asked for is not a failure, and labelling it one is
    // how real failures stop being read.
    return build.state === "cancelled" ? "cancelled" : "failed";
  }

  const since = build.startedAt ? Date.parse(build.startedAt) : Number.NaN;
  if (!Number.isFinite(since)) return "running";

  return `running — ${clock(Math.max(0, Math.round((Date.now() - since) / 1000)))}`;
}

async function readFailure(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { error?: unknown };
    if (typeof payload.error === "string" && payload.error) return payload.error;
  } catch {
    /* not JSON — fall through to the status line */
  }
  return `Request failed with status ${response.status}.`;
}

/**
 * The rate limit as `/api/settings/ratelimit` reports it.
 *
 * Declared here rather than imported from `lib/ratelimit`, which reads the
 * filesystem on the server — the client bundle must not pull node:fs in for a
 * type.
 */
type RateLimitView = {
  /** Effective limit; null means switched off. */
  limit: number | null;
  source: "override" | "env" | "default";
  envLimit: number | null;
  envRaw: string;
  override: number | null;
  max: number;
};

/** One line saying what the limit is right now and which layer decided it. */
function describeRateLimit(state: RateLimitView): string {
  const effective = state.limit === null ? "Off" : `${state.limit}/min`;
  const from =
    state.source === "override"
      ? "set here"
      : state.source === "env"
        ? "from .env"
        : "the deployment default";
  const envNote = state.source === "override" && state.envRaw ? ` (.env says ${state.envRaw})` : "";
  return `Effective: ${effective} — ${from}${envNote}`;
}

/**
 * Where a build is, derived from what the stream has actually produced rather
 * than from a timer: nothing is invented, so the steps are all real.
 */
type BuildPhase = "connecting" | "thinking" | "writing" | "finalizing";

function phaseOf(input: { opened: boolean; bytes: number; writing: boolean }): BuildPhase {
  if (!input.opened) return "connecting";
  if (input.writing) return "writing";
  return input.bytes === 0 ? "thinking" : "finalizing";
}

const PHASE_LABEL: Record<BuildPhase, string> = {
  connecting: "Sending the instruction",
  thinking: "Model is responding",
  writing: "Writing files",
  finalizing: "Finishing files",
};

const PHASE_ORDER: BuildPhase[] = ["connecting", "thinking", "writing", "finalizing"];

export default function Studio({
  user = null,
  siteSuffix = "",
}: {
  user?: string | null;
  /** The domain a published site answers under, e.g. `studio.olympus.innotel.us`. */
  siteSuffix?: string;
}) {
  const [prompt, setPrompt] = useState("");
  const [files, setFiles] = useState<GeneratedFile[]>([]);
  const [raw, setRaw] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  // On the code, not the preview: with nothing to preview, the files are the only
  // thing on this screen that is worth looking at, and the build output on the right
  // says where they got to.
  const [tab, setTab] = useState<Tab>("code");
  // What is being built. It decides the system prompt AND the delivery path, so it
  // is part of the build, not a display option — it is sent with every turn and
  // saved with the project.
  const [kind, setKind] = useState<ProjectKind>("app");
  const [turns, setTurns] = useState(0);
  const [token, setToken] = useState("");
  const [showSettings, setShowSettings] = useState(false);

  // Live progress, driven by the stream itself: which file is open, how much
  // of it has arrived, when the build started, and when data last moved.
  const [currentFile, setCurrentFile] = useState<{ path: string; bytes: number } | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [lastChunkAt, setLastChunkAt] = useState<number | null>(null);
  const [streamOpened, setStreamOpened] = useState(false);
  const [byteCount, setByteCount] = useState(0);
  const [queued, setQueued] = useState(false);
  const [nowTick, setNowTick] = useState(0);

  // The generation rate limit, as an operator setting (deployment-wide).
  const [rateLimit, setRateLimit] = useState<RateLimitView | null>(null);
  const [rateLimitDraft, setRateLimitDraft] = useState("");
  const [rateLimitBusy, setRateLimitBusy] = useState(false);
  const [rateLimitError, setRateLimitError] = useState<string | null>(null);

  // Saved-app library. Kept server-side under the signed-in identity, so the
  // same account gets the same apps on any browser.
  const [savedApps, setSavedApps] = useState<SavedApp[]>([]);
  const [activeAppId, setActiveAppId] = useState<string | null>(null);
  const [appTitle, setAppTitle] = useState("");
  const [libraryBusy, setLibraryBusy] = useState(false);
  const [libraryNote, setLibraryNote] = useState<string | null>(null);
  const [libraryError, setLibraryError] = useState<string | null>(null);

  // The factory build of this app. There is no socket to the runner — Studio
  // polls the status file it writes, and that file is the entire conversation.
  const [build, setBuild] = useState<BuildStatus | null>(null);
  const [buildHistory, setBuildHistory] = useState<BuildStatus[]>([]);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [runner, setRunner] = useState<RunnerState | null>(null);
  const [buildBusy, setBuildBusy] = useState(false);
  const [buildError, setBuildError] = useState<string | null>(null);
  // Publishing is its own in-flight state, not a flavour of building: the two go
  // to different places and a single "busy" would make one button's spinner
  // explain the other's.
  const [publishBusy, setPublishBusy] = useState(false);
  // The log of the build being shown, fetched on its own cadence: it is tens of
  // kilobytes, so it does not belong in the status poll that runs every 3s.
  const [buildLog, setBuildLog] = useState<BuildLog | null>(null);
  // Visible by default: a build's output is the answer to "what is it doing?",
  // and hiding the thing that moves is how a build looks stalled.
  const [logHidden, setLogHidden] = useState(false);
  const [logBusy, setLogBusy] = useState(false);

  const abortRef = useRef<AbortController | null>(null);

  // Mirrors of state the streaming core reads, so a queued instruction can
  // start with the files the previous turn actually produced (state updates
  // have not rendered yet the moment a turn ends) and so continuous prompting
  // never reads a stale closure.
  const busyRef = useRef(false);
  const pendingRef = useRef<string | null>(null);
  const filesRef = useRef<GeneratedFile[]>([]);
  const promptRef = useRef("");
  const tokenRef = useRef("");
  const activeAppRef = useRef<string | null>(null);
  const appTitleRef = useRef("");
  // A mirror of `kind` for the streaming core, which reads its inputs from refs so
  // a queued turn starts with what the previous one used rather than what the
  // render closure captured.
  const kindRef = useRef<ProjectKind>("app");

  useEffect(() => {
    try {
      const stored = window.sessionStorage.getItem(STORAGE_KEY);
      if (stored) setToken(stored);
    } catch {
      /* storage unavailable — the token stays in memory for this session */
    }
  }, []);

  const streamedFiles = useMemo(() => parseFiles(raw), [raw]);
  const activeFiles = streamedFiles.length > 0 ? streamedFiles : files;

  // Keep the mirrors current. filesRef is also assigned directly at the moment
  // a turn produces files, because a queued turn starts before the next render.
  useEffect(() => {
    filesRef.current = files;
  }, [files]);
  useEffect(() => {
    promptRef.current = prompt;
  }, [prompt]);
  useEffect(() => {
    tokenRef.current = token;
  }, [token]);
  useEffect(() => {
    activeAppRef.current = activeAppId;
  }, [activeAppId]);
  useEffect(() => {
    appTitleRef.current = appTitle;
  }, [appTitle]);
  useEffect(() => {
    kindRef.current = kind;
  }, [kind]);

  // Re-render once a second while streaming so the elapsed counter and the
  // waiting indicator move without waiting for stream data.
  useEffect(() => {
    if (status !== "streaming") return;
    const id = window.setInterval(() => setNowTick((tick) => tick + 1), 1000);
    return () => window.clearInterval(id);
  }, [status]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const saveToken = useCallback((value: string) => {
    setToken(value);
    try {
      if (value) window.sessionStorage.setItem(STORAGE_KEY, value);
      else window.sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
  }, []);

  /** fetch carrying the access-token header the server expects, when one is set. */
  const request = useCallback(
    async (path: string, init: RequestInit = {}): Promise<Response> => {
      const headers: Record<string, string> = {
        ...((init.headers as Record<string, string> | undefined) ?? {}),
      };
      if (token) headers["x-studio-token"] = token;

      const response = await fetch(path, { ...init, headers });
      if (!response.ok) throw new Error(await readFailure(response));
      return response;
    },
    [token],
  );

  const listSavedApps = useCallback(async () => {
    try {
      const payload = (await (await request("/api/projects")).json()) as { projects?: SavedApp[] };
      setSavedApps(Array.isArray(payload.projects) ? payload.projects : []);
      setLibraryError(null);
    } catch (thrown) {
      setLibraryError(thrown instanceof Error ? thrown.message : "Could not load saved apps.");
    }
  }, [request]);

  useEffect(() => {
    void listSavedApps();
  }, [listSavedApps]);

  /** Read the runner's status file through the API. Never fetches a build itself. */
  const fetchBuild = useCallback(
    async (appId: string, job?: string) => {
      const query = job ? `?job=${encodeURIComponent(job)}` : "";
      const response = await request(`/api/projects/${encodeURIComponent(appId)}/build${query}`);
      const payload = (await response.json()) as {
        build?: BuildStatus | null;
        history?: BuildStatus[];
        runner?: RunnerState;
      };
      setBuild(payload.build ?? null);
      setBuildHistory(Array.isArray(payload.history) ? payload.history : []);
      setRunner(payload.runner ?? null);
      return payload.build ?? null;
    },
    [request],
  );

  // Pick up where the last build left off when an app is opened, so a reload
  // does not make a finished build look like one that never ran.
  useEffect(() => {
    if (!activeAppId) {
      setBuild(null);
      setBuildHistory([]);
      return;
    }

    void fetchBuild(activeAppId).catch(() => {
      /* the panel is an extra; a failure here never breaks the editor */
    });
  }, [activeAppId, fetchBuild]);

  /**
   * Read one build's log. Separate from the status poll because of its size, and
   * fetched for the build actually on screen — a history entry shows its own log,
   * not the newest one.
   */
  const fetchBuildLog = useCallback(
    async (appId: string, job: string) => {
      setLogBusy(true);
      try {
        const response = await request(
          `/api/projects/${encodeURIComponent(appId)}/build/log?job=${encodeURIComponent(job)}`,
        );
        const payload = (await response.json()) as BuildLog;
        setBuildLog(payload);
      } catch {
        // The log is an extra: the status, message and artifact are still on
        // screen, so a failed read must not turn into an error banner over them.
        setBuildLog(null);
      } finally {
        setLogBusy(false);
      }
    },
    [request],
  );

  // Poll only while something is actually running. A build is minutes long, so a
  // 3s cadence is responsive without being a busy loop. The log is refreshed on
  // the same tick — that movement is the point of watching a build at all.
  useEffect(() => {
    if (!activeAppId || build?.state !== "running") return;

    const id = window.setInterval(() => {
      void fetchBuild(activeAppId, build.job).catch(() => {
        /* transient — the next tick retries */
      });
      void fetchBuildLog(activeAppId, build.job);
    }, 3000);

    return () => window.clearInterval(id);
  }, [activeAppId, build?.state, build?.job, fetchBuild, fetchBuildLog]);

  // Load the running (or last) build's log once it is known, so opening an app
  // shows the build's output rather than an empty pane behind a button.
  useEffect(() => {
    if (!activeAppId || !build?.job) {
      setBuildLog(null);
      return;
    }
    void fetchBuildLog(activeAppId, build.job);
  }, [activeAppId, build?.job, fetchBuildLog]);

  // ---- rate limit (operator setting) ------------------------------------

  const loadRateLimit = useCallback(async () => {
    try {
      const payload = (await (await request("/api/settings/ratelimit")).json()) as {
        rateLimit?: RateLimitView;
      };
      if (!payload.rateLimit) throw new Error("Studio returned an unexpected settings payload.");
      setRateLimit(payload.rateLimit);
      setRateLimitError(null);
    } catch (thrown) {
      // Not fatal: the toggle simply stays hidden rather than blocking a build.
      setRateLimitError(thrown instanceof Error ? thrown.message : "Could not read the rate limit.");
    }
  }, [request]);

  useEffect(() => {
    void loadRateLimit();
  }, [loadRateLimit]);

  // The draft mirrors the live limit until the operator edits it.
  useEffect(() => {
    if (rateLimit && rateLimit.limit !== null) setRateLimitDraft(String(rateLimit.limit));
  }, [rateLimit]);

  const applyRateLimit = useCallback(
    async (body: { disabled: boolean; perMinute?: number } | { reset: true }) => {
      setRateLimitBusy(true);
      setRateLimitError(null);
      try {
        const payload = (await (
          await request("/api/settings/ratelimit", {
            method: "PUT",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(body),
          })
        ).json()) as { rateLimit?: RateLimitView };
        if (!payload.rateLimit) throw new Error("Studio returned an unexpected settings payload.");
        setRateLimit(payload.rateLimit);
      } catch (thrown) {
        setRateLimitError(thrown instanceof Error ? thrown.message : "Could not change the rate limit.");
      } finally {
        setRateLimitBusy(false);
      }
    },
    [request],
  );

  const persistApp = useCallback(
    async (input: {
      id?: string | null;
      title?: string;
      prompt?: string;
      files: GeneratedFile[];
      /** Omitted on a revision, which keeps the kind the app was created with. */
      kind?: ProjectKind;
    }) => {
      const response = await request("/api/projects", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          id: input.id ?? undefined,
          title: input.title ?? "",
          prompt: input.prompt ?? "",
          kind: input.kind,
          files: input.files.map((file) => ({ path: file.path, contents: file.contents })),
        }),
      });
      const payload = (await response.json()) as { project: SavedApp };
      return payload.project;
    },
    [request],
  );

  const saveApp = useCallback(async () => {
    if (activeFiles.length === 0 || status === "streaming") return;

    setLibraryBusy(true);
    setLibraryError(null);
    try {
      const app = await persistApp({
        id: activeAppId,
        title: appTitle || titleFromPrompt(prompt),
        prompt,
        files: activeFiles,
        // A saved app that has never existed sends its kind; an existing one keeps
        // whatever it was created as, and the server ignores this.
        kind: activeAppId ? undefined : kind,
      });
      setActiveAppId(app.id);
      setAppTitle(app.title);
      setLibraryNote(`Saved “${app.title}”.`);
      await listSavedApps();
    } catch (thrown) {
      setLibraryError(thrown instanceof Error ? thrown.message : "Could not save this app.");
    } finally {
      setLibraryBusy(false);
    }
  }, [activeAppId, activeFiles, appTitle, kind, listSavedApps, persistApp, prompt, status]);

  /**
   * Hand this build to the factory.
   *
   * The saved copy is refreshed first, so the spec always describes the build
   * on screen rather than whatever was last saved — an export that quietly
   * lagged the preview would be worse than no export at all.
   */
  const exportApp = useCallback(async () => {
    if (activeFiles.length === 0 || status === "streaming") return;

    setLibraryBusy(true);
    setLibraryError(null);
    setLibraryNote(null);

    try {
      const app = await persistApp({
        id: activeAppId,
        title: appTitle || titleFromPrompt(prompt),
        prompt,
        files: activeFiles,
        kind: activeAppId ? undefined : kind,
      });
      setActiveAppId(app.id);
      setAppTitle(app.title);
      await listSavedApps();

      // Not `request()`: the 409 that says "a spec already exists" is a
      // decision to put to the operator, not an error to show.
      const headers: Record<string, string> = { "content-type": "application/json" };
      if (token) headers["x-studio-token"] = token;

      const send = (overwrite: boolean) =>
        fetch(`/api/projects/${encodeURIComponent(app.id)}/export`, {
          method: "POST",
          headers,
          body: JSON.stringify({ overwrite }),
        });

      let response = await send(false);
      if (response.status === 409) {
        const conflict = (await response.json()) as { error?: string };
        const replace = window.confirm(
          `${conflict.error ?? "That spec already exists."}\n\nReplace it with this build?`,
        );
        if (!replace) {
          setLibraryNote(
            `Saved “${app.title}”. Export cancelled — the existing spec is untouched.`,
          );
          return;
        }
        response = await send(true);
      }

      const payload = (await response.json()) as {
        filename?: string;
        replaced?: boolean;
        next?: string;
        nextSteps?: string[];
        error?: string;
      };
      if (!response.ok) throw new Error(payload.error ?? `Export failed (${response.status}).`);

      // The next steps, as the spec itself states them. For a website the list is
      // longer than "make app" — it has to be packaged before it is a site — and
      // showing the real list is the difference between the handoff working and
      // the operator discovering the extra step after the build.
      const steps = payload.nextSteps ?? [];
      setLibraryNote(
        steps.length > 0
          ? `${payload.replaced ? "Replaced" : "Wrote"} build-requests/${payload.filename}. Next: ${steps.join(" ")}`
          : `${payload.replaced ? "Replaced" : "Wrote"} build-requests/${payload.filename} — continue with: ${payload.next ?? "make app"}.`,
      );
    } catch (thrown) {
      setLibraryError(thrown instanceof Error ? thrown.message : "Could not export this app.");
    } finally {
      setLibraryBusy(false);
    }
  }, [
    activeAppId,
    activeFiles,
    appTitle,
    kind,
    listSavedApps,
    persistApp,
    prompt,
    status,
    token,
  ]);

  /**
   * Export this build and run `make app` on it, then report what happened.
   *
   * The saved copy is refreshed first for the same reason the export does it: a
   * build that describes an older version of the app than the one on screen is a
   * build you cannot trust. The 409 is the same conflict the export raises, and
   * it is put to the operator rather than resolved silently — `replace` covers
   * both the spec and a previous `builds/<slug>`.
   */
  const runBuild = useCallback(async (publish = false) => {
    if (activeFiles.length === 0 || status === "streaming") return;

    setBuildBusy(true);
    setBuildError(null);
    setLibraryError(null);
    setLibraryNote(null);

    try {
      const app = await persistApp({
        id: activeAppId,
        title: appTitle || titleFromPrompt(prompt),
        prompt,
        files: activeFiles,
        kind: activeAppId ? undefined : kind,
      });
      setActiveAppId(app.id);
      setAppTitle(app.title);
      await listSavedApps();

      const headers: Record<string, string> = { "content-type": "application/json" };
      if (token) headers["x-studio-token"] = token;

      const send = (replace: boolean) =>
        fetch(`/api/projects/${encodeURIComponent(app.id)}/build`, {
          method: "POST",
          headers,
          body: JSON.stringify({ replace, publish }),
        });

      let response = await send(false);
      if (response.status === 409) {
        const conflict = (await response.json()) as { error?: string };
        const replace = window.confirm(
          `${conflict.error ?? "A spec or a previous build already exists."}\n\nReplace it and build now?`,
        );
        if (!replace) {
          setLibraryNote(`Saved “${app.title}”. Build cancelled — nothing was replaced.`);
          return;
        }
        response = await send(true);
      }

      const payload = (await response.json()) as {
        job?: string;
        spec?: string;
        runner?: RunnerState;
        error?: string;
      };
      if (!response.ok) throw new Error(payload.error ?? `Build failed to start (${response.status}).`);

      setRunner(payload.runner ?? null);
      if (payload.job) await fetchBuild(app.id, payload.job);
    } catch (thrown) {
      setBuildError(thrown instanceof Error ? thrown.message : "Could not start the build.");
    } finally {
      setBuildBusy(false);
    }
  }, [
    activeAppId,
    activeFiles,
    appTitle,
    fetchBuild,
    kind,
    listSavedApps,
    persistApp,
    prompt,
    status,
    token,
  ]);

  /**
   * Save, then hand over a zip.
   *
   * Saved first because the archive route works on a saved project — it is the
   * saved record that has an identity to scope the download to, and it is also
   * what makes the download match what is on screen rather than what was last
   * written. For a packaged website the server serves the archive the packaging
   * step produced; otherwise it zips these source files.
   *
   * Fetched as a blob rather than navigated to: the request has to carry the
   * access-token header when one is configured, and a navigation cannot.
   */
  const downloadZip = useCallback(async () => {
    if (activeFiles.length === 0 || status === "streaming") return;

    setLibraryBusy(true);
    setLibraryError(null);
    setLibraryNote(null);
    try {
      const app = await persistApp({
        id: activeAppId,
        title: appTitle || titleFromPrompt(prompt),
        prompt,
        files: activeFiles,
        kind: activeAppId ? undefined : kind,
      });
      setActiveAppId(app.id);
      setAppTitle(app.title);
      await listSavedApps();

      const response = await request(`/api/projects/${encodeURIComponent(app.id)}/archive`);
      const blob = await response.blob();
      const match = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "");
      const name = match?.[1] ?? `${app.title || "studio"}.zip`;

      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = name;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);

      setLibraryNote(
        `Downloaded ${name}.` +
          (kind === "website" && !build?.site
            ? " This build has not been packaged yet, so the archive holds the source — run “Build & publish” to get one with dist/ inside."
            : ""),
      );
    } catch (thrown) {
      setLibraryError(thrown instanceof Error ? thrown.message : "Could not download this build.");
    } finally {
      setLibraryBusy(false);
    }
  }, [
    activeAppId,
    activeFiles,
    appTitle,
    build?.site,
    kind,
    listSavedApps,
    persistApp,
    prompt,
    request,
    status,
  ]);

  /**
   * Put what is on screen on a name.
   *
   * Not a factory build with a flag: the runner is sent this project's files and
   * packages those. The distinction is the whole value of the button — a rebuild
   * would publish whatever the model produced this time, which is a different
   * build from the one being looked at.
   *
   * Saved first, because the request carries the saved files and a publish that
   * disagreed with the library would be a name pointing at a build nobody can
   * reopen.
   */
  const publishApp = useCallback(async () => {
    if (activeFiles.length === 0 || status === "streaming") return;

    setPublishBusy(true);
    setBuildError(null);
    setLibraryError(null);
    setLibraryNote(null);

    try {
      const app = await persistApp({
        id: activeAppId,
        title: appTitle || titleFromPrompt(prompt),
        prompt,
        files: activeFiles,
        kind: activeAppId ? undefined : kind,
      });
      setActiveAppId(app.id);
      setAppTitle(app.title);
      await listSavedApps();

      const headers: Record<string, string> = { "content-type": "application/json" };
      if (token) headers["x-studio-token"] = token;

      const response = await fetch(`/api/projects/${encodeURIComponent(app.id)}/build`, {
        method: "POST",
        headers,
        body: JSON.stringify({ action: "publish" }),
      });

      const payload = (await response.json()) as {
        job?: string;
        runner?: RunnerState;
        error?: string;
      };
      if (!response.ok) throw new Error(payload.error ?? `Publish failed to start (${response.status}).`);

      setRunner(payload.runner ?? null);
      if (payload.job) await fetchBuild(app.id, payload.job);
    } catch (thrown) {
      setBuildError(thrown instanceof Error ? thrown.message : "Could not publish this build.");
    } finally {
      setPublishBusy(false);
    }
  }, [
    activeAppId,
    activeFiles,
    appTitle,
    fetchBuild,
    kind,
    listSavedApps,
    persistApp,
    prompt,
    status,
    token,
  ]);

  /**
   * Ask the runner to stop the build, then wait for the status to say so.
   *
   * The 202 this gets back means "the request is on disk", not "stopped" — the
   * runner notices within a second and rewrites the status, which the existing
   * poll then picks up. So the button stays busy until the state moves, rather
   * than reporting success for something that has not happened yet.
   */
  const cancelBuild = useCallback(async () => {
    if (!activeAppId || !build || build.state !== "running" || cancelBusy) return;

    setCancelBusy(true);
    setBuildError(null);
    try {
      const headers: Record<string, string> = { "content-type": "application/json" };
      if (token) headers["x-studio-token"] = token;

      const response = await fetch(
        `/api/projects/${encodeURIComponent(activeAppId)}/build/cancel`,
        { method: "POST", headers, body: JSON.stringify({ job: build.job }) },
      );
      if (!response.ok) {
        const payload = (await response.json()) as { error?: string };
        throw new Error(payload.error ?? `Could not stop the build (${response.status}).`);
      }
      await fetchBuild(activeAppId, build.job);
    } catch (thrown) {
      setBuildError(thrown instanceof Error ? thrown.message : "Could not stop the build.");
    } finally {
      setCancelBusy(false);
    }
  }, [activeAppId, build, cancelBusy, fetchBuild, token]);

  const openApp = useCallback(
    async (id: string) => {
      setLibraryBusy(true);
      setLibraryError(null);
      try {
        const payload = (await (await request(`/api/projects/${id}`)).json()) as {
          project: {
            id: string;
            title: string;
            kind?: ProjectKind;
            prompt: string;
            files: GeneratedFile[];
          };
        };
        setFiles(payload.project.files);
        setPrompt(payload.project.prompt);
        setAppTitle(payload.project.title);
        // The kind comes from the saved record, so reopening a website does not
        // quietly turn the next instruction into an app revision.
        const opened = payload.project.kind === "website" ? "website" : "app";
        setKind(opened);
        kindRef.current = opened;
        setActiveAppId(payload.project.id);
        setRaw("");
        setTurns(1);
        setStatus("idle");
        setError(null);
        setLibraryNote(`Opened “${payload.project.title}”.`);
      } catch (thrown) {
        setLibraryError(thrown instanceof Error ? thrown.message : "Could not open that app.");
      } finally {
        setLibraryBusy(false);
      }
    },
    [request],
  );

  const deleteApp = useCallback(
    async (id: string, title: string) => {
      setLibraryBusy(true);
      setLibraryError(null);
      try {
        await request(`/api/projects/${id}`, { method: "DELETE" });
        if (activeAppId === id) {
          // Only the saved copy goes; the build stays on screen.
          setActiveAppId(null);
          setLibraryNote(`Deleted the saved copy of “${title}”. The build is still open.`);
        } else {
          setLibraryNote(`Deleted “${title}”.`);
        }
        await listSavedApps();
      } catch (thrown) {
        setLibraryError(thrown instanceof Error ? thrown.message : "Could not delete that app.");
      } finally {
        setLibraryBusy(false);
      }
    },
    [activeAppId, listSavedApps, request],
  );

  const newApp = useCallback(() => {
    setActiveAppId(null);
    setAppTitle("");
    setKind("app");
    kindRef.current = "app";
    setFiles([]);
    setRaw("");
    setTurns(0);
    setError(null);
    setLibraryNote(null);
    setLibraryError(null);
  }, []);

  const stop = useCallback(() => {
    pendingRef.current = null;
    setQueued(false);
    abortRef.current?.abort();
    abortRef.current = null;
    setStreamOpened(false);
    setByteCount(0);
    setStatus("idle");
  }, []);

  const generate = useCallback(async () => {
    if (busyRef.current) return;
    const firstInstruction = promptRef.current.trim();
    if (!firstInstruction) return;

    busyRef.current = true;
    const controller = new AbortController();
    abortRef.current = controller;

    setStatus("streaming");
    setError(null);
    setRaw("");
    setQueued(false);
    setCurrentFile(null);
    setStreamOpened(false);
    setByteCount(0);
    setStartedAt(Date.now());
    setLastChunkAt(Date.now());

    try {
      // This turn's inputs. A turn queued while another was streaming takes
      // over here — the loop is what makes continuous prompting seamless: the
      // next instruction starts the moment the current stream closes.
      let turnPrompt = firstInstruction;
      let priorFiles = filesRef.current;

      for (;;) {
        const headers: Record<string, string> = { "content-type": "application/json" };
        if (tokenRef.current) headers["x-studio-token"] = tokenRef.current;

        const response = await fetch("/api/generate", {
          method: "POST",
          headers,
          body: JSON.stringify({ prompt: turnPrompt, kind: kindRef.current, files: priorFiles }),
          signal: controller.signal,
        });

        if (!response.ok) {
          if (response.status === 401) setShowSettings(true);
          throw new Error(await readFailure(response));
        }
        if (!response.body) throw new Error("The gateway returned an empty stream.");

        // Headers are in and the body is about to stream: the wait for the
        // model's first token is a real, visible step from here on.
        setStreamOpened(true);

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let accumulated = "";

        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          accumulated += decoder.decode(value, { stream: true });
          setRaw(accumulated);
          setByteCount(accumulated.length);
          setCurrentFile(currentFileFrom(accumulated));
          setLastChunkAt(Date.now());
        }
        accumulated += decoder.decode();

        // The stream has ended, so recover a final block whose closing tag the
        // model never sent (parseFiles does this by default now).
        const produced = parseFiles(accumulated);
        if (produced.length === 0) {
          setRaw("");
          setCurrentFile(null);
          throw new Error("The model replied without any file blocks. Try rephrasing the request.");
        }

        setFiles(produced);
        filesRef.current = produced;
        setRaw("");
        setCurrentFile(null);
        setTurns((count) => count + 1);

        // Editing a saved app keeps its saved copy current, so reopening it
        // never silently reverts the last revision. Best-effort: the build
        // already succeeded, so a failed save must not fail the build.
        if (activeAppRef.current) {
          try {
            // No `kind` on purpose: this is a revision of a project that already
            // exists, and a revision keeps the kind it was created with. The files
            // on screen were written against that contract.
            const app = await persistApp({
              id: activeAppRef.current,
              title: appTitleRef.current,
              prompt: turnPrompt,
              files: produced,
            });
            appTitleRef.current = app.title;
            setAppTitle(app.title);
            await listSavedApps();
          } catch {
            setLibraryError("The build succeeded, but saving it failed. Use Save to retry.");
          }
        }

        const nextInstruction = pendingRef.current;
        if (!nextInstruction?.trim()) break;

        pendingRef.current = null;
        setQueued(false);
        turnPrompt = nextInstruction.trim();
        priorFiles = produced;

        // Clear the composer only when it still holds the queued text; if the
        // operator started typing something new mid-stream, that draft stays.
        if (promptRef.current === nextInstruction) setPrompt("");

        setError(null);
        setRaw("");
        setStreamOpened(false);
        setByteCount(0);
        setStartedAt(Date.now());
        setLastChunkAt(Date.now());
      }

      setStatus("idle");
    } catch (thrown) {
      if (controller.signal.aborted) return;
      setError(thrown instanceof Error ? thrown.message : "Generation failed.");
      setStatus("error");
      setCurrentFile(null);
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      busyRef.current = false;
    }
  }, [listSavedApps, persistApp]);

  /** Submit: start a build, or queue the instruction while one is running. */
  const submit = useCallback(() => {
    const text = prompt.trim();
    if (!text) return;

    if (busyRef.current) {
      pendingRef.current = text;
      setQueued(true);
      return;
    }
    void generate();
  }, [generate, prompt]);

  const unqueue = useCallback(() => {
    pendingRef.current = null;
    setQueued(false);
  }, []);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        submit();
      }
    },
    [submit],
  );

  const statusLabel = status === "streaming" ? "Building" : status === "error" ? "Failed" : "Ready";
  const statusDot = status === "streaming" ? "live" : status === "error" ? "bad" : "ok";
  const busy = status === "streaming";
  const hasFiles = activeFiles.length > 0;
  // Recomputed each render; the 1-second interval (and every chunk) re-renders
  // while streaming, so all of these move live.
  const elapsedSeconds = busy && startedAt !== null ? Math.max(0, Math.floor((Date.now() - startedAt) / 1000)) : 0;
  const stallSeconds = lastChunkAt !== null ? Math.max(0, Math.floor((Date.now() - lastChunkAt) / 1000)) : 0;
  const waiting = busy && lastChunkAt !== null && Date.now() - lastChunkAt > 4000;

  // Which step the build is on — read off the stream, never guessed.
  const phase = phaseOf({ opened: streamOpened, bytes: byteCount, writing: currentFile !== null });
  const phaseIndex = PHASE_ORDER.indexOf(phase);

  // Files whose closing tag has arrived. `streamedFiles` deliberately includes
  // the block still being written (so the preview fills as it grows), which is
  // why completion is tracked separately rather than reusing `activeFiles`.
  const completedFiles = useMemo(() => parseFiles(raw, { allowUnterminatedLast: false }), [raw]);

  // The bar turns determinate once there is a real ratio to show — files
  // finished against the files finished so far plus the one in flight. Before
  // the first file lands there is no ratio, so it stays the honest "data is
  // flowing" slide rather than inventing a percentage.
  const barDeterminate = currentFile !== null && completedFiles.length > 0;
  const barPercent = barDeterminate
    ? Math.round((completedFiles.length / (completedFiles.length + 1)) * 100)
    : 0;

  const rateLimitOn = rateLimit !== null && rateLimit.limit !== null;

  /** The limit the input asks for, clamped to what the API accepts. */
  const draftLimit = (): number => {
    const max = rateLimit?.max ?? 600;
    const parsed = Number.parseInt(rateLimitDraft, 10);
    if (!Number.isNaN(parsed) && parsed >= 1) return Math.min(parsed, max);
    const fallback = rateLimit?.envLimit ?? rateLimit?.limit ?? 20;
    return Math.min(Math.max(fallback, 1), max);
  };

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          Studio <span>Olympus</span>
        </div>
        <span className="pill">
          <span className={`dot ${statusDot}`} />
          {statusLabel}
          {busy ? ` ${clock(elapsedSeconds)}` : ""}
        </span>
        {turns > 0 ? <span className="pill">{hasFiles ? `${activeFiles.length} files` : "no files"}</span> : null}
        <span className="topbar-spacer" />
        {user ? (
          <span className="pill user" title={user}>
            {user}
          </span>
        ) : null}
        <button type="button" className="ghost" onClick={() => setShowSettings((open) => !open)}>
          Settings
        </button>
        {user ? (
          <a className="ghost" href="/api/auth/logout">
            Sign out
          </a>
        ) : null}
      </header>

      <div className="workspace">
        <section className="composer">
          {/* Choosing the kind is choosing the product, not a setting: it changes
              the system prompt, what the preview can show, and what delivery
              means. Locked while a build is running and while a saved app is
              open — switching either mid-flight would leave a project whose
              contents contradict its own kind. */}
          <div className="kind-picker" role="radiogroup" aria-label="What to build">
            {(["app", "website"] as const).map((option) => (
              <button
                key={option}
                type="button"
                role="radio"
                aria-checked={kind === option}
                className={`kind-option${kind === option ? " current" : ""}`}
                disabled={busy || libraryBusy}
                onClick={() => {
                  setKind(option);
                  kindRef.current = option;
                  if (option === "website") setTab("code");
                }}
              >
                {option === "website" ? "Website" : "App"}
                <em>{option === "website" ? "React + Vite" : "single page"}</em>
              </button>
            ))}
          </div>
          <span className="hint kind-hint">{KIND_HELP[kind]}</span>

          <div className="field">
            <label htmlFor="prompt">What should it build?</label>
            <textarea
              id="prompt"
              className="prompt"
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder="A budgeting app with categories, a monthly summary, and a bar chart drawn with CSS."
              spellCheck={false}
            />
          </div>

          <div className="actions">
            {busy ? (
              <>
                <button
                  type="button"
                  className="primary"
                  onClick={submit}
                  disabled={!prompt.trim() || queued}
                >
                  {queued ? "Queued ✓" : "Queue next"}
                </button>
                <button type="button" className="ghost" onClick={unqueue} disabled={!queued}>
                  Clear
                </button>
                <button type="button" className="ghost" onClick={stop}>
                  Stop
                </button>
              </>
            ) : (
              <button type="button" className="primary" onClick={submit} disabled={!prompt.trim()}>
                {turns > 0 ? "Add on" : "Build It"}
              </button>
            )}
            <span className="hint">
              <kbd>Ctrl</kbd> + <kbd>Enter</kbd>
            </span>
          </div>

          {error ? (
            <div className="alert error" role="alert">
              <span>{error}</span>
            </div>
          ) : null}

          {queued ? (
            <div className="alert note" role="status">
              <span>
                Queued — it runs the moment the current build finishes. Keep typing; your draft stays.
              </span>
              <span className="topbar-spacer" />
              <button type="button" className="ghost" onClick={unqueue}>
                Cancel
              </button>
            </div>
          ) : null}

          {showSettings ? (
            <div className="settings">
              <span className="hint">
                Shared access token. Only needed when <code>STUDIO_ACCESS_TOKEN</code> is set on the
                server; kept in this browser session.
              </span>
              <input
                type="password"
                value={token}
                autoComplete="off"
                spellCheck={false}
                placeholder="access token"
                aria-label="Studio access token"
                onChange={(event) => saveToken(event.target.value)}
              />

              <div className="settings-block">
                <div className="settings-head">
                  <span className="produced-head">Generation rate limit</span>
                  <span className="topbar-spacer" />
                  <label className="switch">
                    <input
                      type="checkbox"
                      checked={rateLimitOn}
                      disabled={!rateLimit || rateLimitBusy}
                      aria-label="Limit generations per minute"
                      onChange={(event) =>
                        void applyRateLimit(
                          event.target.checked
                            ? { disabled: false, perMinute: draftLimit() }
                            : { disabled: true },
                        )
                      }
                    />
                    <span>{rateLimit === null ? "…" : rateLimitOn ? "On" : "Off"}</span>
                  </label>
                </div>

                <span className="hint">
                  {rateLimit ? describeRateLimit(rateLimit) : "Reading the current setting…"}
                </span>

                {rateLimitOn && rateLimit ? (
                  <div className="settings-inline">
                    <input
                      type="number"
                      min={1}
                      max={rateLimit.max}
                      value={rateLimitDraft}
                      spellCheck={false}
                      disabled={rateLimitBusy}
                      aria-label="Generations per minute"
                      onChange={(event) => setRateLimitDraft(event.target.value)}
                    />
                    <span className="hint">per minute · max {rateLimit.max}</span>
                    <span className="topbar-spacer" />
                    <button
                      type="button"
                      className="ghost"
                      disabled={rateLimitBusy || String(rateLimit.limit) === rateLimitDraft.trim()}
                      onClick={() => void applyRateLimit({ disabled: false, perMinute: draftLimit() })}
                    >
                      Set
                    </button>
                  </div>
                ) : null}

                {rateLimit && rateLimit.override !== null ? (
                  <button
                    type="button"
                    className="ghost"
                    disabled={rateLimitBusy}
                    onClick={() => void applyRateLimit({ reset: true })}
                  >
                    Use the deployment default
                  </button>
                ) : null}

                <span className="hint">
                  Applies to every caller of this deployment. Off means no cap on how often an
                  account may start a generation.
                </span>

                {rateLimitError ? (
                  <div className="alert error" role="alert">
                    <span>{rateLimitError}</span>
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}

          {build || buildError ? (
            <section className={`build-panel ${build?.state ?? "failed"}`} aria-label="Factory build">
              <div className="build-panel-head">
                <span className={`dot ${build?.state === "running" ? "live" : "idle"}`} />
                <span className="produced-head">Factory build</span>
                <span className="topbar-spacer" />
                {build?.state === "running" ? (
                  <button
                    type="button"
                    className="danger"
                    onClick={() => void cancelBuild()}
                    disabled={cancelBusy}
                    title="Stop this build on the host runner"
                  >
                    {cancelBusy ? "Stopping…" : "Cancel build"}
                  </button>
                ) : null}
              </div>

              <span className="build-state">{build ? buildStateLabel(build) : "not started"}</span>

              <p className="build-message">{buildError ?? build?.message}</p>

              {build?.state === "running" ? (
                <div className="build-bar" aria-hidden="true">
                  <span />
                </div>
              ) : null}

              {build?.artifact ? (
                <p className="hint">
                  {build.artifact.files ?? 0} file(s), {kilobytes(build.artifact.bytes ?? 0)} — entry{" "}
                  {build.artifact.entry ?? "unknown"} in {build.artifact.dir}
                </p>
              ) : null}

              {/* The packaged site, which is what a website build actually
                  produces. Separate from the artifact line above because it
                  answers a different question: the source was written, and this
                  says it was built into something servable. */}
              {build?.site ? (
                <p className="hint">
                  packaged: {build.site.distFiles ?? 0}{" "}
                  {kind === "app" ? "client" : "dist"} file(s),{" "}
                  {kilobytes(build.site.distBytes ?? 0)} — {build.site.entry ?? "unknown"}
                  {build.site.zip ? ` · ${build.site.zip}` : ""}
                </p>
              ) : null}

              {/* Where the build actually lives, once a publish has put it
                  somewhere. The URL is the runner's own report rather than one
                  this component composes, so what is shown is what happened. */}
              {build?.publishedUrl ? (
                <p className="hint">
                  Live at{" "}
                  <a className="link-button" href={build.publishedUrl} target="_blank" rel="noreferrer">
                    {build.publishedUrl}
                  </a>
                </p>
              ) : build?.state === "succeeded" ? (
                <p className="hint">
                  {kind === "app" ? "Packaged" : "Packaged and staged"}. Put it on a name with{" "}
                  <em>Publish It</em>
                  {siteSuffix ? <> — it would answer at {slugPreview(build.slug, siteSuffix)}</> : null}.
                </p>
              ) : null}

              {runner && !runner.live ? (
                <p className="hint">No build runner is responding — start olympus-build-runner.</p>
              ) : null}

              {/* The record, not the tail in the status file: the runner keeps
                  4000 characters there for the 3s poll, and this route reads the
                  log the build actually wrote. */}
              {build?.job ? (
                <div className="build-log-block">
                  <div className="build-log-head">
                    <span className="hint">
                      {buildLog && !logHidden
                        ? `Build log${buildLog.truncated ? " (tail)" : ""} · ${kilobytes(buildLog.bytes)}`
                        : "Build log"}
                    </span>
                    <span className="topbar-spacer" />
                    {logBusy ? <span className="hint">reading…</span> : null}
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => setLogHidden((hidden) => !hidden)}
                    >
                      {logHidden ? "Show" : "Hide"}
                    </button>
                  </div>
                  {!logHidden ? (
                    <pre className="build-log-pane">
                      {buildLog?.text?.trim()
                        ? buildLog.text
                        : logBusy
                          ? "Reading the build log…"
                          : "This build has not written any output yet."}
                    </pre>
                  ) : null}
                </div>
              ) : null}

              {buildHistory.length > 1 ? (
                <div className="build-history">
                  <span className="hint">Earlier builds ({buildHistory.length - 1})</span>
                  <ul>
                    {buildHistory.slice(1).map((past) => (
                      <li key={past.job}>
                        <button
                          type="button"
                          className={`link-button${past.job === build?.job ? " current" : ""}`}
                          onClick={() => {
                            setBuild(past);
                            if (activeAppId) void fetchBuildLog(activeAppId, past.job);
                          }}
                          title="Show this build's message and log"
                        >
                          {buildStateLabel(past)}
                        </button>
                        <span className="hint">
                          {" "}
                          {timestampLabel(past.finishedAt ?? past.startedAt ?? past.requestedAt ?? "")}
                          {past.artifact ? ` — ${past.artifact.files ?? 0} file(s)` : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </section>
          ) : null}

          <div className="library">
            <div className="library-head">
              <span className="produced-head">Saved apps</span>
              <span className="topbar-spacer" />
              <span className="hint">
                {activeAppId ? "Editing a saved app" : hasFiles ? "Unsaved build" : "Nothing yet"}
              </span>
              {activeAppId ? (
                <button type="button" className="ghost" onClick={newApp} disabled={libraryBusy}>
                  New
                </button>
              ) : null}
            </div>

            <div className="library-save">
              <input
                value={appTitle}
                onChange={(event) => setAppTitle(event.target.value)}
                placeholder="app title"
                aria-label="Saved app title"
                maxLength={120}
                spellCheck={false}
                disabled={libraryBusy}
              />
              <button
                type="button"
                className="primary"
                onClick={() => void saveApp()}
                disabled={!hasFiles || busy || libraryBusy}
              >
                {activeAppId ? "Update" : "Save"}
              </button>
            </div>

            {/*
              * FOUR DELIVERIES, EACH WITH ONE JOB.
              *
              * These used to overlap — one button both rebuilt and published —
              * and the words gave no clue which did what. Now each names exactly
              * one destination:
              *
              *   Factory Build  run `make app` (Archon + the model) from a spec
              *   Publish It     take the files on screen and put them on a name
              *   Export It      write a build-requests spec for CI or a hand-off
              *   Download It    a zip of the source, and of dist/ once built
              *
              * Publish It deliberately does NOT run the factory. Publishing what a
              * rebuild would produce, rather than what the operator is looking at,
              * is a different app with the same name.
              */}
            <div className="deliveries">
              <button
                type="button"
                className="ghost"
                onClick={() => void runBuild(true)}
                disabled={!hasFiles || busy || libraryBusy || buildBusy || build?.state === "running"}
                title={
                  kind === "website"
                    ? "Run make app from a spec, then package the result into dist/"
                    : "Run make app from a spec, then package the client and the server around it"
                }
              >
                {buildBusy ? "Building…" : "Factory Build"}
              </button>
              <button
                type="button"
                className="ghost"
                onClick={() => void publishApp()}
                disabled={!hasFiles || busy || libraryBusy || publishBusy || build?.state === "running"}
                title={
                  kind === "website"
                    ? `Package these files and serve them at <name>.${siteSuffix}`
                    : `Package these files, build the image, run the container and serve it at <name>.${siteSuffix}`
                }
              >
                {publishBusy ? "Publishing…" : "Publish It"}
              </button>
              <button
                type="button"
                className="ghost"
                onClick={() => void exportApp()}
                disabled={!hasFiles || busy || libraryBusy}
                title="Write a build-requests spec so the factory or CI can continue this app"
              >
                Export It
              </button>
              <button
                type="button"
                className="ghost"
                onClick={() => void downloadZip()}
                disabled={!hasFiles || busy || libraryBusy}
                title={
                  kind === "website"
                    ? "Download a zip of the source (and of dist/, once it has been packaged)"
                    : "Download a zip of the client, the server and the Dockerfile"
                }
              >
                Download It
              </button>
            </div>

            {savedApps.length > 0 ? (
              <ul className="library-list">
                {savedApps.map((app) => (
                  <li
                    key={app.id}
                    className={app.id === activeAppId ? "library-item current" : "library-item"}
                  >
                    <button
                      type="button"
                      className="library-open"
                      onClick={() => void openApp(app.id)}
                      disabled={libraryBusy}
                      title={`Open ${app.title}`}
                    >
                      <strong>{app.title}</strong>
                      <em>
                        {app.fileCount} {app.fileCount === 1 ? "file" : "files"} ·{" "}
                        {timestampLabel(app.updatedAt)}
                      </em>
                    </button>
                    <button
                      type="button"
                      className="library-delete"
                      onClick={() => void deleteApp(app.id, app.title)}
                      disabled={libraryBusy}
                      aria-label={`Delete ${app.title}`}
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <span className="hint">
                Build something, then Save it — it comes back with your sign-in, on any browser.
              </span>
            )}

            {libraryError ? (
              <div className="alert error" role="alert">
                <span>{libraryError}</span>
              </div>
            ) : null}

            {libraryNote ? (
              <div className="alert note">
                <span>{libraryNote}</span>
              </div>
            ) : null}
          </div>

          {turns === 0 ? (
            <div className="field">
              <span className="produced-head">Try one of these</span>
              <div className="examples">
                {EXAMPLES.map((example) => (
                  <button key={example} type="button" className="chip" onClick={() => setPrompt(example)}>
                    {example}
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {busy && startedAt !== null ? (
            <div className="produced progress">
              <span className="produced-head">
                <span className={`dot ${waiting ? "wait" : "live"}`} />
                {PHASE_LABEL[phase]}
                {currentFile ? ` — ${currentFile.path} · ${kilobytes(currentFile.bytes)}` : ""}
                <span className="topbar-spacer" />
                <span className={waiting ? "progress-wait" : "progress-clock"}>
                  {waiting ? `no data for ${clock(stallSeconds)}` : `${clock(elapsedSeconds)} elapsed`}
                </span>
              </span>

              {/* Every step is a fact about the stream, not a timer. */}
              <ol className="steps">
                {PHASE_ORDER.map((step, index) => (
                  <li
                    key={step}
                    className={`step ${
                      index < phaseIndex ? "done" : index === phaseIndex ? "active" : "todo"
                    }`}
                  >
                    <span className="step-mark">
                      {index < phaseIndex ? "✓" : index === phaseIndex ? "•" : "·"}
                    </span>
                    {PHASE_LABEL[step]}
                  </li>
                ))}
              </ol>

              <div
                className="progress-bar"
                role="progressbar"
                aria-label="Build progress"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={barDeterminate ? barPercent : undefined}
              >
                <div
                  className={`progress-fill ${waiting ? "stalled" : ""} ${
                    barDeterminate ? "determinate" : ""
                  }`}
                  style={barDeterminate ? { width: `${barPercent}%` } : undefined}
                />
              </div>

              {completedFiles.length > 0 || currentFile ? (
                <div className="produced-list">
                  {completedFiles.map((file) => (
                    <span key={file.path} className="file-chip done">
                      ✓ {file.path}
                      <em>{kilobytes(file.contents.length)}</em>
                    </span>
                  ))}
                  {currentFile ? (
                    <span className="file-chip writing">
                      <span className={`dot ${waiting ? "wait" : "live"}`} />
                      {currentFile.path}
                      <em>{kilobytes(currentFile.bytes)}</em>
                    </span>
                  ) : null}
                </div>
              ) : null}

              <span className="hint">
                {completedFiles.length > 0
                  ? `${completedFiles.length} ${completedFiles.length === 1 ? "file" : "files"} written · `
                  : ""}
                {kilobytes(byteCount)} received
              </span>
            </div>
          ) : hasFiles ? (
            <div className="produced">
              <span className="produced-head">Files in this build</span>
              <div className="produced-list">
                {activeFiles.map((file) => (
                  <span key={file.path} className="file-chip">
                    {file.path}
                  </span>
                ))}
              </div>
            </div>
          ) : null}

          {!busy && turns > 0 ? (
            <div className="alert note">
              <span>
                Keep going — this is not a one-shot. Send another instruction and the current files
                go with it, so each turn develops the same app rather than starting over. Add a
                screen, change the data, make it better.
              </span>
            </div>
          ) : null}
        </section>

        <section className="stage">
          <div className="stage-head" role="tablist" aria-label="Build output">
            <button
              type="button"
              role="tab"
              className="tab"
              aria-selected={tab === "preview"}
              onClick={() => setTab("preview")}
            >
              Preview (after build)
            </button>
            <button
              type="button"
              role="tab"
              className="tab"
              aria-selected={tab === "code"}
              onClick={() => setTab("code")}
            >
              Code
            </button>
          </div>

          <div className="stage-body">
            {tab === "preview" ? (
              /* Neither kind can preview here, and pretending otherwise is the
                 worst option: the sandboxed iframe runs no JSX and no bundler, so
                 it would sit blank and read as a failure. What each one needs
                 before it can be seen is different, and saying which is the
                 difference between a dead pane and a next step. */
              <div className="site-note">
                <strong>
                  {kind === "app"
                    ? "An application has no preview until it is running"
                    : "A React site has no preview until it is built"}
                </strong>
                <span>
                  {kind === "app" ? (
                    <>
                      The client is React, so it renders only after packaging — and the API it
                      reads from does not exist until the app is running. Read it under{" "}
                      <em>Code</em>, then use <em>Publish It</em>: that builds the client, starts
                      the container and puts the name in front of it.
                    </>
                  ) : (
                    <>
                      JSX needs a compiler, so these components render only after packaging.
                      Read them under <em>Code</em>, then use <em>Publish It</em> — that runs the
                      Vite build on the host and produces a servable <code>dist/</code>.
                    </>
                  )}
                </span>
              </div>
            ) : (
              <CodeView files={activeFiles} />
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
