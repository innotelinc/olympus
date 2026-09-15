"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { BuildLog, BuildStatus, RunnerState } from "@/lib/build-queue";
import { currentFileFrom, mergeFiles, parseFiles, type GeneratedFile } from "@/lib/files";
import { missingPlannedFiles, type BuildPlan } from "@/lib/plan";
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

const STORAGE_KEY = "studio.token";
// `localStorage`, unlike the token: which model builds your apps is a preference
// that should outlive a tab, and it is not a secret.
const MODEL_KEY = "studio.model";
/** How many matches the model dropdown renders at once. Search still sees them all. */
const MODEL_MENU_LIMIT = 400;

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

/**
 * The newest address any of this project's jobs left behind.
 *
 * Newest-first is the order the history arrives in, and within one job there is only
 * ever one address — a preview reports no published URL, a publish reports no preview
 * URL — so the choice is between jobs. The newest job is the one that ran the files
 * most recently, which is the one the pane should be showing.
 */
function latestDeliveryUrl(history: BuildStatus[]): string | null {
  for (const past of history) {
    if (past.previewUrl) return past.previewUrl;
    if (past.publishedUrl) return past.publishedUrl;
  }
  return null;
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
 * One model the gateway has linked in, as `/api/models` reports it.
 *
 * Studio does not know any model names of its own. Provider keys live in
 * OmniRoute, so the list is whatever is linked in *now* — which is why this is
 * fetched rather than shipped, and why the picker is not a constant.
 */
type ModelOption = {
  id: string;
  provider: string;
  combo: boolean;
  contextLength: number | null;
  capabilities: Record<string, boolean>;
};

/** `auto/coding · combo · 1M` — enough to choose between two names that differ by a word. */
function describeModel(model: ModelOption): string {
  const context =
    model.contextLength === null
      ? ""
      : model.contextLength >= 1_000_000
        ? ` · ${Math.round(model.contextLength / 1_000_000)}M ctx`
        : ` · ${Math.round(model.contextLength / 1_000)}k ctx`;
  return `${model.id} · ${model.provider}${context}`;
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

/**
 * One line under each step, saying what waiting there actually means.
 *
 * The labels alone are three words each, and the two that look alike — the model is
 * thinking, then it is writing — are the difference between a request that is
 * working and one that is being thought about for a long time. Saying what is
 * happening, and what would appear if it were happening, is what makes a slow step
 * legible as slow rather than stuck.
 */
const PHASE_DETAIL: Record<BuildPhase, string> = {
  connecting: "The request is on its way to the gateway.",
  thinking: "The stack is being decided. Nothing is written yet, so an empty tree here is normal.",
  writing: "Files are arriving one at a time, in full — this is the part that takes the longest.",
  finalizing: "The last file is being closed and the stream is ending.",
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
  // What this project turns out to be — a running service, or files served
  // statically. Not a setting and not sent anywhere: the planner reads the request
  // and answers, it arrives with the plan, and a project opened from the library
  // brings its own. Held in state because the delivery wording reads it, and for
  // the first turn it is not knowable yet.
  const [kind, setKind] = useState<ProjectKind>("app");
  const [turns, setTurns] = useState(0);
  const [token, setToken] = useState("");
  const [showSettings, setShowSettings] = useState(false);
  // Which linked model builds this. Empty means "whatever the gateway is
  // configured with", which is a real choice and not an absence — it is the
  // deployment default, and it tracks the day the default changes.
  const [model, setModel] = useState("");
  const [models, setModels] = useState<ModelOption[]>([]);
  const [modelFilter, setModelFilter] = useState("");
  const [modelBusy, setModelBusy] = useState(false);
  const [modelError, setModelError] = useState<string | null>(null);
  const [modelDefault, setModelDefault] = useState("");

  // The plan step: what the model proposes before anything is written, held here
  // until the person building it says go.
  const [plan, setPlan] = useState<BuildPlan | null>(null);
  const [planPrompt, setPlanPrompt] = useState("");
  const [planModel, setPlanModel] = useState("");
  const [planBusy, setPlanBusy] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  // Non-fatal: the plan said it would write a file and the turn did not. Worth
  // saying, not worth discarding a whole generation over.
  const [planWarning, setPlanWarning] = useState<string | null>(null);
  const [projectPlan, setProjectPlan] = useState<BuildPlan | null>(null);
  // Bumped to force the preview frame to reload; a frame's `src` is not re-fetched by
  // re-rendering, and after a republish the old document is what is on screen.
  const [previewNonce, setPreviewNonce] = useState(0);

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
  // explain the other's. Previewing is a third for the same reason — it neither
  // builds with the factory nor puts anything on a name.
  const [publishBusy, setPublishBusy] = useState(false);
  const [previewBusy, setPreviewBusy] = useState(false);
  // The log of the build being shown, fetched on its own cadence: it is tens of
  // kilobytes, so it does not belong in the status poll that runs every 3s.
  const [buildLog, setBuildLog] = useState<BuildLog | null>(null);
  // Visible by default: a build's output is the answer to "what is it doing?",
  // and hiding the thing that moves is how a build looks stalled.
  const [logHidden, setLogHidden] = useState(false);
  const [logBusy, setLogBusy] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const planAbortRef = useRef<AbortController | null>(null);

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
  // A mirror of the model choice for the streaming core, which reads its inputs
  // from refs: a queued turn must build with what the user picked, not with what
  // the render closure happened to capture when the queue was filled.
  const modelRef = useRef("");
  // The confirmed plan, and the instruction it was written for. The pair matters:
  // a plan answers one instruction, so if the prompt has been edited since it was
  // drafted the plan no longer describes what is about to be built and is not
  // confirmed — it is re-planned.
  const planRef = useRef<BuildPlan | null>(null);
  const planPromptRef = useRef("");
  const planBusyRef = useRef(false);
  // The plan the project *is*, as opposed to the one being confirmed. It survives
  // the generation that consumed it: build and publish need the language, the
  // commands and the port, and those are facts about the project rather than about
  // the turn that produced it.
  const projectPlanRef = useRef<BuildPlan | null>(null);

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
    modelRef.current = model;
  }, [model]);

  // Remembered across sessions on purpose: which model builds your apps is a
  // preference, not a per-tab secret, and re-picking it every visit would be the
  // tax on having a choice at all.
  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(MODEL_KEY);
      if (stored) setModel(stored);
    } catch {
      /* storage unavailable — the choice stays in memory */
    }
  }, []);

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

  // Reload the frame when a preview finishes.
  //
  // A frame re-fetches when its `src` changes, and a project's preview address is
  // the same one every time — `<slug>-preview.<suffix>`, by design, so that the name
  // stays stable across previews. Without this, pressing Preview It a second time
  // would run the new build and leave the previous one on screen, which reads as the
  // preview not working. Keyed on the job id so it fires once per run rather than on
  // every poll.
  const previewedJobRef = useRef<string | null>(null);
  useEffect(() => {
    if (build?.action !== "preview" || build.state !== "succeeded") return;
    if (previewedJobRef.current === build.job) return;
    previewedJobRef.current = build.job;
    setPreviewNonce((nonce) => nonce + 1);
  }, [build?.action, build?.job, build?.state]);

  // ---- which model builds this ------------------------------------------

  const loadModels = useCallback(
    async (fresh = false) => {
      setModelBusy(true);
      try {
        const payload = (await (
          await request(`/api/models${fresh ? "?fresh=1" : ""}`)
        ).json()) as { models?: ModelOption[]; default?: string; note?: string | null };
        if (!Array.isArray(payload.models) || payload.models.length === 0) {
          throw new Error("The gateway has no models linked in.");
        }
        setModels(payload.models);
        setModelDefault(payload.default ?? "");
        setModelError(payload.note ?? null);
      } catch (thrown) {
        // Not fatal: generation still works on the configured default, so this is
        // reported where the picker is rather than as a banner over the build.
        setModels([]);
        setModelError(thrown instanceof Error ? thrown.message : "Could not read the model list.");
      } finally {
        setModelBusy(false);
      }
    },
    [request],
  );

  // Fetched when the panel first opens rather than on mount: it is a few hundred
  // kilobytes of provider catalog that most visits never look at.
  useEffect(() => {
    if (showSettings && models.length === 0 && !modelBusy) void loadModels();
  }, [showSettings, models.length, modelBusy, loadModels]);

  const saveModel = useCallback((value: string) => {
    setModel(value);
    try {
      if (value) window.localStorage.setItem(MODEL_KEY, value);
      else window.localStorage.removeItem(MODEL_KEY);
    } catch {
      /* storage unavailable — the choice stays in memory for this session */
    }
  }, []);

  // Every linked model is searchable, but not all of them are rendered: 2,064
  // `<option>` elements make the control slow to open, while the filter narrows to
  // what is actually being looked for. The cap is what keeps both true.
  const filteredModels = useMemo(() => {
    const needle = modelFilter.trim().toLowerCase();
    if (!needle) return models;
    return models.filter(
      (option) =>
        option.id.toLowerCase().includes(needle) || option.provider.toLowerCase().includes(needle),
    );
  }, [models, modelFilter]);

  const visibleModels = useMemo(
    () => filteredModels.slice(0, MODEL_MENU_LIMIT),
    [filteredModels],
  );

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
      /** Omitted on a revision, which keeps the plan it has. */
      plan?: BuildPlan | null;
    }) => {
      const response = await request("/api/projects", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          id: input.id ?? undefined,
          title: input.title ?? "",
          prompt: input.prompt ?? "",
          kind: input.kind,
          // The plan the files were built to. This is what makes Build It and
          // Publish It possible on a reloaded project: without it the runner has
          // nothing but the older packagers to fall back on.
          plan: input.plan ?? undefined,
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
        // The plan this project is built to. Omitted only when there is not one,
        // which is a project saved before the planner existed.
        plan: projectPlanRef.current,
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
        // The plan this project is built to. Omitted only when there is not one,
        // which is a project saved before the planner existed.
        plan: projectPlanRef.current,
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
        // The plan this project is built to. Omitted only when there is not one,
        // which is a project saved before the planner existed.
        plan: projectPlanRef.current,
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
        // The plan this project is built to. Omitted only when there is not one,
        // which is a project saved before the planner existed.
        plan: projectPlanRef.current,
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
            ? " This build has not been packaged yet, so the archive holds the source — run “Build It” to get one with the built output inside."
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
        // The plan this project is built to. Omitted only when there is not one,
        // which is a project saved before the planner existed.
        plan: projectPlanRef.current,
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
   * Package the files on screen, run them, and show the result — without publishing.
   *
   * This is what the Preview tab needs and could never have: the project itself,
   * served by its own process, not a re-render of its markup. A publish produces the
   * same running container and then registers the name; a preview stops before that,
   * because looking at a build is not a decision to announce it.
   *
   * Saved first, like a publish, and for the same reason: the request carries the
   * saved files, and a preview that disagreed with the library would show a build
   * nobody can reopen.
   */
  const previewApp = useCallback(async () => {
    if (activeFiles.length === 0 || status === "streaming") return;

    setPreviewBusy(true);
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
        plan: projectPlanRef.current,
      });
      setActiveAppId(app.id);
      setAppTitle(app.title);
      await listSavedApps();

      const headers: Record<string, string> = { "content-type": "application/json" };
      if (token) headers["x-studio-token"] = token;

      const response = await fetch(`/api/projects/${encodeURIComponent(app.id)}/build`, {
        method: "POST",
        headers,
        body: JSON.stringify({ action: "preview" }),
      });

      const payload = (await response.json()) as {
        job?: string;
        runner?: RunnerState;
        error?: string;
      };
      if (!response.ok) throw new Error(payload.error ?? `Preview failed to start (${response.status}).`);

      setRunner(payload.runner ?? null);
      // The pane is where the answer will arrive, so go there now rather than after
      // the build: the operator asked to see it, and the run's progress is visible
      // under the same tab while the container comes up.
      setTab("preview");
      if (payload.job) await fetchBuild(app.id, payload.job);
    } catch (thrown) {
      setBuildError(thrown instanceof Error ? thrown.message : "Could not start the preview.");
    } finally {
      setPreviewBusy(false);
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
            plan?: BuildPlan | null;
          };
        };
        setFiles(payload.project.files);
        setPrompt(payload.project.prompt);
        setAppTitle(payload.project.title);
        // The project's plan comes back with it, so Build It and Publish It work on a
        // project reopened days later, and the panel can say what stack it is.
        projectPlanRef.current = payload.project.plan ?? null;
        setProjectPlan(payload.project.plan ?? null);
        // Any plan awaiting confirmation belongs to the instruction that was in the
        // box, which is not this project's. Dropping it avoids offering to confirm a
        // plan for text that is no longer there.
        planRef.current = null;
        planPromptRef.current = "";
        setPlan(null);
        setPlanPrompt("");
        setPlanModel("");
        // The kind comes from the saved record — which took it from this project's
        // own plan — so reopening a project does not relabel what it is. An unread
        // one is not a third kind; there are two, and "app" is what the server
        // reports for anything stored before this field existed.
        setKind(payload.project.kind === "website" ? "website" : "app");
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
    setFiles([]);
    setRaw("");
    setTurns(0);
    setError(null);
    setLibraryNote(null);
    setLibraryError(null);
    // Nothing on screen means nothing to build, so the plan goes with the files.
    projectPlanRef.current = null;
    setProjectPlan(null);
    planRef.current = null;
    planPromptRef.current = "";
    setPlan(null);
    setPlanPrompt("");
    setPlanModel("");
    setPlanWarning(null);
  }, []);

  const stop = useCallback(() => {
    pendingRef.current = null;
    setQueued(false);
    abortRef.current?.abort();
    abortRef.current = null;
    // A planning request in flight is cancelled too: Stop means stop.
    planAbortRef.current?.abort();
    planAbortRef.current = null;
    setStreamOpened(false);
    setByteCount(0);
    setStatus("idle");
  }, []);

  /**
   * Ask what this instruction would be built from, and show the answer.
   *
   * This is the step that replaced a hardcoded stack. Nothing is generated until
   * the plan it produces has been confirmed, so the stack, the run commands and
   * the port are all things the person saw before a single file was written.
   */
  const draftPlan = useCallback(
    async (instruction?: string) => {
      const text = (instruction ?? promptRef.current).trim();
      if (!text || planBusyRef.current) return;

      planBusyRef.current = true;
      planAbortRef.current?.abort();
      const controller = new AbortController();
      planAbortRef.current = controller;

      setPlanBusy(true);
      setPlanError(null);
      setPlanWarning(null);

      try {
        const response = await request("/api/plan", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            prompt: text,
            files: filesRef.current,
            ...(modelRef.current ? { model: modelRef.current } : {}),
          }),
          signal: controller.signal,
        });

        const payload = (await response.json()) as { plan?: BuildPlan; model?: string };
        if (!payload.plan) throw new Error("The planner returned nothing usable.");

        planRef.current = payload.plan;
        planPromptRef.current = text;
        setPlan(payload.plan);
        setPlanPrompt(text);
        setPlanModel(typeof payload.model === "string" ? payload.model : "");
        // The planner's answer includes what it decided this is, and the delivery
        // wording keys on it. Taken when the plan arrives rather than when it is
        // confirmed, so the panel describes the plan on screen.
        setKind(payload.plan.kind);
      } catch (thrown) {
        if (controller.signal.aborted) return;
        if (thrown instanceof Error && /sign in/i.test(thrown.message)) setShowSettings(true);
        setPlanError(thrown instanceof Error ? thrown.message : "Planning failed.");
      } finally {
        planBusyRef.current = false;
        setPlanBusy(false);
      }
    },
    [request],
  );

  /** Set the plan aside. The instruction stays in the prompt, ready to re-plan. */
  const discardPlan = useCallback(() => {
    planRef.current = null;
    planPromptRef.current = "";
    setPlan(null);
    setPlanPrompt("");
    setPlanModel("");
    setPlanError(null);
    setPlanWarning(null);
  }, []);

  const generate = useCallback(async () => {
    if (busyRef.current) return;
    // The instruction the plan was written for, not whatever is in the box now. A
    // plan cannot be confirmed against text it did not answer.
    const turnPrompt = planPromptRef.current.trim();
    const plan = planRef.current;
    if (!turnPrompt || !plan) return;

    // The project now *is* this plan. Everything that comes later — Build It,
    // Publish It, saving, reopening — reads it from here, which is why the plan is
    // not cleared with the confirmation that consumed it.
    projectPlanRef.current = plan;
    setProjectPlan(plan);
    setKind(plan.kind);

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
      {
        const priorFiles = filesRef.current;
        const headers: Record<string, string> = { "content-type": "application/json" };
        if (tokenRef.current) headers["x-studio-token"] = tokenRef.current;

        const response = await fetch("/api/generate", {
          method: "POST",
          headers,
          body: JSON.stringify({
            prompt: turnPrompt,
            files: priorFiles,
            // The confirmed plan travels with the build. The route re-reads every
            // field of it, so a plan cannot be weakened by the trip through the page.
            plan,
            // Omitted when the user left it on the default, so the gateway decides
            // — which is what keeps a redeployment from pinning a stale model.
            ...(modelRef.current ? { model: modelRef.current } : {}),
          }),
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
        // Fold this turn into the project rather than replacing it. A development
        // turn returns the files it changed, so the ones it did not mention are
        // kept — which is what makes "add on" additive instead of a rewrite.
        const merged = mergeFiles(filesRef.current, produced);
        filesRef.current = merged;
        setFiles(merged);
        setRaw("");
        setCurrentFile(null);
        setTurns((count) => count + 1);

        // The plan promised a file list. A file it named and the turn did not write
        // is worth saying out loud — the build that follows will fail on it, and the
        // message there would be about a missing module.
        const missing = missingPlannedFiles(produced, plan);
        setPlanWarning(
          missing
            ? `The plan listed ${missing} and this turn did not write it, so the build will probably fail there. Ask for it again, naming that file.`
            : null,
        );

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
              files: merged,
              // The plan this turn was built to, not the mirror: the mirror is only
              // updated at the top of the turn, and this is the value that turn used.
              plan,
            });
            appTitleRef.current = app.title;
            setAppTitle(app.title);
            await listSavedApps();
          } catch {
            setLibraryError("The build succeeded, but saving it failed. Use Save to retry.");
          }
        }
      }

      // The plan has been built. Clearing it is what makes the next instruction go
      // through a fresh plan instead of silently reusing this one's stack.
      planRef.current = null;
      planPromptRef.current = "";
      setPlan(null);
      setPlanPrompt("");
      setPlanModel("");
      if (promptRef.current === turnPrompt) setPrompt("");

      // An instruction queued while this turn streamed is picked up here — planned,
      // not run blind, because an addition is still a decision about a stack.
      const nextInstruction = pendingRef.current;
      if (nextInstruction?.trim()) {
        pendingRef.current = null;
        setQueued(false);
        void draftPlan(nextInstruction.trim());
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
  }, [draftPlan, listSavedApps, persistApp]);

  /**
   * Take a failed build back to the model, with what the build actually said.
   *
   * This is the half of the loop that was missing: a build fails, the reason goes
   * into the log, and the person who asked for the project has to read a compiler
   * error and re-describe it. So the error goes back in as the instruction, with
   * the project's own plan attached, and the model is asked to fix the cause.
   *
   * It deliberately does NOT re-plan. A plan is a decision about a stack and that
   * decision did not change; asking the person to confirm a new stack every time a
   * dependency version is wrong would make the confirm step noise.
   *
   * The log tail is capped well under the route's prompt limit, and the newest lines
   * are the ones kept: a build log is mostly progress, and the error is at the end.
   */
  const fixBuild = useCallback(() => {
    const plan = projectPlanRef.current;
    if (!plan || busyRef.current) return;

    const reported = build?.message?.trim() || buildError?.trim() || "The build failed.";
    const tail = (build?.logTail ?? "").trim();
    const budget = tail.length > 5_000 ? tail.slice(-5_000) : tail;

    const instruction = [
      "The last build of this project failed. Fix it, keeping the confirmed plan and every feature that already works.",
      "",
      `What the build reported: ${reported}`,
      budget ? `\nThe end of the build output:\n\n${budget}` : "",
      "",
      "Return only the files that need to change, in full. Fix the cause of the failure, not the symptom — and do not remove a feature to make an error go away.",
    ]
      .filter(Boolean)
      .join("\n")
      .slice(0, 7_000);

    // The same shape a confirmed plan produces, so generation needs no special case:
    // a plan, the instruction it answers, and the project it belongs to.
    planRef.current = plan;
    planPromptRef.current = instruction;
    setPlan(plan);
    setPlanPrompt(instruction);
    setProjectPlan(plan);
    setBuildError(null);
    void generate();
  }, [build, buildError, generate]);

  /**
   * Submit: plan, confirm, or queue.
   *
   * One button, three meanings, and which one it has is decided by what is
   * already on screen: a plan that answers the text in the box is confirmed, a
   * prompt with no plan gets one, and anything typed while a build streams is
   * queued to be planned the moment it finishes.
   */
  const submit = useCallback(() => {
    const text = prompt.trim();
    if (!text) return;

    if (busyRef.current) {
      pendingRef.current = text;
      setQueued(true);
      return;
    }

    // A plan is only confirmed when it answers the instruction that is in the box
    // now. Editing the prompt reopens the question rather than building the plan
    // for the text that was there before.
    if (planRef.current && planPromptRef.current === text) {
      void generate();
      return;
    }

    void draftPlan(text);
  }, [draftPlan, generate, prompt]);

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

  const statusLabel = planBusy
    ? "Planning"
    : status === "streaming"
      ? "Building"
      : status === "error"
        ? "Failed"
        : "Ready";
  const statusDot = planBusy || status === "streaming" ? "live" : status === "error" ? "bad" : "ok";
  const busy = status === "streaming";
  // A plan describes one instruction. Change the instruction and the plan no longer
  // answers it, so it is shown as out of date rather than silently confirmed.
  const planStale = plan !== null && prompt.trim() !== planPrompt;
  const planReady = plan !== null && !planStale && !busy;
  // Where the project is actually running, as the runner reported it — never a URL
  // this component composes, so what is framed is what was started. The current job's
  // own status comes first (it is the newest thing that happened), then the newest
  // address in the history, so reopening an app shows its last run rather than
  // forgetting it ever had one.
  const previewUrl = build?.previewUrl ?? build?.publishedUrl ?? latestDeliveryUrl(buildHistory);
  // A job that packages and runs the files on screen and stops before the name. The
  // pane has to tell this apart from a factory build: one ends in a frame, the other
  // in a directory, and they take about the same time to get there.
  const previewing = build?.state === "running" && build.action === "preview";
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
          {/* No kind to pick. This used to ask "app or website", which was a guess
              dressed as a choice: the person had to name the delivery shape before
              they had described the thing, and the planner then had to build to the
              answer they guessed at — a résumé page planned as a server, or a
              tracker planned as static files, with the plan contradicting its own
              kind. The turn that reads the request decides now, and the plan on
              screen says what it decided before anything is written. */}
          <span className="hint kind-hint">
            Say what you want built — a site, a tool, a service, an app. What it is
            made of and how it runs is worked out from your description, and you
            confirm it before a file is written.
          </span>

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

          {/*
            * THE ONE BUTTON, AND WHAT IT MEANS RIGHT NOW.
            *
            * There used to be a Build button that generated code and a separate
            * row that also said Build, and neither said which stack it would use.
            * Now:
            *
            *   no plan        Plan & Build / Develop It   ask what it would be built from
            *   plan ready     Confirm & build              write it, against the plan on screen
            *   streaming      Queue next                   plan the instruction when this finishes
            *
            * Building a new project and adding to an existing one are the same
            * gesture with different words, because they are the same operation:
            * plan an addition, confirm, write it.
            */}
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
              <>
                <button
                  type="button"
                  className="primary"
                  onClick={submit}
                  disabled={!prompt.trim() || planBusy}
                >
                  {planReady
                    ? turns > 0
                      ? "Confirm & develop"
                      : "Confirm & build"
                    : planBusy
                      ? "Planning…"
                      : turns > 0
                        ? "Develop It"
                        : "Plan & Build"}
                </button>
                {plan ? (
                  <button type="button" className="ghost" onClick={discardPlan} disabled={planBusy}>
                    Discard plan
                  </button>
                ) : null}
              </>
            )}
            <span className="hint">
              <kbd>Ctrl</kbd> + <kbd>Enter</kbd>
            </span>
          </div>

          {planBusy ? (
            <div className="alert note" role="status">
              <span>
                Asking what this would be built from — the stack, the commands that run it, and the
                files it needs. Nothing is written until you confirm.
              </span>
            </div>
          ) : null}

          {planError ? (
            <div className="alert error" role="alert">
              <span>{planError}</span>
            </div>
          ) : null}

          {plan ? (
            <div className={`plan${planStale ? " stale" : ""}`} aria-label="Build plan">
              <div className="plan-head">
                <span className="produced-head">The plan</span>
                <span className="topbar-spacer" />
                <span className="hint">
                  {planStale ? "the prompt changed — plan it again" : planModel ? `planned by ${planModel}` : ""}
                </span>
              </div>

              <h3 className="plan-name">
                {plan.name}
                <span className="plan-slug">
                  {plan.slug}.{siteSuffix}
                </span>
              </h3>
              <p className="plan-summary">{plan.summary}</p>

              <ul className="plan-stack">
                <li>{plan.runtime.language}</li>
                {plan.runtime.frameworks.map((framework) => (
                  <li key={framework}>{framework}</li>
                ))}
                <li>{plan.runtime.database ? plan.runtime.database : "no database"}</li>
                <li>{plan.kind === "website" ? "static" : "persistent"}</li>
                {/* Only when it is not the default: a chip reading "container" on every
                    plan would be noise, and the absence of a Convex chip is the
                    answer for everything that is not one. */}
                {plan.target === "convex" ? <li>convex backend</li> : null}
              </ul>

              <dl className="plan-run">
                {plan.run.install ? (
                  <>
                    <dt>install</dt>
                    <dd>
                      <code>{plan.run.install}</code>
                    </dd>
                  </>
                ) : null}
                {plan.run.build ? (
                  <>
                    <dt>build</dt>
                    <dd>
                      <code>{plan.run.build}</code>
                    </dd>
                  </>
                ) : null}
                <dt>start</dt>
                <dd>
                  <code>{plan.run.start}</code> · port {plan.run.port}
                </dd>
              </dl>

              {plan.files.length > 0 ? (
                <details className="plan-files">
                  <summary>
                    {plan.files.length} {plan.files.length === 1 ? "file" : "files"}
                  </summary>
                  <ul>
                    {plan.files.map((file) => (
                      <li key={file.path}>
                        <code>{file.path}</code>
                        {file.purpose ? <em>{file.purpose}</em> : null}
                      </li>
                    ))}
                  </ul>
                </details>
              ) : null}

              {plan.notes ? <p className="plan-notes">{plan.notes}</p> : null}
            </div>
          ) : null}

          {planWarning ? (
            <div className="alert note" role="status">
              <span>{planWarning}</span>
              <span className="topbar-spacer" />
              <button type="button" className="ghost" onClick={() => setPlanWarning(null)}>
                Dismiss
              </button>
            </div>
          ) : null}

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

              <div className="settings-block">
                <div className="settings-head">
                  <span className="produced-head">Model</span>
                  <span className="topbar-spacer" />
                  <button
                    type="button"
                    className="ghost"
                    disabled={modelBusy}
                    onClick={() => void loadModels(true)}
                  >
                    {modelBusy ? "Reading…" : "Refresh"}
                  </button>
                </div>

                <span className="hint">
                  {model
                    ? `Building with ${model}. Every model the gateway has linked in is listed — the provider keys live in OmniRoute, not here.`
                    : `Building with the gateway's default${
                        modelDefault ? ` (${modelDefault})` : ""
                      }. Pick a model to build with that instead.`}
                </span>

                <div className="settings-inline">
                  <input
                    type="search"
                    value={modelFilter}
                    spellCheck={false}
                    placeholder="filter by model or provider"
                    aria-label="Filter models"
                    onChange={(event) => setModelFilter(event.target.value)}
                  />
                  <span className="topbar-spacer" />
                  <button
                    type="button"
                    className="ghost"
                    disabled={!model}
                    onClick={() => saveModel("")}
                  >
                    Use default
                  </button>
                </div>

                {models.length > 0 ? (
                  <select
                    className="model-select"
                    value={model}
                    aria-label="Model"
                    onChange={(event) => saveModel(event.target.value)}
                  >
                    <option value="">
                      Gateway default{modelDefault ? ` — ${modelDefault}` : ""}
                    </option>
                    {visibleModels.map((option) => (
                      <option key={option.id} value={option.id}>
                        {describeModel(option)}
                      </option>
                    ))}
                  </select>
                ) : null}

                {models.length > 0 && visibleModels.length < filteredModels.length ? (
                  <span className="hint">
                    Showing {visibleModels.length} of {filteredModels.length} matches — keep typing to
                    narrow it down.
                  </span>
                ) : null}

                {models.length > 0 ? (
                  <span className="hint">
                    {filteredModels.length} of {models.length} linked models
                    {modelFilter ? ` matching “${modelFilter.trim()}”` : ""}.
                  </span>
                ) : null}

                {modelError ? <span className="hint">{modelError}</span> : null}
              </div>
            </div>
          ) : null}

          {build || buildError ? (
            <section className={`build-panel ${build?.state ?? "failed"}`} aria-label="Build">
              <div className="build-panel-head">
                <span className={`dot ${build?.state === "running" ? "live" : "idle"}`} />
                <span className="produced-head">
                  Build{build?.language ? ` · ${build.language}` : ""}
                </span>
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

              {/*
                * THE ERROR, AND WHAT TO DO WITH IT.
                *
                * A failed build used to end at "see the build log", which leaves the
                * person reading a compiler's output and re-describing it in their own
                * words. The failure goes back to the model instead, with the project's
                * own plan, and it fixes it against the code it wrote.
                */}
              {(build?.state === "failed" || Boolean(buildError)) && projectPlan ? (
                <div className="build-fix">
                  <button
                    type="button"
                    className="primary"
                    onClick={fixBuild}
                    disabled={busy || libraryBusy}
                    title="Send the build's own error back to the model, with this project's plan"
                  >
                    {busy ? "Fixing…" : "Fix it"}
                  </button>
                  <span className="hint">
                    Sends what the build said, and the plan it failed on, back to the model.
                    The stack does not change — only the code does.
                  </span>
                </div>
              ) : null}

              {(build?.state === "failed" || Boolean(buildError)) && !projectPlan ? (
                <span className="hint">
                  This project has no plan to rebuild from — it was saved before projects had them.
                  Plan a change from the prompt to give it one.
                </span>
              ) : null}

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
                  packaged: {build.site.distFiles ?? 0} file(s),{" "}
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
                  Packaged. Put it on a name with{" "}
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
              * These used to overlap — one button both rebuilt and published — and
              * the words gave no clue which did what. Now each names exactly one
              * destination, and the run commands come from the project's own plan
              * rather than from a kind:
              *
              *   Build It       install, build and start the project to see it run
              *   Preview It     run it and show it here, announcing nothing
              *   Publish It     put the built project on its own name
              *   Export It      write a build-requests spec for CI or a hand-off
              *   Download It    a zip of the source, and of the build output once built
              *
              * None of them runs the factory. Delivering what a rebuild would
              * produce, rather than what the operator is looking at, is a different
              * project with the same name.
              */}
            <div className="deliveries">
              <button
                type="button"
                className="ghost"
                onClick={() => void runBuild(true)}
                disabled={!hasFiles || busy || libraryBusy || buildBusy || build?.state === "running"}
                title="Install, build and start this project on the build host, using the commands in its plan"
              >
                {buildBusy ? "Building…" : "Build It"}
              </button>
              <button
                type="button"
                className="ghost"
                onClick={() => void previewApp()}
                disabled={
                  !hasFiles || busy || libraryBusy || previewBusy || build?.state === "running"
                }
                title="Package these files, run the project and show it in the pane — no name is registered and nothing is published"
              >
                {previewing || previewBusy ? "Previewing…" : "Preview It"}
              </button>
              <button
                type="button"
                className="ghost"
                onClick={() => void publishApp()}
                disabled={!hasFiles || busy || libraryBusy || publishBusy || build?.state === "running"}
                title={`Build this project as it stands and serve it at <name>.${siteSuffix}`}
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
                title="Download a zip of the source, and of the build output once it has been built"
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

              {/* Every step is a fact about the stream, not a timer. The marks are
                  drawn as a column with connectors so the outline reads as one
                  sequence rather than four unrelated lines. */}
              <ol className="steps" aria-label="Build steps">
                {PHASE_ORDER.map((step, index) => (
                  <li
                    key={step}
                    className={`step ${
                      index < phaseIndex ? "done" : index === phaseIndex ? "active" : "todo"
                    }`}
                    aria-current={index === phaseIndex ? "step" : undefined}
                  >
                    <span className="step-mark">{index < phaseIndex ? "✓" : index + 1}</span>
                    <span className="step-body">
                      <span className="step-name">{PHASE_LABEL[step]}</span>
                      <span className="step-detail">{PHASE_DETAIL[step]}</span>
                    </span>
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
              Preview
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
              /*
               * THE PREVIEW IS THE RUNNING PROJECT, NOT A RE-RENDER OF IT.
               *
               * The frame used to hold a sandboxed document with no network and no
               * bundler, which could only ever show a self-contained HTML page — and
               * neither kind is one: a project's source needs its own toolchain, and an
               * app needs its API running before any client can render. So the frame
               * holds the project itself, on the address the runner reported after it
               * started it.
               *
               * Two jobs put an address there, and they differ in one thing: Preview It
               * runs the project and stops, Publish It runs it and registers the name at
               * the edge. Neither composes a URL here — what is framed is where the
               * project was actually started, because a frame pointed at a hostname
               * nothing has been told to answer on is exactly the blank pane this
               * replaced.
               */
              previewUrl ? (
                <div className="preview">
                  <div className="preview-bar">
                    <span className="preview-url" title={previewUrl}>
                      {previewUrl.replace(/^https?:\/\//, "")}
                    </span>
                    <span className="topbar-spacer" />
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => setPreviewNonce((nonce) => nonce + 1)}
                    >
                      Reload
                    </button>
                    <a className="link-button" href={previewUrl} target="_blank" rel="noreferrer">
                      Open in a new tab
                    </a>
                  </div>
                  <iframe
                    key={previewNonce}
                    className="preview-frame"
                    src={previewUrl}
                    title={`${projectPlan?.name ?? activeFiles[0]?.path ?? "project"} preview`}
                    sandbox="allow-forms allow-modals allow-popups allow-same-origin allow-scripts"
                  />
                </div>
              ) : (
                <div className="site-note">
                  <strong>
                    {previewing
                      ? "Previewing — the project appears when it answers"
                      : build?.state === "running"
                        ? "Building — the preview appears if this run puts it on a name"
                        : "Nothing is running yet"}
                  </strong>
                  <span>
                    The preview is the project itself, served from its own container. Use{" "}
                    <em>Preview It</em> to package these files, run them and show them here —
                    that registers no name and publishes nothing. Use <em>Publish It</em> to do
                    the same and put the name in front of it. Read the source under <em>Code</em>
                    meanwhile.
                    {projectPlan ? (
                      <>
                        {" "}
                        This one is {projectPlan.runtime.language}
                        {projectPlan.runtime.frameworks.length > 0
                          ? ` (${projectPlan.runtime.frameworks.join(", ")})`
                          : ""}
                        , listening on port {projectPlan.run.port}.
                      </>
                    ) : null}
                  </span>
                </div>
              )
            ) : (
              <CodeView files={activeFiles} />
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
