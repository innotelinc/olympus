---
description: Build the app described by a build-request spec, as real files in the app directory.
argument-hint: (no arguments -- receives the spec and the target directory)
---

# Build the application

You are building **{{TITLE}}** from a written specification. Your working directory
is the app directory:

    {{APP_DIR}}

## The specification

{{SPEC_BODY}}

{{PLAN}}

## What to produce

Write the application into the working directory as **real files**. The plan above —
when there is one — decides the stack, the commands and the files, because it is what
the project is packaged and run from. The spec's "Key Features & Pages" decides the
behaviour, and its "Reference build" section — when present — is a previous Studio
build of the same app: use it for intent and details, not as a structure you must
preserve.

- Implement **every** feature the spec lists. A feature you skip is the one that gets
  noticed; a feature you half-build is worse than one you leave out, because the spec
  then reads as satisfied.
- Every file the app needs must be written. An `index.html` that references
  `styles.css` or `app.js` requires those files to exist beside it.
- If the spec names a verification command, make the app actually pass it. Write the
  tests it implies if none exist and the stack supports them.
- Prefer a working app over an elaborate one. No placeholder copy, no `TODO`, no
  `// implement this` — the next reader is the person who asked for the app.

## Constraints

- **Only write inside the working directory.** Do not edit anything outside it, and
  do not create a git repository.
- No network at runtime: an app that needs a reachable API it does not ship is not a
  finished app.
- Write no `plan.json` and no `MANIFEST.json`: those are the workflow's records, not
  the app's, and a plan you write would be overwritten by the one already here.
- Match the stack the plan names, or the one the spec declares when there is no plan.
  Do not add a build step, a framework, or a dependency that neither asks for.

## When you are done

Stop. Do not summarise the files in prose — the files are the deliverable, and a
description of an app that was not written is the failure this workflow exists to
prevent. If you could not implement something the spec requires, say so plainly and
specifically in your final message.
