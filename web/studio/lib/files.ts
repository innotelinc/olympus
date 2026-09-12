/**
 * Parsing and assembly for the Studio preview.
 *
 * The model emits `<file path="…">…</file>` blocks. We parse them as they
 * stream in (incomplete blocks simply do not match yet) and inline linked
 * assets so the result renders inside a sandboxed iframe with no network.
 */

export type GeneratedFile = {
  path: string;
  contents: string;
};

const FILE_BLOCK = /<file\s+path="([^"]+)"\s*>\n?([\s\S]*?)<\/file>/g;

export const EMPTY_DOCUMENT = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Studio preview</title>
    <style>
      :root { color-scheme: dark; }
      body {
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background: #0b0d12;
        color: #7c8698;
        font: 14px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
        text-align: center;
      }
      .hint { max-width: 34ch; padding: 24px; }
      strong { display: block; margin-bottom: 6px; color: #c9d1e0; font-size: 15px; }
      code {
        font-family: ui-monospace, "SF Mono", Menlo, monospace;
        color: #8fb6ff;
      }
    </style>
  </head>
  <body>
    <div class="hint">
      <strong>Nothing rendered yet</strong>
      Describe an app and it appears here. Generated files are inlined and run
      without network access.
    </div>
  </body>
</html>`;

export function parseFiles(raw: string): GeneratedFile[] {
  const files: GeneratedFile[] = [];
  const seen = new Set<string>();

  FILE_BLOCK.lastIndex = 0;
  let match = FILE_BLOCK.exec(raw);

  while (match !== null) {
    const path = match[1].trim();
    const contents = match[2].replace(/\s+$/, "");

    if (path && !seen.has(path)) {
      seen.add(path);
      files.push({ path, contents });
    }

    match = FILE_BLOCK.exec(raw);
  }

  return files;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function baseName(path: string): string {
  const parts = path.split("/");
  return parts[parts.length - 1] ?? path;
}

export function languageFor(path: string): string {
  const name = baseName(path).toLowerCase();
  if (name.endsWith(".html") || name.endsWith(".htm")) return "html";
  if (name.endsWith(".css")) return "css";
  if (name.endsWith(".js") || name.endsWith(".mjs")) return "javascript";
  if (name.endsWith(".json")) return "json";
  if (name.endsWith(".svg")) return "svg";
  if (name.endsWith(".md")) return "markdown";
  return "text";
}

/**
 * Compose a single self-contained document from the generated file set:
 * replace `<link href="styles.css">` and `<script src="app.js">` with the
 * actual contents, then append any assets the entry point never linked.
 */
export function buildPreviewDocument(files: GeneratedFile[]): string {
  if (files.length === 0) return EMPTY_DOCUMENT;

  const entry =
    files.find((file) => /(^|\/)index\.html$/i.test(file.path)) ??
    files.find((file) => /\.html?$/i.test(file.path));

  if (!entry) return listingDocument(files);

  const inlined = new Set<string>([entry.path]);
  let document = entry.contents;

  for (const file of files) {
    if (file.path === entry.path) continue;

    const name = baseName(file.path);

    if (/\.css$/i.test(file.path)) {
      const pattern = new RegExp(
        `<link\\b[^>]*href\\s*=\\s*["'][^"']*${escapeRegExp(name)}["'][^>]*>`,
        "gi",
      );
      const replaced = document.replace(pattern, () => `<style>\n${file.contents}\n</style>`);
      if (replaced !== document) {
        document = replaced;
        inlined.add(file.path);
      }
    } else if (/\.js$/i.test(file.path)) {
      const pattern = new RegExp(
        `<script\\b[^>]*src\\s*=\\s*["'][^"']*${escapeRegExp(name)}["'][^>]*>\\s*</script>`,
        "gi",
      );
      const replaced = document.replace(pattern, () => `<script>\n${file.contents}\n</script>`);
      if (replaced !== document) {
        document = replaced;
        inlined.add(file.path);
      }
    }
  }

  const orphanCss = files.filter((file) => /\.css$/i.test(file.path) && !inlined.has(file.path));
  const orphanJs = files.filter((file) => /\.js$/i.test(file.path) && !inlined.has(file.path));

  const cssBlock = orphanCss.map((file) => `<style>\n${file.contents}\n</style>`).join("\n");
  const jsBlock = orphanJs.map((file) => `<script>\n${file.contents}\n</script>`).join("\n");

  if (cssBlock) {
    document = /<\/head>/i.test(document)
      ? document.replace(/<\/head>/i, `${cssBlock}\n</head>`)
      : `${cssBlock}\n${document}`;
  }

  if (jsBlock) {
    document = /<\/body>/i.test(document)
      ? document.replace(/<\/body>/i, `${jsBlock}\n</body>`)
      : `${document}\n${jsBlock}`;
  }

  return document;
}

/** Fallback preview when the model produced no HTML entry point. */
function listingDocument(files: GeneratedFile[]): string {
  const items = files
    .map(
      (file) =>
        `<li><code>${escapeHtml(file.path)}</code> <span>${file.contents.length} bytes</span></li>`,
    )
    .join("\n");

  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Studio preview</title>
    <style>
      :root { color-scheme: dark; }
      body {
        margin: 0;
        padding: 40px;
        background: #0b0d12;
        color: #c9d1e0;
        font: 14px/1.7 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
      }
      h1 { font-size: 15px; margin: 0 0 4px; }
      p { margin: 0 0 20px; color: #7c8698; }
      ul { list-style: none; margin: 0; padding: 0; }
      li {
        display: flex;
        justify-content: space-between;
        gap: 16px;
        padding: 10px 14px;
        border: 1px solid #1e2430;
        border-radius: 8px;
        margin-bottom: 8px;
      }
      span { color: #7c8698; }
      code { font-family: ui-monospace, "SF Mono", Menlo, monospace; color: #8fb6ff; }
    </style>
  </head>
  <body>
    <h1>No HTML entry point</h1>
    <p>The build produced these files but nothing to render.</p>
    <ul>
${items}
    </ul>
  </body>
</html>`;
}
