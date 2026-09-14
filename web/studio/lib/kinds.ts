/**
 * What a project turned out to be — the two *delivery* shapes, not two products.
 *
 * WHY THIS IS ITS OWN MODULE. It was in `projects.ts`, which is where a saved
 * project is read and written, and that file imports `node:fs` and `node:crypto`.
 * Everything that touches a kind therefore dragged the filesystem with it: the
 * client bundle of `app/page.tsx` reaches `lib/plan.ts`, and `lib/plan.ts` had
 * `import { parseKind } from "./projects"`. The production build only survived
 * because that import was **dead** — nothing called `parseKind` — so the bundler
 * dropped the edge and never saw `node:fs`. The first real use of it failed the
 * build with `the chunking context does not support external modules (request:
 * node:fs)`, which is a confusing way to be told that a vocabulary module owns a
 * filesystem.
 *
 * So: no imports here, ever. A kind, and the one function that reads one, are things
 * the browser and the server both need to agree about, and neither of them is a file.
 *
 * `app` is software that runs: something has to stay in the foreground and answer
 * requests, and what the person enters is still there next time. `website` is static
 * files: no server and no state, so what is served is whatever the build produced.
 *
 * Neither implies a stack. The language, the framework and the database are the
 * planner's decision and live in the project's `plan`; this is only the question of
 * which door the finished thing leaves by — a container run behind a name, or files
 * staged and served. It is *not* something the user picks: Studio used to offer it as
 * a choice, which made the request and the button two opinions that could disagree.
 */
export type ProjectKind = "app" | "website";

export const PROJECT_KINDS: readonly ProjectKind[] = ["app", "website"];

/**
 * Whatever a plan or a stored record said, as one of the two.
 *
 * The three readers of this field — the packager, the delivery wording and the
 * export spec — each handle two shapes, so a third has to collapse here rather than
 * being interpreted three ways. An answer that is neither is an app, which is what
 * every record written before this field existed meant.
 */
export function parseKind(value: unknown): ProjectKind {
  return value === "website" ? "website" : "app";
}
