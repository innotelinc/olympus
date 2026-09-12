"use client";

/**
 * The preview runs generated markup in a sandboxed iframe. `allow-scripts`
 * without `allow-same-origin` gives the app a unique origin, so generated code
 * cannot reach Studio's DOM, cookies, or storage. There is no network access.
 */
export default function Preview({ source }: { source: string }) {
  return (
    <div className="preview-shell">
      <iframe
        className="preview-frame"
        title="App preview"
        sandbox="allow-scripts allow-modals allow-forms"
        referrerPolicy="no-referrer"
        srcDoc={source}
      />
    </div>
  );
}
