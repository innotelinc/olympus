import { authorizeRequest } from "@/lib/auth";
import {
  FactorySpecError,
  buildFactorySpec,
  factoryRequestsDir,
  specSlug,
  writeFactorySpec,
} from "@/lib/factory-spec";
import { readProject } from "@/lib/projects";
import { libraryNamespace } from "@/lib/identities";
import { auditBuild } from "@/lib/tenancy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Export a saved app to the factory.
 *
 * `POST` writes the spec into the repository's `build-requests/` (bind-mounted
 * into this container), which is the directory the factory already manufactures
 * from — locally via `make app SPEC=…`, and in CI on push. `GET` returns the
 * same bytes as a download, so the handoff still works on a deployment where
 * `build-requests/` is not mounted.
 *
 * Both go through the same gate as every other route, and the app is resolved
 * from the caller's namespace — a caller cannot export someone else's app, and
 * cannot name where the file lands.
 */

const NO_STORE = { "cache-control": "no-store" } as const;

function fail(message: string, status: number): Response {
  return Response.json({ error: message }, { status, headers: NO_STORE });
}

type Context = { params: Promise<{ id: string }> };

export async function GET(request: Request, context: Context): Promise<Response> {
  const gate = authorizeRequest(request);
  if (!gate.ok) return gate.response;

  const { id } = await context.params;
  const project = readProject(await libraryNamespace(gate), id);
  if (!project) return fail("No such saved app.", 404);

  const spec = buildFactorySpec(project);

  return new Response(spec.markdown, {
    headers: {
      ...NO_STORE,
      "content-type": "text/markdown; charset=utf-8",
      "content-disposition": `attachment; filename="${spec.filename}"`,
    },
  });
}

export async function POST(request: Request, context: Context): Promise<Response> {
  const gate = authorizeRequest(request);
  if (!gate.ok) return gate.response;

  const { id } = await context.params;
  const project = readProject(await libraryNamespace(gate), id);
  if (!project) return fail("No such saved app.", 404);

  // An optional JSON body: `{ "overwrite": true }` replaces an existing spec.
  // Anything unreadable is treated as no body rather than a 400 — the write is
  // the request, and the flag is a detail.
  let overwrite = false;
  try {
    const payload = (await request.json()) as unknown;
    if (typeof payload === "object" && payload !== null) {
      overwrite = (payload as Record<string, unknown>).overwrite === true;
    }
  } catch {
    /* no body */
  }

  try {
    const result = writeFactorySpec(project, { overwrite });
    // Exporting to the factory is one of the three actions worth an audit row
    // (build, publish, export): it is what puts a spec in front of the factory
    // and, from there, in front of a name. Best-effort, so a tenancy hiccup can
    // never fail a write that already happened.
    await auditBuild(gate, "build.export", {
      targetId: project.id,
      meta: { slug: specSlug(project.title), spec: result.filename, replaced: result.replaced },
    });
    return Response.json(
      {
        ...result,
        requestsDir: factoryRequestsDir(),
        next: `make app SPEC=build-requests/${result.filename}`,
      },
      { status: result.replaced ? 200 : 201, headers: NO_STORE },
    );
  } catch (error) {
    if (error instanceof FactorySpecError) return fail(error.message, error.status);

    const detail = error instanceof Error ? error.message : "unknown error";
    return fail(`Could not export this app: ${detail.slice(0, 200)}`, 500);
  }
}
