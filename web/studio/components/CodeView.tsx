"use client";

import { useEffect, useState } from "react";
import { languageFor, type GeneratedFile } from "@/lib/files";

export default function CodeView({ files }: { files: GeneratedFile[] }) {
  const [selected, setSelected] = useState(0);
  const [copied, setCopied] = useState(false);

  // A new build replaces the file set — fall back to the first file.
  useEffect(() => {
    setSelected(0);
  }, [files]);

  useEffect(() => {
    if (!copied) return;
    const id = window.setTimeout(() => setCopied(false), 1400);
    return () => window.clearTimeout(id);
  }, [copied]);

  if (files.length === 0) {
    return <div className="empty">No source yet. Describe an app to generate files.</div>;
  }

  const active = files[Math.min(selected, files.length - 1)];

  async function copyActive() {
    try {
      await navigator.clipboard.writeText(active.contents);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="code-shell">
      <nav className="code-list" aria-label="Generated files">
        {files.map((file, index) => (
          <button
            key={file.path}
            type="button"
            className="code-item"
            aria-current={index === Math.min(selected, files.length - 1)}
            onClick={() => setSelected(index)}
          >
            {file.path}
            <em>{languageFor(file.path)}</em>
          </button>
        ))}
      </nav>

      <div className="code-pane">
        <div className="code-head">
          <strong>{active.path}</strong>
          <span>
            {active.contents.split("\n").length} lines · {active.contents.length} bytes
          </span>
          <span className="topbar-spacer" />
          <button type="button" className="ghost" onClick={copyActive}>
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
        <pre className="code-body">
          <code>{active.contents}</code>
        </pre>
      </div>
    </div>
  );
}
