import { FLOW_COOKIE, SESSION_COOKIE, clearCookie } from "@/lib/auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(): Promise<Response> {
  const headers = new Headers({ location: "/", "cache-control": "no-store" });
  headers.append("set-cookie", clearCookie(SESSION_COOKIE));
  headers.append("set-cookie", clearCookie(FLOW_COOKIE));
  return new Response(null, { status: 307, headers });
}
