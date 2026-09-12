import { loadRepoEnv } from "./env";

/**
 * OmniRoute client config. Every value is read server-side only — the gateway
 * key is never sent to, or bundled for, the browser.
 */

export type OmniRouteConfig = {
  baseUrl: string;
  chatPath: string;
  apiKey: string;
  model: string;
};

export type PriorFile = {
  path: string;
  contents: string;
};

const DEFAULT_BASE_URL = "http://127.0.0.1:20128/v1";
const DEFAULT_CHAT_PATH = "/chat/completions";
const DEFAULT_MODEL = "auto/coding";

/** Values shipped in `.env.example` that must not be treated as real credentials. */
const PLACEHOLDER_PREFIXES = ["change-me", "changeme", "your-", "xxx", "todo"];

export function isPlaceholderSecret(value: string): boolean {
  const lowered = value.trim().toLowerCase();
  if (!lowered) return true;
  return PLACEHOLDER_PREFIXES.some((prefix) => lowered.startsWith(prefix));
}

export function readConfig(): OmniRouteConfig {
  loadRepoEnv();

  return {
    baseUrl: (process.env.OMNIROUTE_BASE_URL?.trim() || DEFAULT_BASE_URL).replace(/\/+$/, ""),
    chatPath: process.env.OMNIROUTE_CHAT_PATH?.trim() || DEFAULT_CHAT_PATH,
    apiKey: process.env.OMNIROUTE_API_KEY?.trim() || "",
    model: process.env.OMNIROUTE_MODEL?.trim() || DEFAULT_MODEL,
  };
}

export function chatCompletionsUrl(config: OmniRouteConfig): string {
  const path = config.chatPath.startsWith("/") ? config.chatPath : `/${config.chatPath}`;
  return `${config.baseUrl}${path}`;
}

/**
 * The output contract. Studio renders whatever the model emits inside a
 * sandboxed iframe with no network access, so generated apps must be
 * dependency-free and self-contained.
 */
export const SYSTEM_PROMPT = `You are Studio, the build agent inside Olympus. The user describes a web app in plain language and you return complete, runnable source files.

Output rules — follow them exactly:
- Reply with one or more file blocks and nothing else. No prose, no preamble, no explanation, no markdown fences.
- Wrap every file exactly like this:
<file path="index.html">
...file contents...
</file>
- Always include index.html. It is the entry point and is rendered live in a sandboxed iframe.
- Rewrite each file in full on every turn. Never emit patches, diffs, or partial edits.
- Keep the file set small: index.html plus optional styles.css and app.js.

Runtime rules — the preview has no network access and no build step:
- Vanilla HTML, CSS, and JavaScript only.
- No CDN script or stylesheet tags, no npm imports, no external fonts, no fetch/XHR to third parties.
- Inline CSS in a <style> tag or in styles.css; inline JS in a <script> tag or in app.js.
- Keep all state in memory. Use localStorage only if the user explicitly asks for persistence.

Design rules:
- Semantic HTML, responsive layout, and visible focus states.
- Make it look deliberate: real spacing, a coherent colour palette, sensible typography.
- Ship something that runs the moment it renders — no placeholder TODOs.`;

export function buildMessages(prompt: string, priorFiles: PriorFile[]): Array<{ role: "system" | "user"; content: string }> {
  const messages: Array<{ role: "system" | "user"; content: string }> = [
    { role: "system", content: SYSTEM_PROMPT },
  ];

  if (priorFiles.length > 0) {
    const rendered = priorFiles
      .map((file) => `<file path="${file.path}">\n${file.contents}\n</file>`)
      .join("\n\n");

    messages.push({
      role: "user",
      content: `Current version of the app:\n\n${rendered}\n\nRevise it according to the next instruction and return every file in full.`,
    });
  }

  messages.push({ role: "user", content: prompt });

  return messages;
}

/**
 * Pull the text delta out of one SSE payload, tolerating both the Chat
 * Completions shape and the Responses API shape — the gateway can be
 * configured for either.
 */
function extractDelta(payload: unknown): string {
  if (typeof payload !== "object" || payload === null) return "";
  const record = payload as Record<string, unknown>;

  const choices = record.choices;
  if (Array.isArray(choices) && choices.length > 0) {
    const choice = choices[0] as Record<string, unknown> | undefined;
    const carrier = (choice?.delta ?? choice?.message) as Record<string, unknown> | undefined;
    const content = carrier?.content;
    if (typeof content === "string") return content;
    if (Array.isArray(content)) {
      return content
        .map((part) => {
          const text = (part as Record<string, unknown> | undefined)?.text;
          return typeof text === "string" ? text : "";
        })
        .join("");
    }
  }

  if (typeof record.delta === "string") return record.delta;
  if (typeof record.output_text === "string") return record.output_text;

  return "";
}

function deltaFromSseLine(line: string): string {
  const trimmed = line.trim();
  if (!trimmed.startsWith("data:")) return "";

  const payload = trimmed.slice(5).trim();
  if (!payload || payload === "[DONE]") return "";

  try {
    return extractDelta(JSON.parse(payload));
  } catch {
    return "";
  }
}

/**
 * Convert an SSE byte stream into a plain-text stream of generated source, so
 * the browser can accumulate and parse code blocks as they arrive.
 */
export function sseToTextStream(source: ReadableStream<Uint8Array>): ReadableStream<Uint8Array> {
  const decoder = new TextDecoder();
  const encoder = new TextEncoder();
  let buffer = "";

  return source.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        buffer += decoder.decode(chunk, { stream: true });

        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        for (const line of lines) {
          const delta = deltaFromSseLine(line);
          if (delta) controller.enqueue(encoder.encode(delta));
        }
      },
      flush(controller) {
        buffer += decoder.decode();
        const delta = deltaFromSseLine(buffer);
        if (delta) controller.enqueue(encoder.encode(delta));
      },
    }),
  );
}
