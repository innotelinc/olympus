import { authorizeRequest } from "@/lib/auth";
import { ProjectError, listProjects, saveProject } from "@/lib/projects";
import { libraryNamespace } from "@/lib/identities";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * The saved-app library.
 *
 * GET lists this identity's apps; POST creates one or updates it in place. Both
 * go through the same gate as `/api/generate`, and both resolve the caller's
 * namespace from the session subject rather than from anything the client sends
 * — a user cannot name whose library they are reading.
 */

const NO_STORE = { "cache-control": "no-store" } as const;

function fail(message: string, status: number): Response {
  return Response.json({ error: message }, { status, headers: NO_STORE });
}

export async function GET(request: Request): Promise<Response> {
  const gate = authorizeRequest(request);
  if (!gate.ok) return gate.response;

  const namespace = await libraryNamespace(gate);
  return Response.json({ projects: listProjects(namespace) }, { headers: NO_STORE });
}

export async function POST(request: Request): Promise<Response> {
  const gate = authorizeRequest(request);
  if (!gate.ok) return gate.response;

  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return fail("Request body must be JSON.", 400);
  }

  const body = (payload ?? {}) as Record<string, unknown>;
  const namespace = await libraryNamespace(gate);

  try {
    const { project, created } = saveProject(namespace, body);
    return Response.json({ project, created }, { status: created ? 201 : 200, headers: NO_STORE });
  } catch (error) {
    if (error instanceof ProjectError) return fail(error.message, error.status);
    // Filesystem refused (read-only volume, permissions) — say so instead of a 500.
    const detail = error instanceof Error ? error.message : "unknown error";
    return fail(`Could not save this app: ${detail.slice(0, 200)}`, 500);
  }
}
