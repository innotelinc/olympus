import { authorizeRequest } from "@/lib/auth";
import {
  BuildQueueError,
  FactorySpecError,
  latestBuildStatus,
  queueBuild,
  readBuildStatus,
  readRunnerState,
} from "@/lib/build-queue";
import { specSlug } from "@/lib/factory-spec";
import { namespaceFor, readProject } from "@/lib/projects";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Run this app through the factory, from the browser.
 *
 * `POST` regenerates the spec and queues a real `make app` for the host-side
 * runner (`scripts/build-runner.py`). `GET` reports where that build got to —
 * by job id while one is running, or the app's most recent build otherwise, so a
 * reload does not lose the thread.
 *
 * Studio never runs the build itself: this container has no toolchain by design.
 * The queue file is the entire interface, and the runner re-validates everything
 * in it before acting.
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
  const project = readProject(namespaceFor(gate.session?.sub), id);
  if (!project) return fail("No such saved app.", 404);

  const job = new URL(request.url).searchParams.get("job");
  if (job !== null) {
    // A malformed id is a 400 rather than a 404: it is a caller bug, not a
    // missing build, and readBuildStatus refuses to use it as a path.
    const status = readBuildStatus(job);
    if (!status) return fail("No such build job.", 404);
    return Response.json({ build: status, runner: readRunnerState() }, { headers: NO_STORE });
  }

  return Response.json(
    {
      build: latestBuildStatus(specSlug(project.title)),
      runner: readRunnerState(),
      next: `make app SPEC=build-requests/${specSlug(project.title)}.md`,
    },
    { headers: NO_STORE },
  );
}

export async function POST(request: Request, context: Context): Promise<Response> {
  const gate = authorizeRequest(request);
  if (!gate.ok) return gate.response;

  const { id } = await context.params;
  const project = readProject(namespaceFor(gate.session?.sub), id);
  if (!project) return fail("No such saved app.", 404);

  if (project.files.length === 0) {
    return fail("This app has no files yet — build something in Studio first.", 409);
  }

  // `{ "replace": true }` is the operator agreeing to overwrite an existing spec
  // and/or an existing `builds/<slug>`. Without it, either existing artifact is a
  // conflict rather than something quietly destroyed.
  let replace = false;
  try {
    const payload = (await request.json()) as unknown;
    if (typeof payload === "object" && payload !== null) {
      replace = (payload as Record<string, unknown>).replace === true;
    }
  } catch {
    /* no body */
  }

  try {
    const queued = queueBuild(project, { replace });
    return Response.json(
      {
        job: queued.job,
        slug: queued.slug,
        spec: queued.spec,
        replaced: queued.replaced,
        runner: queued.runner,
        build: readBuildStatus(queued.job),
        next: `make app SPEC=${queued.spec}`,
      },
      { status: 202, headers: NO_STORE },
    );
  } catch (error) {
    if (error instanceof FactorySpecError || error instanceof BuildQueueError) {
      return fail(error.message, error.status);
    }

    const detail = error instanceof Error ? error.message : "unknown error";
    return fail(`Could not start this build: ${detail.slice(0, 200)}`, 500);
  }
}
