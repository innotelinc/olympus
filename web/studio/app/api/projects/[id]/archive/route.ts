import { existsSync, readFileSync, statSync } from "node:fs";
import { join, resolve, sep } from "node:path";
import { authorizeRequest } from "@/lib/auth";
import { createZip, type ArchiveEntry } from "@/lib/archive";
import { specSlug } from "@/lib/factory-spec";
import { loadRepoEnv } from "@/lib/env";
import { namespaceFor, readProject, type Project } from "@/lib/projects";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * The app or site, as a zip a browser can save.
 *
 * Two paths, and the first is the one that matters:
 *
 *   1. A **packaged website** has a real archive on disk — `builds/<slug>/site.zip`,
 *      written by `scripts/package-website.py` — containing the Vite project, the
 *      source and the built `dist/`. That is the artifact the publish step stages
 *      and the archive a host would deploy, so it is served verbatim rather than
 *      reconstructed here. Rebuilding it would risk the download and the deploy
 *      being different files.
 *
 *   2. Everything else — an app, or a website that has not been packaged yet —
 *      is archived from the saved files directly. For an app that is the complete
 *      deliverable. For an un-packaged website it is the source plus a README
 *      naming the command that turns it into something servable, because a zip
 *      that silently contains no `dist/` is a zip the recipient will misread.
 *
 * `builds/` is mounted read-only into this container; when it is not, this falls
 * back to path 2 rather than failing.
 */

const NO_STORE = { "cache-control": "no-store" } as const;

function fail(message: string, status: number): Response {
  return Response.json({ error: message }, { status, headers: NO_STORE });
}

type Context = { params: Promise<{ id: string }> };

/** Where packaged builds live, or null when the directory is not mounted. */
export function buildsDir(): string | null {
  loadRepoEnv();
  const configured = process.env.STUDIO_BUILDS_DIR?.trim();
  const dir = configured ? resolve(configured) : "/app/builds";

  try {
    if (!statSync(dir).isDirectory()) return null;
  } catch {
    return null;
  }
  return dir;
}

/**
 * The packaged archive for a slug, if one exists.
 *
 * The slug is checked before it is joined: it comes from the project's own title,
 * but a joined path is a joined path, and the target is confirmed to stay inside
 * the builds directory rather than assumed to.
 */
export function packagedArchive(slug: string): string | null {
  const dir = buildsDir();
  if (!dir) return null;

  const target = join(dir, slug, "site.zip");
  if (!target.startsWith(dir + sep) || !existsSync(target)) return null;

  return target;
}

const WEBSITE_README = `# {title}

Generated in Olympus Studio as a **website** and exported before it was packaged.

\`src/\` is the source. It does not run on its own — it is React and TypeScript, and
both need a build. To turn it into a servable site:

    python3 scripts/package-website.py <slug>

That generates the Vite project around \`src/\`, installs the pinned dependencies
and writes \`dist/\`, which is what you deploy. Studio's "Build & publish" does this
for you and downloads the packaged archive instead of this one.
`;

const APP_README = `# {title}

Generated in Olympus Studio as a self-contained **app**.

\`index.html\` is the entry point and the whole app: styles and behaviour are either
inlined or beside it. Open it directly, or copy these files onto any static host.
There is no build step and no dependency to install.
`;

function archiveEntries(project: Project): ArchiveEntry[] {
  const entries: ArchiveEntry[] = project.files.map((file) => ({
    path: file.path,
    contents: file.contents,
  }));

  if (!entries.some((entry) => entry.path.toLowerCase() === "readme.md")) {
    const template = project.kind === "website" ? WEBSITE_README : APP_README;
    entries.unshift({
      path: "README.md",
      contents: template
        .replace("{title}", project.title || "Untitled")
        .replace("<slug>", specSlug(project.title)),
    });
  }

  return entries;
}

export async function GET(request: Request, context: Context): Promise<Response> {
  const gate = authorizeRequest(request);
  if (!gate.ok) return gate.response;

  const { id } = await context.params;
  const project = readProject(namespaceFor(gate.session?.sub), id);
  if (!project) return fail("No such saved app.", 404);

  const slug = specSlug(project.title);
  const packaged = project.kind === "website" ? packagedArchive(slug) : null;

  let body: Buffer;
  let filename: string;

  if (packaged) {
    // Served as bytes from disk. The file the operator downloads is then the exact
    // archive the packaging step produced and the publish step staged.
    try {
      body = readFileSync(packaged);
      filename = `${slug}.site.zip`;
    } catch {
      return fail("The packaged archive could not be read.", 500);
    }
  } else {
    try {
      body = createZip(archiveEntries(project));
      filename = `${slug}.zip`;
    } catch (error) {
      // `createZip` refuses unsafe names and oversized input; both are the
      // caller's data, not a server fault.
      const detail = error instanceof Error ? error.message : "unknown error";
      return fail(detail.slice(0, 200), 413);
    }
  }

  return new Response(new Uint8Array(body), {
    status: 200,
    headers: {
      ...NO_STORE,
      "content-type": "application/zip",
      "content-length": String(body.byteLength),
      "content-disposition": `attachment; filename="${filename}"`,
    },
  });
}
