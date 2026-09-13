import { execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { MAX_ARCHIVE_BYTES, createZip, crc32, isSafeEntryName } from "@/lib/archive";

/**
 * The archive has to be readable by something that is not this code.
 *
 * Every assertion here checks bytes the writer produced, and the last one hands the
 * file to a real unzip when the host has one. That matters more than it looks: a
 * zip is easy to write in a way that this module's own tests agree with and nothing
 * else opens.
 */

const FIXED_TIME = new Date("2026-09-13T12:00:00Z");

const dirs: string[] = [];

function scratch(): string {
  const dir = mkdtempSync(join(tmpdir(), "studio-zip-"));
  dirs.push(dir);
  return dir;
}

afterAll(() => {
  for (const dir of dirs) rmSync(dir, { recursive: true, force: true });
});

describe("crc32", () => {
  it("matches the known value for a known string", () => {
    // The canonical check value from the CRC-32/ISO-HDLC spec.
    expect(crc32(new TextEncoder().encode("123456789"))).toBe(0xcbf43926);
  });

  it("is 0 for empty input", () => {
    expect(crc32(new Uint8Array())).toBe(0);
  });
});

describe("isSafeEntryName", () => {
  it("accepts the paths Studio stores", () => {
    for (const path of ["index.html", "src/App.tsx", "src/components/Hero.tsx", "a/b/c.css"]) {
      expect(isSafeEntryName(path)).toBe(true);
    }
  });

  it("refuses anything that would extract somewhere else", () => {
    for (const path of [
      "",
      "../escape.txt",
      "a/../../escape.txt",
      "/etc/passwd",
      "C:/windows",
      "a\\b.txt",
      "a//b.txt",
      "trailing/",
    ]) {
      expect(isSafeEntryName(path)).toBe(false);
    }
  });
});

describe("createZip", () => {
  const entries = [
    { path: "README.md", contents: "# hello\n" },
    { path: "src/App.tsx", contents: "export default () => null;\n" },
  ];

  it("writes a local header, a central directory and an end record", () => {
    const zip = createZip(entries, FIXED_TIME);

    expect(zip.readUInt32LE(0)).toBe(0x04034b50);
    // The end-of-central-directory signature sits in the last 22 bytes, because
    // there is no archive comment.
    expect(zip.readUInt32LE(zip.length - 22)).toBe(0x06054b50);
    expect(zip.readUInt16LE(zip.length - 22 + 8)).toBe(entries.length);
    expect(zip.readUInt16LE(zip.length - 22 + 10)).toBe(entries.length);
  });

  it("stores the names and sizes the format promises", () => {
    const zip = createZip(entries, FIXED_TIME);
    const nameLength = zip.readUInt16LE(26);

    expect(zip.subarray(30, 30 + nameLength).toString("utf8")).toBe("README.md");
    expect(zip.readUInt32LE(22)).toBe(entries[0].contents.length);
  });

  it("deflates text and stores what deflate cannot shrink", () => {
    const text = "const value = 1;\n".repeat(200);
    const withText = createZip([{ path: "big.js", contents: text }], FIXED_TIME);

    expect(withText.readUInt16LE(8)).toBe(8); // deflate
    expect(withText.readUInt32LE(18)).toBeLessThan(text.length);
    expect(withText.readUInt32LE(22)).toBe(text.length);

    // Random bytes do not compress, and a zip entry that grows its own contents
    // would be worse than the uncompressed original. (A repeating pattern does not
    // do — deflate finds its period and shrinks it, which is the bug this test had
    // first.)
    const noise = randomBytes(512);
    const withNoise = createZip([{ path: "blob.bin", contents: noise }], FIXED_TIME);

    expect(withNoise.readUInt16LE(8)).toBe(0); // stored
    expect(withNoise.readUInt32LE(18)).toBe(noise.length);
  });

  it("is deterministic for a fixed timestamp", () => {
    expect(createZip(entries, FIXED_TIME).equals(createZip(entries, FIXED_TIME))).toBe(true);
  });

  it("skips a duplicate path rather than writing it twice", () => {
    const zip = createZip([...entries, { path: "README.md", contents: "changed" }], FIXED_TIME);
    expect(zip.readUInt16LE(zip.length - 22 + 8)).toBe(2);
  });

  it("refuses a path that would escape the archive", () => {
    expect(() => createZip([{ path: "../evil.sh", contents: "rm -rf /" }], FIXED_TIME)).toThrow(
      /unsafe path/i,
    );
  });

  it("refuses more than it can index without zip64", () => {
    // The limit is passed in rather than reached: allocating the real one would be
    // 3.5 GB, and the guard is what is under test, not the constant.
    expect(MAX_ARCHIVE_BYTES).toBeGreaterThan(0);
    expect(() =>
      createZip([{ path: "huge.bin", contents: "x".repeat(4096) }], FIXED_TIME, 1024),
    ).toThrow(/too large/i);
  });

  it("handles an empty entry list without producing a broken file", () => {
    const zip = createZip([], FIXED_TIME);
    expect(zip.length).toBe(22);
    expect(zip.readUInt32LE(zip.length - 22)).toBe(0x06054b50);
    expect(zip.readUInt16LE(zip.length - 22 + 8)).toBe(0);
  });

  // The assertion that matters: something other than this writer reads it back.
  it("is unpacked by the system unzip, when one is available", () => {
    let unzip: string | null = null;
    try {
      unzip = execFileSync("sh", ["-c", "command -v unzip"], { encoding: "utf8" }).trim() || null;
    } catch {
      unzip = null;
    }
    if (!unzip) return; // Not every host has unzip; the format checks above still ran.

    const dir = scratch();
    const target = join(dir, "site.zip");
    writeFileSync(target, createZip(entries, FIXED_TIME));

    execFileSync(unzip, ["-q", target, "-d", join(dir, "out")]);

    expect(readFileSync(join(dir, "out", "README.md"), "utf8")).toBe("# hello\n");
    expect(readFileSync(join(dir, "out", "src", "App.tsx"), "utf8")).toBe(
      "export default () => null;\n",
    );
  });
});
