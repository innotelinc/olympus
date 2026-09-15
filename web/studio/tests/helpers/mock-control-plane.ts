import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";

/**
 * A local stand-in for Distro's control plane.
 *
 * Tests drive the real client over real HTTP, so the thing under test includes
 * which header carries which credential — a `fetch` stub would let Studio and the
 * plane drift apart without a test noticing. The knobs (`identityStatus`,
 * `quotaStatus`, …) exist to drive the failure paths, which are the interesting
 * ones: a turn that must be refused, and a plane that must not become an outage.
 */

export type FakeControlPlane = {
  url: string;
  identityCalls: Array<{ headers: Record<string, string | undefined>; body: Record<string, unknown> }>;
  quotaHeaders: Array<Record<string, string | undefined>>;
  usage: Array<{ headers: Record<string, string | undefined>; body: Record<string, unknown> }>;
  audits: Array<{ headers: Record<string, string | undefined>; body: Record<string, unknown> }>;
  /** Identity provisioning answer; ≥ 400 answers with `identityError`. */
  identityStatus: number;
  identityError: string;
  /** The account the plane returns. */
  userId: string;
  gatewayKey: string;
  created: boolean;
  /** Quota answer; `quotaStatus` ≥ 400 simulates a decision that cannot be read. */
  quotaAllowed: boolean;
  quotaReasons: string[];
  quotaStatus: number;
  close: () => Promise<void>;
};

export async function startFakeControlPlane(
  overrides: Partial<FakeControlPlane> = {},
): Promise<FakeControlPlane> {
  const fake: FakeControlPlane = {
    url: "",
    identityCalls: [],
    quotaHeaders: [],
    usage: [],
    audits: [],
    identityStatus: 200,
    identityError: "nope",
    userId: "cp-user-1",
    gatewayKey: "sk-per-user",
    created: true,
    quotaAllowed: true,
    quotaReasons: [],
    quotaStatus: 200,
    close: async () => {},
    ...overrides,
  };

  const server: Server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => chunks.push(chunk));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      const body = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
      const headers = req.headers as Record<string, string | undefined>;
      const json = (status: number, payload: unknown): void => {
        res.writeHead(status, { "content-type": "application/json" });
        res.end(JSON.stringify(payload));
      };

      if (req.url === "/api/internal/identity" && req.method === "POST") {
        fake.identityCalls.push({ headers, body });
        if (fake.identityStatus >= 400) {
          json(fake.identityStatus, { error: fake.identityError });
          return;
        }
        json(200, {
          user: { id: fake.userId, email: "dev@example.com", role: "user" },
          oidcSub: body.sub,
          created: fake.created,
          gatewayKeyId: "key-1",
          gatewayKey: fake.gatewayKey,
          quota: { plan: "pro", requests_per_day: 100, tokens_per_day: null, spend_cap_usd: 5 },
          usageToday: { date: "2026-09-14", tokens_in: 1, tokens_out: 2, requests: 3, cost_usd: 0.01 },
        });
        return;
      }

      if (req.url === "/api/internal/quota-check") {
        fake.quotaHeaders.push(headers);
        if (fake.quotaStatus >= 400) {
          json(fake.quotaStatus, { error: "boom" });
          return;
        }
        json(200, { allowed: fake.quotaAllowed, reasons: fake.quotaReasons });
        return;
      }

      if (req.url === "/api/internal/usage-report") {
        fake.usage.push({ headers, body });
        json(200, { ok: true });
        return;
      }

      if (req.url === "/api/internal/audit") {
        fake.audits.push({ headers, body });
        json(201, { ok: true });
        return;
      }

      json(404, { error: "not found" });
    });
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  fake.url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  fake.close = () =>
    new Promise<void>((resolve) => {
      server.close(() => resolve());
    });
  return fake;
}
