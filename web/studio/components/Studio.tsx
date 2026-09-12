"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { EMPTY_DOCUMENT, buildPreviewDocument, parseFiles, type GeneratedFile } from "@/lib/files";
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

async function readFailure(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { error?: unknown };
    if (typeof payload.error === "string" && payload.error) return payload.error;
  } catch {
    /* not JSON — fall through to the status line */
  }
  return `Request failed with status ${response.status}.`;
}

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

  // Saved-app library. Kept server-side under the signed-in identity, so the
  // same account gets the same apps on any browser.
  const [savedApps, setSavedApps] = useState<SavedApp[]>([]);
  const [activeAppId, setActiveAppId] = useState<string | null>(null);
  const [appTitle, setAppTitle] = useState("");
  const [libraryBusy, setLibraryBusy] = useState(false);
  const [libraryNote, setLibraryNote] = useState<string | null>(null);
  const [libraryError, setLibraryError] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);

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
    abortRef.current?.abort();
    abortRef.current = null;
    setStatus("idle");
  }, []);

  const generate = useCallback(async () => {
    const instruction = prompt.trim();
    if (!instruction || status === "streaming") return;

    const controller = new AbortController();
    abortRef.current = controller;

    setStatus("streaming");
    setError(null);
    setRaw("");

    try {
      const headers: Record<string, string> = { "content-type": "application/json" };
      if (token) headers["x-studio-token"] = token;

      const response = await fetch("/api/generate", {
        method: "POST",
        headers,
        body: JSON.stringify({ prompt: instruction, files }),
        signal: controller.signal,
      });

      if (!response.ok) {
        if (response.status === 401) setShowSettings(true);
        throw new Error(await readFailure(response));
      }
      if (!response.body) throw new Error("The gateway returned an empty stream.");

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let accumulated = "";

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        accumulated += decoder.decode(value, { stream: true });
        setRaw(accumulated);
      }
      accumulated += decoder.decode();

      // The stream has ended, so recover a final block whose closing tag the
      // model never sent. Mid-stream this stays strict (see `streamedFiles`),
      // which is what keeps half-written files out of the preview.
      const produced = parseFiles(accumulated, { allowUnterminatedLast: true });
      if (produced.length === 0) {
        setRaw("");
        throw new Error("The model replied without any file blocks. Try rephrasing the request.");
      }

      setFiles(produced);
      setRaw("");
      setTurns((count) => count + 1);
      setStatus("idle");

      // Editing a saved app keeps its saved copy current, so reopening it never
      // silently reverts the last revision. Best-effort: the build already
      // succeeded, so a failed save is reported and must not fail the build.
      if (activeAppId) {
        try {
          const app = await persistApp({
            id: activeAppId,
            title: appTitle,
            prompt: instruction,
            files: produced,
          });
          setAppTitle(app.title);
          await listSavedApps();
        } catch {
          setLibraryError("The build succeeded, but saving it failed. Use Save to retry.");
        }
      }
    } catch (thrown) {
      if (controller.signal.aborted) return;
      setError(thrown instanceof Error ? thrown.message : "Generation failed.");
      setStatus("error");
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  }, [
    activeAppId,
    appTitle,
    files,
    listSavedApps,
    persistApp,
    prompt,
    status,
    token,
  ]);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        void generate();
      }
    },
    [generate],
  );

  const statusLabel = status === "streaming" ? "Building" : status === "error" ? "Failed" : "Ready";
  const statusDot = status === "streaming" ? "live" : status === "error" ? "bad" : "ok";
  const busy = status === "streaming";
  const hasFiles = activeFiles.length > 0;

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          Studio <span>Olympus</span>
        </div>
        <span className="pill">
          <span className={`dot ${statusDot}`} />
          {statusLabel}
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
              disabled={busy}
            />
          </div>

          <div className="actions">
            {busy ? (
              <button type="button" className="primary" onClick={stop}>
                Stop
              </button>
            ) : (
              <button type="button" className="primary" onClick={() => void generate()} disabled={!prompt.trim()}>
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

          {hasFiles ? (
            <div className="produced">
              <span className="produced-head">
                {busy ? "Writing files" : "Files in this build"}
              </span>
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
