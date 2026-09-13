/**
 * A minimal ZIP writer.
 *
 * WHY THIS IS HERE AND NOT A DEPENDENCY. "Publish to a zip file" is a delivery
 * path, so it has to work on the deployment where it is offered. Studio's image is
 * deliberately bare — no shell, no `zip` binary, and nothing to add one to at
 * runtime — so archiving has to be code. A ZIP file is a well-specified format
 * with three record types, and this module writes all three; pulling in an
 * archiver package to do it would add a supply-chain edge to a route whose only
 * job is concatenation.
 *
 * Deflate comes from `node:zlib`, which is already in the runtime. Every entry is
 * deflated when that helps and **stored** when it does not: a PNG or an already
 * minified bundle can be larger after deflate, and a zip that grows its contents is
 * a zip nobody can explain.
 *
 * Deliberately not supported: encryption, zip64 (>4 GiB), symlinks, directory
 * entries. The first two are refused rather than half-implemented (see
 * `MAX_ARCHIVE_BYTES`), and the last two do not occur in generated source.
 */

import { deflateRawSync } from "node:zlib";

/** Beyond this the archive is refused — outside zip64 and not what a browser made. */
export const MAX_ARCHIVE_BYTES = 3_500_000_000;

/** How many bytes of a name a ZIP can hold, per spec. */
const MAX_NAME_BYTES = 0xffff;

export type ArchiveEntry = {
  /** Path inside the archive. Normalized by the caller; validated here. */
  path: string;
  contents: string | Uint8Array;
};

/* ---- crc32 --------------------------------------------------------------- */

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let index = 0; index < 256; index += 1) {
    let value = index;
    for (let bit = 0; bit < 8; bit += 1) {
      value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    }
    table[index] = value >>> 0;
  }
  return table;
})();

