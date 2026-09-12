import { headers } from "next/headers";
import { redirect } from "next/navigation";
import Studio from "@/components/Studio";
import { readAuthConfig, readSession } from "@/lib/auth";

// The page depends on the incoming session cookie, so it is never prerendered.
export const dynamic = "force-dynamic";

export default async function Page() {
  const config = readAuthConfig();
  const cookieHeader = (await headers()).get("cookie");
  const session = config ? readSession(cookieHeader, config) : null;

  // When OIDC is configured, an unauthenticated visitor is sent to the provider.
  if (config && !session) redirect("/api/auth/login");

  return <Studio user={session?.name ?? session?.email ?? null} />;
}
