import { authorizeRequest } from "@/lib/auth";
import { BuildQueueError, requestCancel } from "@/lib/build-queue";
import { namespaceFor, readProject } from "@/lib/projects";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Ask the host runner to stop a running build.
 *
 * Studio cannot signal it: the runner is a host process and this is a container.
 * So the request is a marker file in the shared queue directory, and the runner
 * — which already polls that directory, and already has to reap its own build
 * tree on timeout — acts on it within a second.
 *
 * This answers 202 rather than "cancelled": the marker is a request, and the
 * truth arrives in the status file the panel is already polling. Reporting
 * success here would mean claiming a stop that had not happened yet.
 *
 * `POST /api/projects/<id>/build/cancel` with `{ "job": "<16 hex>" }`.
 */

const NO_STORE = { "cache-control": "no-store" } as const;

function fail(message: string, status: number): Response {
  return Response.json({ error: message }, { status, headers: NO_STORE });
}

type Context = { params: Promise<{ id: string }> };

export async function POST(request: Request, context: Context): Promise<Response> {
  const gate = authorizeRequest(request);
  if (!gate.ok) return gate.response;

  const { id } = await context.params;
  const project = readProject(namespaceFor(gate.session?.sub), id);
  if (!project) return fail("No such saved app.", 404);

  let job = "";
  try {
    const payload = (await request.json()) as unknown;
    if (typeof payload === "object" && payload !== null) {
      const value = (payload as Record<string, unknown>).job;
      if (typeof value === "string") job = value.trim();
    }
  } catch {
    /* no body */
  }

  if (!job) return fail("A job id is required to cancel a build.", 400);

  try {
    const status = requestCancel(job);
    return Response.json(
      {
        requested: true,
        job: status.job,
        slug: status.slug,
        message:
          "Stop requested — the runner reports it on its next poll (within a " +
          "second), and the build is recorded as cancelled.",
      },
      { status: 202, headers: NO_STORE },
    );
  } catch (error) {
    if (error instanceof BuildQueueError) return fail(error.message, error.status);

    const detail = error instanceof Error ? error.message : "unknown error";
    return fail(`Could not request a stop: ${detail.slice(0, 200)}`, 500);
  }
}
