import { authorizeRequest } from "@/lib/auth";
import { deleteProject, readProject } from "@/lib/projects";
import { libraryNamespace } from "@/lib/identities";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** One saved app: fetch it, or delete it. Scoped to the caller's namespace. */

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

  return Response.json({ project }, { headers: NO_STORE });
}

export async function DELETE(request: Request, context: Context): Promise<Response> {
  const gate = authorizeRequest(request);
  if (!gate.ok) return gate.response;

  const { id } = await context.params;
  const namespace = await libraryNamespace(gate);

  // A missing id is reported as 404 rather than silently succeeding: the client
  // is showing a list that just changed underneath it.
  if (!readProject(namespace, id)) return fail("No such saved app.", 404);
  if (!deleteProject(namespace, id)) return fail("Could not delete this app.", 500);

  return Response.json({ deleted: id }, { headers: NO_STORE });
}
