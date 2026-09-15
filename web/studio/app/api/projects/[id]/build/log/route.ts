import { authorizeRequest } from "@/lib/auth";
import { MAX_LOG_BYTES, readBuildLog } from "@/lib/build-queue";
import { specSlug } from "@/lib/factory-spec";
import { readProject } from "@/lib/projects";
import { libraryNamespace } from "@/lib/identities";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * One build's log, for the app whose library asked for it.
 *
 * `GET /api/projects/<id>/build/log?job=<16 hex>`
 *
 * The log is not in the status file. The runner keeps a 4000-character tail there
 * because it rewrites that file every five seconds and the panel polls it; a real
 * build's log is tens of kilobytes, and shipping it in every poll would replace a
 * progress indicator with a bandwidth problem. So the tail is the live view and
 * this route is the record.
 *
 * OWNERSHIP IS CHECKED, NOT ASSUMED. The queue directory is shared by every tenant
 * of this deployment — the runner is one process with one queue — so a job id
 * alone would let any signed-in user read any other user's build log by guessing
 * 16 hex characters. The job has to belong to this app's slug, which is derived
 * from the caller's own saved app, and a job that does not match answers 404 the
 * same as one that does not exist: telling the difference would confirm someone
 * else's build.
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

  const job = (new URL(request.url).searchParams.get("job") ?? "").trim();
  if (!job) return fail("A job id is required to read a build log.", 400);

  const log = readBuildLog(job);
  if (!log || log.slug !== specSlug(project.title)) {
    return fail("No such build for this app.", 404);
  }

  return Response.json(
    { ...log, limitBytes: MAX_LOG_BYTES },
    { headers: NO_STORE },
  );
}
