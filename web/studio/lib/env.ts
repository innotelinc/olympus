import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

/**
 * Studio reads the same repo-root `.env` the rest of Olympus uses: credentials
 * live in the SecretOps workspace and the `.env` is derived from it (gitignored).
 *
 * Next.js only auto-loads env files from its own project directory, so we walk
 * upward from the process cwd and adopt the nearest `.env` then `.env.local`,
 * stopping at the repository root (the directory holding `.git`).
 *
 * Real process env always wins — `docker compose` env_file, the operator's
 * shell, and CI must never be shadowed by a file on disk.
 */

const ENV_FILENAMES = [".env", ".env.local"] as const;
const MAX_WALK_UP = 5;

let loaded = false;

function parseEnvFile(text: string): Record<string, string> {
  const parsed: Record<string, string> = {};

  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;

    const separator = line.indexOf("=");
    if (separator === -1) continue;

    const key = line.slice(0, separator).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue;

    let value = line.slice(separator + 1).trim();
    const isQuoted =
      value.length >= 2 &&
      ((value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'")));

    if (isQuoted) {
      value = value.slice(1, -1);
    } else {
      // Drop an unquoted trailing comment: KEY=value  # note
      value = value.replace(/\s+#.*$/, "").trim();
    }

    parsed[key] = value;
  }

  return parsed;
}

/**
 * Merge the nearest env files into process.env without overwriting anything
 * already set. Idempotent and cheap; safe to call per request.
 */
export function loadRepoEnv(): void {
  if (loaded) return;
  loaded = true;

  let dir = process.cwd();

  for (let depth = 0; depth <= MAX_WALK_UP; depth += 1) {
    for (const name of ENV_FILENAMES) {
      // turbopackIgnore: the walk is intentionally dynamic. Without this, the
      // build tracer treats the whole project as a dependency of the standalone
      // server bundle.
      const file = join(/* turbopackIgnore: true */ dir, name);
      if (!existsSync(file)) continue;

      let parsed: Record<string, string>;
      try {
        parsed = parseEnvFile(readFileSync(file, "utf8"));
      } catch {
        continue;
      }

      for (const [key, value] of Object.entries(parsed)) {
        if (process.env[key] === undefined) process.env[key] = value;
      }
    }

    // The repository root is the boundary — do not read a parent workspace's env.
    if (existsSync(join(dir, ".git"))) return;

    const parent = dirname(dir);
    if (parent === dir) return;
    dir = parent;
  }
}
