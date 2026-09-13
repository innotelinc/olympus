import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SYSTEM_PROMPT,
  WEBSITE_SYSTEM_PROMPT,
  buildMessages,
  chatCompletionsUrl,
  isPlaceholderSecret,
  missingEntryPoint,
  readConfig,
  sseToTextStream,
} from "@/lib/omniroute";

// readConfig() calls loadRepoEnv(), which reads the repo-root .env — and deleting a key
// from process.env does not make it absent, because the loader reads the file straight
// back. So without this the deployment's own values decide these assertions: a checkout
// that pins OMNIROUTE_MODEL failed "falls back to the default model" for a reason that
// has nothing to do with the code under test. Every test here sets what it needs.
// (env.test.ts covers the loader itself against temp directories.)
vi.mock("@/lib/env", () => ({ loadRepoEnv: () => {} }));

const MANAGED = ["OMNIROUTE_BASE_URL", "OMNIROUTE_API_KEY", "OMNIROUTE_MODEL", "OMNIROUTE_CHAT_PATH"];

let saved: Record<string, string | undefined> = {};

beforeEach(() => {
  saved = {};
  for (const key of MANAGED) {
    saved[key] = process.env[key];
    delete process.env[key];
  }
});

afterEach(() => {
  for (const key of MANAGED) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
});

describe("isPlaceholderSecret", () => {
  it("treats empty and template values as unset", () => {
    expect(isPlaceholderSecret("")).toBe(true);
    expect(isPlaceholderSecret("   ")).toBe(true);
    expect(isPlaceholderSecret("change-me-omniroute-api-key")).toBe(true);
    expect(isPlaceholderSecret("CHANGE-ME")).toBe(true);
    expect(isPlaceholderSecret("your-api-key")).toBe(true);
  });

  it("accepts a real-looking key", () => {
    expect(isPlaceholderSecret("sk-abc123")).toBe(false);
  });
});

describe("readConfig", () => {
  it("falls back to localhost and the default model", () => {
    const config = readConfig();
    expect(config.baseUrl).toBe("http://127.0.0.1:20128/v1");
    expect(config.model).toBe("auto/coding");
    expect(config.chatPath).toBe("/chat/completions");
  });

  it("strips trailing slashes from the base URL", () => {
    process.env.OMNIROUTE_BASE_URL = "http://gateway:20128/v1///";
    expect(readConfig().baseUrl).toBe("http://gateway:20128/v1");
  });

  it("honours overrides", () => {
    process.env.OMNIROUTE_MODEL = "auto/fast";
    process.env.OMNIROUTE_CHAT_PATH = "responses";
    const config = readConfig();
    expect(config.model).toBe("auto/fast");
    expect(chatCompletionsUrl(config)).toBe("http://127.0.0.1:20128/v1/responses");
  });
});

describe("chatCompletionsUrl", () => {
  it("joins the base URL and path", () => {
    expect(
      chatCompletionsUrl({
        baseUrl: "http://omniroute:20128/v1",
        chatPath: "/chat/completions",
        apiKey: "x",
        model: "auto/coding",
      }),
    ).toBe("http://omniroute:20128/v1/chat/completions");
  });
});

describe("buildMessages", () => {
  it("puts the system contract first and the instruction last", () => {
    const messages = buildMessages("a counter", [], "app");
    expect(messages).toHaveLength(2);
    expect(messages[0].role).toBe("system");
    expect(messages.at(-1)).toEqual({ role: "user", content: "a counter" });
  });

  it("includes prior files so revisions have context", () => {
    const messages = buildMessages(
      "make it blue",
      [
        { path: "index.html", contents: "<html></html>" },
        { path: "styles.css", contents: "body{}" },
      ],
      "app",
    );
    expect(messages).toHaveLength(3);
    const context = messages[1].content;
    expect(context).toContain('<file path="index.html">');
    expect(context).toContain('<file path="styles.css">');
    expect(messages.at(-1)?.content).toBe("make it blue");
  });

  // The whole point of threading `kind` this far: the system message is the only
  // thing that tells the model which of two incompatible products it is making,
  // and a caller that sent the wrong one would produce a page that looks like a
  // plugin failure rather than a wrong argument.
  it("sends the website contract for a website, not the app one", () => {
    const website = buildMessages("a landing page", [], "website");
    expect(website[0].content).toBe(WEBSITE_SYSTEM_PROMPT);
    expect(website[0].content).toContain("src/App.tsx");
    expect(website[0].content).not.toContain("Always include index.html");
  });

  it("calls a website's prior files a site, not an app", () => {
    const messages = buildMessages(
      "darker",
      [{ path: "src/App.tsx", contents: "export default () => null;" }],
      "website",
    );
    expect(messages[1].content).toContain("Current version of the site:");
  });

  it("keeps the app contract for an app", () => {
    const messages = buildMessages("a timer", [], "app");
    expect(messages[0].content).toBe(SYSTEM_PROMPT);
    expect(messages[0].content).toContain("Always include index.html");
  });
});

