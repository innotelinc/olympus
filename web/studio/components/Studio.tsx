"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { EMPTY_DOCUMENT, buildPreviewDocument, currentFileFrom, parseFiles, type GeneratedFile } from "@/lib/files";
import CodeView from "./CodeView";
import Preview from "./Preview";

type Status = "idle" | "streaming" | "error";
type Tab = "preview" | "code";

const EXAMPLES = [
  "A pomodoro timer with a circular progress ring and start, pause, and reset controls.",
  "A kanban board with three columns where cards can be dragged between them.",
  "A tip splitter: bill total, party size, and a slider that updates the per-person amount live.",
  "A markdown notes app with a live preview pane and a saved-notes sidebar.",
];

const STORAGE_KEY = "studio.token";

type SavedApp = {
  id: string;
  title: string;
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

/** m:ss elapsed label. */
function clock(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

/** Human-readable byte count for the progress line. */
function kilobytes(bytes: number): string {
  return bytes >= 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${bytes} B`;
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

export default function Studio({ user = null }: { user?: string | null }) {
  const [prompt, setPrompt] = useState("");
  const [files, setFiles] = useState<GeneratedFile[]>([]);
  const [raw, setRaw] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("preview");
  const [turns, setTurns] = useState(0);
  const [token, setToken] = useState("");
  const [showSettings, setShowSettings] = useState(false);
  const [previewDoc, setPreviewDoc] = useState(EMPTY_DOCUMENT);

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

  // Re-render once a second while streaming so the elapsed counter and the
  // waiting indicator move without waiting for stream data.
  useEffect(() => {
    if (status !== "streaming") return;
    const id = window.setInterval(() => setNowTick((tick) => tick + 1), 1000);
    return () => window.clearInterval(id);
  }, [status]);

  // Rebuild the preview on a delay while streaming so the iframe is not torn
  // down and recreated on every token.
  useEffect(() => {
    const delay = status === "streaming" ? 400 : 0;
    const id = window.setTimeout(() => {
      setPreviewDoc(buildPreviewDocument(activeFiles));
    }, delay);
    return () => window.clearTimeout(id);
  }, [activeFiles, status]);

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
    async (input: { id?: string | null; title?: string; prompt?: string; files: GeneratedFile[] }) => {
      const response = await request("/api/projects", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          id: input.id ?? undefined,
          title: input.title ?? "",
          prompt: input.prompt ?? "",
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
  }, [activeAppId, activeFiles, appTitle, listSavedApps, persistApp, prompt, status]);

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
        error?: string;
      };
      if (!response.ok) throw new Error(payload.error ?? `Export failed (${response.status}).`);

      setLibraryNote(
        `${payload.replaced ? "Replaced" : "Wrote"} build-requests/${payload.filename} — ` +
          `continue with: ${payload.next ?? "make app"}.`,
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
    listSavedApps,
    persistApp,
    prompt,
    status,
    token,
  ]);

  const openApp = useCallback(
    async (id: string) => {
      setLibraryBusy(true);
      setLibraryError(null);
      try {
        const payload = (await (await request(`/api/projects/${id}`)).json()) as {
          project: { id: string; title: string; prompt: string; files: GeneratedFile[] };
        };
        setFiles(payload.project.files);
        setPrompt(payload.project.prompt);
        setAppTitle(payload.project.title);
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
          body: JSON.stringify({ prompt: turnPrompt, files: priorFiles }),
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
                {turns > 0 ? "Revise" : "Build"}
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
              <button
                type="button"
                className="ghost"
                onClick={() => void exportApp()}
                disabled={!hasFiles || busy || libraryBusy}
                title="Write a build-requests spec so the factory can continue this app"
              >
                Export to factory
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
                Iterate in plain language — the current files are sent with the next instruction.
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
            {tab === "preview" ? <Preview source={previewDoc} /> : <CodeView files={activeFiles} />}
          </div>
        </section>
      </div>
    </div>
  );
}
