import { headers } from "next/headers";
import { redirect } from "next/navigation";
import Studio from "@/components/Studio";
import { readAuthConfig, readSession } from "@/lib/auth";

// The page depends on the incoming session cookie, so it is never prerendered.
export const dynamic = "force-dynamic";

/**
 * Two public hosts serve this app, and they play different roles:
 *
 *   studio.olympus.innotel.us  — the tool itself. Signing in lands directly in
 *                                the builder; nothing to click through.
 *   olympus.innotel.us         — the front door. Show what this is first, with
 *                                one button into the builder, so the root host
 *                                can be handed to someone who has never seen
 *                                the project.
 *
 * Both hosts are registered as OIDC redirect URIs (`make studio-oidc` keeps
 * them registered), so sign-in works whichever host the visitor came in on.
 * The GitHub Pages landing page remains the project's public face outside the
 * deployment; this is the in-app front door.
 */
export default async function Page() {
  const config = readAuthConfig();
  const requestHeaders = await headers();
  const cookieHeader = requestHeaders.get("cookie");
  const session = config ? readSession(cookieHeader, config) : null;

  const host = (requestHeaders.get("x-forwarded-host") ?? "").split(",")[0]?.trim();
  const isStudioHost = /^studio\./i.test(host ?? "");

  // The studio host is the tool: an unauthenticated visitor goes straight to
  // the provider rather than to a page they cannot use yet.
  if (config && !session) {
    if (isStudioHost) redirect("/api/auth/login");

    // The root host without a session shows the front door; the button starts
    // sign-in. Both hosts already have their callbacks registered, so sign-in
    // lands back here (now authenticated) or straight into the builder.
    return (
      <main className="welcome">
        <div className="welcome-card">
          <div className="brand">
            Studio <span>Olympus</span>
          </div>
          <h1>Describe an app. Watch it build.</h1>
          <p>
            Plain language in, a running web app out — streamed file by file into a
            live preview you can keep revising, all inside the browser.
          </p>
          <a className="primary welcome-cta" href="/api/auth/login">
            Sign in to start building
          </a>
          <p className="welcome-foot">
            <a href="https://innotelinc.github.io/olympus">About the Olympus platform</a>
          </p>
        </div>
      </main>
    );
  }

  return <Studio user={session?.name ?? session?.email ?? null} />;
}
