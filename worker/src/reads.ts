// Device endpoints (state, purge) and processor reads (spec 6.1).
import type { Caller } from "./auth";
import { requireKind } from "./auth";
import { fail, json } from "./errors";
import { type BatchRow, type Ctx, type DeviceRow, type GrantRow, getDevice, maxGrant } from "./context";
import { instant } from "./encoding";
import { parseSeq } from "./uploads";

function requireOwnDevice(caller: Caller, deviceId: string): void {
  const d = requireKind(caller, "device");
  if (d.id !== deviceId) fail("forbidden", "a device may only call its own endpoints");
}

export async function getState(ctx: Ctx, caller: Caller, deviceId: string): Promise<Response> {
  requireOwnDevice(caller, deviceId);
  const dev = (await getDevice(ctx, deviceId)) as DeviceRow; // exists: the caller authenticated with it
  const last = dev.max_seq > 0
    ? await ctx.env.DB.prepare("SELECT manifest_sha256 FROM batches WHERE device_id = ?1 AND seq = ?2")
        .bind(deviceId, dev.max_seq).first<{ manifest_sha256: string }>()
    : null;
  const grant = await maxGrant(ctx, deviceId);
  const usage = await ctx.env.DB.prepare(
    `SELECT
       (SELECT COUNT(*) FROM batches WHERE device_id = ?1) AS batches,
       (SELECT COALESCE(SUM(size), 0) FROM batches WHERE device_id = ?1) AS bytes,
       (SELECT MAX(received_at) FROM batches WHERE device_id = ?1) AS last_received_at,
       (SELECT COALESCE(SUM(b.size), 0) FROM batches b JOIN devices d ON d.device_id = b.device_id
          WHERE d.user_id = ?2 AND d.status != 'purged') AS user_bytes`,
  ).bind(deviceId, dev.user_id).first<{ batches: number; bytes: number; last_received_at: string | null; user_bytes: number }>();
  return json({
    status: dev.status,
    contiguous_seq: dev.contiguous_seq,
    max_seq: dev.max_seq,
    last_manifest_sha256: last?.manifest_sha256 ?? null,
    grant_seq: grant?.grant_seq ?? null,
    grant_sha256: grant?.sha256 ?? null,
    server_time: Math.floor(ctx.now / 1000),
    limits: ctx.config.limits,
    batches: usage?.batches ?? 0,
    bytes: usage?.bytes ?? 0,
    last_received_at: usage?.last_received_at ?? null,
    user_bytes: usage?.user_bytes ?? 0,
  });
}

export async function purgeDevice(ctx: Ctx, caller: Caller, deviceId: string): Promise<Response> {
  requireOwnDevice(caller, deviceId);
  const dev = (await getDevice(ctx, deviceId)) as DeviceRow;
  if (dev.status === "retired") fail("device_inactive", "a retired device cannot purge", { status: dev.status });
  if (dev.status === "active") {
    const res = await ctx.env.DB.prepare("UPDATE devices SET status = 'purged', purged_at = ?2 WHERE device_id = ?1 AND status = 'active'")
      .bind(deviceId, instant(ctx.now)).run();
    if (res.meta.changes === 0) {
      // Retired or purged concurrently: answer from what is now true.
      const now = (await getDevice(ctx, deviceId)) as DeviceRow;
      if (now.status !== "purged") fail("device_inactive", "a retired device cannot purge", { status: now.status });
    }
  }
  return json({ status: "purged" });
}

// ---------------------------------------------------------------------------- processors

interface Access {
  dev: DeviceRow;
  granted: boolean;
}

/** Spec 6.1 order: device exists (404), caller listed (403 not_granted); batch reads add purged and granted checks. */
async function processorAccess(ctx: Ctx, caller: Caller, deviceId: string, forBatches: boolean): Promise<Access> {
  const p = requireKind(caller, "processor");
  const dev = await getDevice(ctx, deviceId);
  if (!dev) fail("not_found", "no such device");
  const listed = await ctx.env.DB.prepare("SELECT 1 FROM grant_processors WHERE device_id = ?1 AND processor_id = ?2 LIMIT 1")
    .bind(deviceId, p.id).first();
  if (!listed) fail("not_granted", "processor is not listed by this device");
  const current = await maxGrant(ctx, deviceId);
  const granted = !!current && (JSON.parse(current.recipients) as string[]).includes(p.id);
  if (forBatches) {
    if (dev.status === "purged") fail("device_purged", "device was purged");
    if (!granted) fail("not_granted", "processor is not in the current grant");
  }
  return { dev, granted };
}

