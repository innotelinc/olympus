import { headers } from "next/headers";
import { redirect } from "next/navigation";
import AdminPanel from "@/components/AdminPanel";
import { isAdmin, readAuthConfig, readSession } from "@/lib/auth";

// The page depends on the incoming session cookie, so it is never prerendered.
export const dynamic = "force-dynamic";

/**
 * The deployment's own panel: what the factory reads, what the runner is doing,
 * and which of the estate's rules this image is currently breaking.
 *
 * **The page is a shell; the data is behind the API.** The gate here mirrors
 * `app/page.tsx` — session, then group — because a browser cannot send the
 * `x-studio-token` header a page navigation would need, and gating the shell on it
 * would make a token-mode deployment unable to open its own panel. The route
 * (`/api/admin/status`) applies the strict gate, so the shell leaks nothing: it is
 * chrome until the fetch answers, and the fetch is what `authorizeAdmin` guards.
 *
 * The denial is its own screen rather than a redirect, because the two failures
 * need different fixes: no session means sign in again, and a session outside the
 * admin group means ask for the group. Bouncing both to the provider loops the
 * second one forever.
 */
export default async function AdminPage() {
  const config = readAuthConfig();
  const requestHeaders = await headers();
  const session = config ? readSession(requestHeaders.get("cookie"), config) : null;

  if (config && !session) redirect("/api/auth/login");

  if (!isAdmin(session, config)) {
    return (
      <main className="welcome">
        <div className="welcome-card">
          <div className="brand">
            Studio <span>Olympus</span>
          </div>
          <h1>Not your deployment to administer</h1>
          <p>
            You are signed in as {session?.email ?? session?.name ?? "an unknown identity"}, and
            this account is not in a group allowed to read the deployment panel.
          </p>
          <p className="welcome-foot">
            Ask for membership of the group in <code>OLYMPUS_ADMIN_GROUPS</code>, then sign in
            again — the group is re-checked on every request, so the change takes effect at once.
          </p>
          <a className="primary welcome-cta" href="/">
            Back to the builder
          </a>
        </div>
      </main>
    );
  }

  return <AdminPanel viewer={session?.email ?? session?.name ?? null} />;
}