describe("missingEntryPoint", () => {
  it("passes an app only with index.html", () => {
    expect(missingEntryPoint([{ path: "index.html", contents: "" }], "app")).toBeNull();
    expect(missingEntryPoint([{ path: "app.js", contents: "" }], "app")).toBe("index.html");
  });

  it("needs src/App.tsx for a website, and does not accept index.html for it", () => {
    expect(missingEntryPoint([{ path: "src/App.tsx", contents: "" }], "website")).toBeNull();
    // A website that emitted an index.html instead is a website that will fail to
    // package, so this has to be reported rather than accepted.
    expect(missingEntryPoint([{ path: "index.html", contents: "" }], "website")).toBe(
      "src/App.tsx",
    );
  });
});

describe("WEBSITE_SYSTEM_PROMPT", () => {
  it("pins the contract the packaging step relies on", () => {
    expect(WEBSITE_SYSTEM_PROMPT).toContain('<file path="src/App.tsx">');
    expect(WEBSITE_SYSTEM_PROMPT).toContain("NO DEPENDENCIES");
    // The scaffold owns these; a model that emits them changes nothing but its own
    // output length, and the prompt has to say so or it will try.
    for (const owned of ["package.json", "vite.config.ts", "tsconfig.json", "src/main.tsx"]) {
      expect(WEBSITE_SYSTEM_PROMPT).toContain(owned);
    }
  });
});

describe("SYSTEM_PROMPT", () => {
  it("pins the output contract the parser depends on", () => {
    expect(SYSTEM_PROMPT).toContain('<file path="index.html">');
    expect(SYSTEM_PROMPT).toContain("Always include index.html");
    expect(SYSTEM_PROMPT).toContain("no network access");
  });
});

/* ---- streaming ---------------------------------------------------------- */

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function readAll(stream: ReadableStream<Uint8Array>): Promise<string> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let out = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    out += decoder.decode(value, { stream: true });
  }
  return out + decoder.decode();
}

function chatChunk(text: string): string {
  return `data: ${JSON.stringify({ choices: [{ delta: { content: text } }] })}\n\n`;
}

describe("sseToTextStream", () => {
  it("concatenates content deltas", async () => {
    const source = streamOf([chatChunk("<file "), chatChunk('path="a">'), chatChunk("x</file>")]);
    expect(await readAll(sseToTextStream(source))).toBe('<file path="a">x</file>');
  });

  it("reassembles a frame split across network chunks", async () => {
    const frame = chatChunk("hello");
    const cut = Math.floor(frame.length / 2);
    const source = streamOf([frame.slice(0, cut), frame.slice(cut)]);
    expect(await readAll(sseToTextStream(source))).toBe("hello");
  });

  it("ignores the [DONE] sentinel and non-data lines", async () => {
    const source = streamOf([chatChunk("a"), "event: message\n", ": keep-alive\n", "data: [DONE]\n\n"]);
    expect(await readAll(sseToTextStream(source))).toBe("a");
  });

  it("ignores malformed JSON frames", async () => {
    const source = streamOf(["data: {not json}\n\n", chatChunk("ok")]);
    expect(await readAll(sseToTextStream(source))).toBe("ok");
  });

  it("supports the Responses API delta shape", async () => {
    const source = streamOf([`data: ${JSON.stringify({ type: "response.output_text.delta", delta: "hi" })}\n\n`]);
    expect(await readAll(sseToTextStream(source))).toBe("hi");
  });

  it("supports array-shaped content parts", async () => {
    const source = streamOf([
      `data: ${JSON.stringify({ choices: [{ delta: { content: [{ type: "text", text: "part" }] } }] })}\n\n`,
    ]);
    expect(await readAll(sseToTextStream(source))).toBe("part");
  });

  it("preserves newlines inside generated code", async () => {
    const source = streamOf([chatChunk("<file>\n"), chatChunk("line1\nline2\n"), chatChunk("</file>")]);
    expect(await readAll(sseToTextStream(source))).toBe("<file>\nline1\nline2\n</file>");
  });

  it("emits a final frame that arrives without a trailing newline", async () => {
    const source = streamOf([chatChunk("a"), `data: ${JSON.stringify({ choices: [{ delta: { content: "z" } }] })}`]);
    expect(await readAll(sseToTextStream(source))).toBe("az");
  });
});
