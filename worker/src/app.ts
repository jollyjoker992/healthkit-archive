// Router. `handle` takes the clock as a parameter so tests can pin time.
import { listUsers, purgeUser, retireDevice } from "./admin";
import { authenticate } from "./auth";
import { type Env, loadConfig } from "./config";
import type { Ctx } from "./context";
import { enroll } from "./enroll";
import { HttpError, errorResponse, fail } from "./errors";
import { getBatch, getState, listBatches, listGrants, listStreams, purgeDevice } from "./reads";
import { putBatch, putGrant, readBounded } from "./uploads";

const D = "(d_[0-9a-f]{32})";
const U = "(u_[0-9a-f]{32})";
const SEQ = "([^/]+)";

type Route = [method: string, pattern: RegExp, handler: (ctx: Ctx, params: string[]) => Promise<Response>];

async function readBody(ctx: Ctx, max: number): Promise<Uint8Array> {
  const body = await readBounded(ctx.request, max);
  if (!body) fail("bad_request", "body too large");
  return body;
}

async function signed(ctx: Ctx) {
  return authenticate(ctx, await readBody(ctx, 1024));
}

const routes: Route[] = [
  ["POST", /^\/v1\/enroll$/, async (ctx) => enroll(ctx, await readBody(ctx, 4096))],
  ["PUT", new RegExp(`^/v1/devices/${D}/grant$`), async (ctx, [d]) => putGrant(ctx, d, await readBody(ctx, 32 * 1024))],
  ["PUT", new RegExp(`^/v1/devices/${D}/batches/${SEQ}$`), (ctx, [d, s]) => putBatch(ctx, d, s)],
  ["GET", new RegExp(`^/v1/devices/${D}/state$`), async (ctx, [d]) => getState(ctx, await signed(ctx), d)],
  ["POST", new RegExp(`^/v1/devices/${D}/purge$`), async (ctx, [d]) => purgeDevice(ctx, await signed(ctx), d)],
  ["GET", /^\/v1\/streams$/, async (ctx) => listStreams(ctx, await signed(ctx))],
  ["GET", new RegExp(`^/v1/devices/${D}/grants$`), async (ctx, [d]) => listGrants(ctx, await signed(ctx), d)],
  ["GET", new RegExp(`^/v1/devices/${D}/batches$`), async (ctx, [d]) => listBatches(ctx, await signed(ctx), d)],
  ["GET", new RegExp(`^/v1/devices/${D}/batches/${SEQ}$`), async (ctx, [d, s]) => getBatch(ctx, await signed(ctx), d, s)],
  ["GET", /^\/v1\/admin\/users$/, async (ctx) => listUsers(ctx, await signed(ctx))],
  ["POST", new RegExp(`^/v1/admin/devices/${D}/retire$`), async (ctx, [d]) => retireDevice(ctx, await signed(ctx), d)],
  ["DELETE", new RegExp(`^/v1/admin/users/${U}$`), async (ctx, [u]) => purgeUser(ctx, await signed(ctx), u)],
];

export async function handle(request: Request, env: Env, now: number): Promise<Response> {
  const nowSeconds = Math.floor(now / 1000);
  try {
    const config = await loadConfig(env);
    const url = new URL(request.url);
    const ctx: Ctx = { env, config, request, url, now };
    for (const [method, pattern, handler] of routes) {
      const match = pattern.exec(url.pathname);
      if (match && request.method === method) return await handler(ctx, match.slice(1));
    }
    fail("not_found", "no such endpoint");
  } catch (err) {
    if (err instanceof HttpError) return errorResponse(err, nowSeconds);
    console.error("internal error", err instanceof Error ? err.message : String(err));
    return errorResponse(new HttpError("internal", "internal error"), nowSeconds);
  }
}
