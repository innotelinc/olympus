/**
 * Where a planned project's data and its functions live — the one *target* a plan
 * may pick besides its own container.
 *
 * WHY THIS IS ITS OWN MODULE. Same reason `kinds.ts` is: `lib/plan.ts` is reached
 * by the browser (the page component imports `missingPlannedFiles`), so anything it
 * imports has to be filesystem-free. A target, and the one function that reads one,
 * are two words the browser and the server both need to agree about, and neither of
 * them is a file. No imports here, ever.
 *
 * WHY A TARGET AND NOT A KIND. `kind` answers *how the finished thing leaves* —
 * files staged and served, or a container run behind a name. A target answers a
 * different question: what serves its state. They are independent, and most
 * projects only ever use one combination, which is exactly why the second one
 * should be named rather than inferred.
 *
 * `container` is the default and the one that needs no explaining: the project
 * keeps its own database, on disk, beside its code, inside its own container. It
 * is what every build before this field existed is.
 *
 * `convex` means the deployment's self-hosted Convex backend (Atlas runs it)
 * serves the state: the schema and the functions are deployed to Convex, and the
 * client talks to it over HTTP/WebSocket. It is *not* a different runtime — a
 * Convex-targeted project still gets a container for its client, which is why the
 * run contract is unchanged and this is a plan field rather than a fourth kind.
 *
 * It is a plan field and not something the user picks, for the same reason `kind`
 * is not: the turn that reads the request is the one that can tell an app that
 * needs live, shared state from one that needs a file.
 *
 * It is *deliberately* not offered for a website. Static files have nowhere to
 * keep a Convex query, so a `website` that claims to target Convex is a
 * contradiction, and `parseTarget` does not have to resolve it: the planner is
 * told, and the generation contract keys on the same field.
 */
export type PlanTarget = "container" | "convex";

/** Both targets, in the order they are worth mentioning: the default first. */
export const PLAN_TARGETS: readonly PlanTarget[] = ["container", "convex"];

/**
 * Whatever a plan said, as one of the two.
 *
 * Anything that is not exactly `"convex"` is `container`, because that is the
 * target every project built before this field had. A third value cannot be
 * interpreted three ways by three readers — the packager, the runtime and the
 * export spec — so it collapses here instead.
 */
export function parseTarget(value: unknown): PlanTarget {
  return value === "convex" ? "convex" : "container";
}