export async function listStreams(ctx: Ctx, caller: Caller): Promise<Response> {
  const p = requireKind(caller, "processor");
  const rows = await ctx.env.DB.prepare(
    `SELECT d.device_id, d.user_id, u.name AS user_name, d.name AS device_name, d.pub AS device_pub, d.status,
            d.contiguous_seq, d.max_seq, g.grant_seq, g.recipients
     FROM devices d
     JOIN users u ON u.user_id = d.user_id
     JOIN grants g ON g.device_id = d.device_id
       AND g.grant_seq = (SELECT MAX(grant_seq) FROM grants WHERE device_id = d.device_id)
     WHERE d.device_id IN (SELECT device_id FROM grant_processors WHERE processor_id = ?1)
     ORDER BY d.user_id, d.device_id`,
  ).bind(p.id).all<{
    device_id: string; user_id: string; user_name: string; device_name: string; device_pub: string; status: string;
    contiguous_seq: number; max_seq: number; grant_seq: number; recipients: string;
  }>();
  return json({
    streams: rows.results.map((r) => ({
      user_id: r.user_id,
      user_name: r.user_name,
      device_id: r.device_id,
      device_name: r.device_name,
      device_pub: r.device_pub,
      status: r.status,
      granted: (JSON.parse(r.recipients) as string[]).includes(p.id),
      contiguous_seq: r.contiguous_seq,
      max_seq: r.max_seq,
      grant_seq: r.grant_seq,
    })),
  });
}

export async function listGrants(ctx: Ctx, caller: Caller, deviceId: string): Promise<Response> {
  await processorAccess(ctx, caller, deviceId, false);
  const rows = await ctx.env.DB.prepare("SELECT * FROM grants WHERE device_id = ?1 ORDER BY grant_seq").bind(deviceId).all<GrantRow>();
  return json({
    grants: rows.results.map((g) => ({ grant_seq: g.grant_seq, doc: g.doc, sig: g.sig, received_at: g.received_at })),
  });
}

const INT_RE = /^(0|[1-9][0-9]{0,15})$/;

export async function listBatches(ctx: Ctx, caller: Caller, deviceId: string): Promise<Response> {
  const { dev } = await processorAccess(ctx, caller, deviceId, true);
  let after = 0;
  let limit = 500;
  const seen = new Set<string>();
  for (const [name, value] of ctx.url.searchParams) {
    if (seen.has(name) || !INT_RE.test(value)) fail("bad_request", "malformed query");
    seen.add(name);
    if (name === "after") after = Number(value);
    else if (name === "limit") limit = Number(value);
    else fail("bad_request", `unknown query parameter ${name}`);
  }
  if (!Number.isSafeInteger(after) || limit < 1 || limit > 1000) fail("bad_request", "after or limit out of range");
  const rows = await ctx.env.DB.prepare(
    "SELECT seq, manifest, sig, size, received_at FROM batches WHERE device_id = ?1 AND seq > ?2 ORDER BY seq LIMIT ?3",
  ).bind(deviceId, after, limit).all<Pick<BatchRow, "seq" | "manifest" | "sig" | "size" | "received_at">>();
  return json({ batches: rows.results, contiguous_seq: dev.contiguous_seq, max_seq: dev.max_seq });
}

export async function getBatch(ctx: Ctx, caller: Caller, deviceId: string, seqPath: string): Promise<Response> {
  await processorAccess(ctx, caller, deviceId, true);
  const seq = parseSeq(seqPath);
  const row = await ctx.env.DB.prepare("SELECT object_key, size, ciphertext_sha256 FROM batches WHERE device_id = ?1 AND seq = ?2")
    .bind(deviceId, seq).first<Pick<BatchRow, "object_key" | "size" | "ciphertext_sha256">>();
  if (!row) fail("not_found", "no such batch");
  const obj = await ctx.env.BATCHES.get(row.object_key);
  if (!obj) throw new Error(`object ${row.object_key} missing from R2`);
  return new Response(obj.body, {
    headers: {
      "content-type": "application/octet-stream",
      "content-length": String(obj.size),
      "cache-control": "no-store",
      "x-hka-ciphertext-sha256": row.ciphertext_sha256,
    },
  });
}
