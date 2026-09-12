import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  SYSTEM_PROMPT,
  buildMessages,
  chatCompletionsUrl,
  isPlaceholderSecret,
  readConfig,
  sseToTextStream,
} from "@/lib/omniroute";

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
    const messages = buildMessages("a counter", []);
    expect(messages).toHaveLength(2);
    expect(messages[0].role).toBe("system");
    expect(messages.at(-1)).toEqual({ role: "user", content: "a counter" });
  });

  it("includes prior files so revisions have context", () => {
    const messages = buildMessages("make it blue", [
      { path: "index.html", contents: "<html></html>" },
      { path: "styles.css", contents: "body{}" },
    ]);
    expect(messages).toHaveLength(3);
    const context = messages[1].content;
    expect(context).toContain('<file path="index.html">');
    expect(context).toContain('<file path="styles.css">');
    expect(messages.at(-1)?.content).toBe("make it blue");
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