export function crc32(data: Uint8Array): number {
  let crc = 0xffffffff;
  for (let index = 0; index < data.length; index += 1) {
    crc = CRC_TABLE[(crc ^ data[index]) & 0xff]! ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

/* ---- helpers ------------------------------------------------------------- */

function toBytes(contents: string | Uint8Array): Uint8Array {
  return typeof contents === "string" ? new TextEncoder().encode(contents) : contents;
}

/**
 * Reject anything that would not round-trip.
 *
 * An entry name with a separator at the front, a `..` segment, or a backslash is a
 * name that some extractor will resolve somewhere other than inside the archive —
 * the classic zip-slip. Studio's stored paths are already normalized, but this is
 * the boundary that writes a file the operator will extract on their own machine,
 * so it re-checks rather than trusting its caller.
 */
export function isSafeEntryName(path: string): boolean {
  if (!path || path.length > 200) return false;
  if (path.includes("\\")) return false;
  if (path.startsWith("/") || /^[a-zA-Z]:/.test(path)) return false;
  return !path.split("/").some((segment) => segment === ".." || segment === "");
}

/* ---- the archive --------------------------------------------------------- */

type Prepared = {
  name: Uint8Array;
  data: Uint8Array;
  deflated: Uint8Array;
  method: number;
  crc: number;
  /** Offset of this entry's local header, filled in as it is written. */
  offset: number;
};

/** DOS date/time, which is what the format stores. Not a precision problem. */
function dosStamp(date: Date): { time: number; date: number } {
  const time =
    ((date.getHours() & 0x1f) << 11) |
    ((date.getMinutes() & 0x3f) << 5) |
    ((Math.floor(date.getSeconds() / 2) & 0x1f) << 0);
  const stamp =
    (((date.getFullYear() - 1980) & 0x7f) << 9) |
    (((date.getMonth() + 1) & 0x0f) << 5) |
    (date.getDate() & 0x1f);
  return { time, date: stamp };
}

/**
 * Build a ZIP archive.
 *
 * Deterministic for a given entry list and timestamp: entries appear in the order
 * they were given, so the same build produces the same bytes and a regression in
 * the writer is a diff rather than a surprise. `modified` is injectable so tests
 * do not depend on the clock.
 */
export function createZip(
  entries: ArchiveEntry[],
  modified: Date = new Date(),
  /** Overridable only so the guard is testable without allocating 3.5 GB. */
  limitBytes: number = MAX_ARCHIVE_BYTES,
): Buffer {
  const prepared: Prepared[] = [];
  const encoder = new TextEncoder();
  const { time, date } = dosStamp(modified);

  let total = 0;
  const seen = new Set<string>();

  for (const entry of entries) {
    if (!isSafeEntryName(entry.path)) {
      throw new Error(`Refusing to put "${entry.path}" in an archive: unsafe path.`);
    }
    if (seen.has(entry.path)) continue;
    seen.add(entry.path);

    const name = encoder.encode(entry.path);
    if (name.length > MAX_NAME_BYTES) {
      throw new Error(`Refusing to archive "${entry.path}": name is too long.`);
    }

    const data = toBytes(entry.contents);
    const deflated = deflateRawSync(data);
    // Keep whichever is smaller. A stored entry is legal, and it is the honest
    // choice for data deflate could not help.
    const useDeflate = deflated.length < data.length;

    total += data.length + name.length + 100;
    if (total > limitBytes) {
      throw new Error("This is too large to archive as a plain zip (over 3.5 GB).");
    }

    prepared.push({
      name,
      data,
      deflated: useDeflate ? deflated : data,
      method: useDeflate ? 8 : 0,
      crc: crc32(data),
      offset: 0,
    });
  }

  const chunks: Buffer[] = [];
  let offset = 0;

  for (const item of prepared) {
    item.offset = offset;

    const header = Buffer.alloc(30);
    header.writeUInt32LE(0x04034b50, 0); // local file header
    header.writeUInt16LE(20, 4); // version needed
    header.writeUInt16LE(0x0800, 6); // UTF-8 names
    header.writeUInt16LE(item.method, 8);
    header.writeUInt16LE(time, 10);
    header.writeUInt16LE(date, 12);
    header.writeUInt32LE(item.crc, 14);
    header.writeUInt32LE(item.deflated.length, 18);
    header.writeUInt32LE(item.data.length, 22);
    header.writeUInt16LE(item.name.length, 26);
    header.writeUInt16LE(0, 28); // no extra field

    chunks.push(header, Buffer.from(item.name), Buffer.from(item.deflated));
    offset += header.length + item.name.length + item.deflated.length;
  }

  // Central directory: the index, written after the data it points into.
  const directoryStart = offset;

  for (const item of prepared) {
    const record = Buffer.alloc(46);
    record.writeUInt32LE(0x02014b50, 0); // central directory header
    record.writeUInt16LE(20, 4); // version made by
    record.writeUInt16LE(20, 6); // version needed
    record.writeUInt16LE(0x0800, 8);
    record.writeUInt16LE(item.method, 10);
    record.writeUInt16LE(time, 12);
    record.writeUInt16LE(date, 14);
    record.writeUInt32LE(item.crc, 16);
    record.writeUInt32LE(item.deflated.length, 20);
    record.writeUInt32LE(item.data.length, 24);
    record.writeUInt16LE(item.name.length, 28);
    record.writeUInt16LE(0, 30); // extra
    record.writeUInt16LE(0, 32); // comment
    record.writeUInt16LE(0, 34); // disk number
    record.writeUInt16LE(0, 36); // internal attributes
    record.writeUInt32LE(0, 38); // external attributes
    record.writeUInt32LE(item.offset, 42);

    chunks.push(record, Buffer.from(item.name));
    offset += record.length + item.name.length;
  }

  const directoryBytes = offset - directoryStart;

  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); // end of central directory
  end.writeUInt16LE(0, 4); // this disk
  end.writeUInt16LE(0, 6); // disk with the directory
  end.writeUInt16LE(prepared.length, 8);
  end.writeUInt16LE(prepared.length, 10);
  end.writeUInt32LE(directoryBytes, 12);
  end.writeUInt32LE(directoryStart, 16);
  end.writeUInt16LE(0, 20); // no comment

  chunks.push(end);

  return Buffer.concat(chunks);
}
