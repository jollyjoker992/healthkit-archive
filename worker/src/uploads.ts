// Grant upload (spec 4.2) and batch upload (spec 4.3).
import { importPublicKey, verify } from "./crypto";
import { parseGrant, parseManifest } from "./documents";
import { b64uDecode, b64uEncode, bytesEqual, concatBytes, hexEncode, instant, parseJsonObject, sha256, sha256Hex } from "./encoding";
import { fail, json } from "./errors";
import { type BatchRow, type Ctx, type DeviceRow, type GrantRow, getDevice, maxGrant, objectKey } from "./context";

async function deviceKeyOr401(device: DeviceRow | null): Promise<CryptoKey> {
  const key = device ? (await importPublicKey(device.pub))?.key : null;
  if (!key) fail("bad_signature", "unknown device");
  return key;
}

// ---------------------------------------------------------------------------- grant

export async function putGrant(ctx: Ctx, deviceIdPath: string, body: Uint8Array): Promise<Response> {
  if (body.length > 32 * 1024) fail("bad_request", "body too large");
  const envelope = parseJsonObject(body);
  if (!envelope || typeof envelope.doc !== "string" || typeof envelope.sig !== "string") {
    fail("bad_request", "body must be {\"doc\": …, \"sig\": …}");
  }
  const docBytes = b64uDecode(envelope.doc);
  if (!docBytes) fail("bad_request", "doc is not b64u");

  const device = await getDevice(ctx, deviceIdPath);
  const key = await deviceKeyOr401(device);
  if (!(await verify(key, "grant", docBytes, envelope.sig))) fail("bad_signature", "grant signature does not verify");
  const dev = device as DeviceRow;

  const grant = await parseGrant(docBytes);
  if (grant.device_id !== dev.device_id || grant.user_id !== dev.user_id) fail("bad_request", "grant names another device or user");

  const current = await maxGrant(ctx, dev.device_id);
  const storedMax = current?.grant_seq ?? 0;
  const docSha = await sha256Hex(docBytes);
  const stored = grant.grant_seq === storedMax
    ? current
    : await ctx.env.DB.prepare("SELECT * FROM grants WHERE device_id = ?1 AND grant_seq = ?2")
        .bind(dev.device_id, grant.grant_seq).first<GrantRow>();
  if (stored) {
    if (stored.sha256 === docSha) return json({ grant_seq: grant.grant_seq });
    fail("grant_conflict", "a different grant is stored at this grant_seq", { grant_seq: storedMax });
  }
  if (dev.status !== "active") fail("device_inactive", "device is not active", { status: dev.status });
  if (grant.grant_seq !== storedMax + 1) {
    fail("grant_out_of_order", "grant_seq must be the stored maximum + 1", { grant_seq: storedMax });
  }

  const at = instant(ctx.now);
  // Stored only while the device is still active, so a retire or purge that lands during
  // this request wins; processor rows follow only if the grant row went in.
  const stmts = [
    ctx.env.DB.prepare(
      `INSERT INTO grants (device_id, grant_seq, doc, sig, sha256, recipients, received_at)
       SELECT ?1, ?2, ?3, ?4, ?5, ?6, ?7 WHERE EXISTS (SELECT 1 FROM devices WHERE device_id = ?1 AND status = 'active')`,
    ).bind(dev.device_id, grant.grant_seq, envelope.doc, envelope.sig, docSha,
      JSON.stringify(grant.processors.map((p) => p.id)), at),
    ...grant.processors.map((p) =>
      ctx.env.DB.prepare(
        `INSERT INTO grant_processors (device_id, grant_seq, processor_id, sign_pub)
         SELECT ?1, ?2, ?3, ?4 WHERE EXISTS (SELECT 1 FROM grants WHERE device_id = ?1 AND grant_seq = ?2 AND sha256 = ?5)`,
      ).bind(dev.device_id, grant.grant_seq, p.id, p.sign_pub, docSha)),
  ];
  let results: D1Result[];
  try {
    results = await ctx.env.DB.batch(stmts);
  } catch {
    // A concurrent upload stored this grant_seq first.
    const raced = await ctx.env.DB.prepare("SELECT sha256 FROM grants WHERE device_id = ?1 AND grant_seq = ?2")
      .bind(dev.device_id, grant.grant_seq).first<{ sha256: string }>();
    if (raced?.sha256 === docSha) return json({ grant_seq: grant.grant_seq });
    if (raced) {
      const storedMax = (await maxGrant(ctx, dev.device_id))?.grant_seq ?? 0;
      fail("grant_conflict", "a different grant is stored at this grant_seq", { grant_seq: storedMax });
    }
    throw new Error("grant insert failed");
  }
  if (results[0].meta.changes === 0) {
    // Not inserted: either an identical grant was stored concurrently (a repeat, so 200) or the
    // device stopped being active during this request.
    const same = await ctx.env.DB.prepare("SELECT 1 FROM grants WHERE device_id = ?1 AND grant_seq = ?2 AND sha256 = ?3")
      .bind(dev.device_id, grant.grant_seq, docSha).first();
    if (same) return json({ grant_seq: grant.grant_seq });
    const nowDev = await getDevice(ctx, dev.device_id);
    fail("device_inactive", "device is not active", { status: nowDev?.status ?? "purged" });
  }
  return json({ grant_seq: grant.grant_seq });
}

// ---------------------------------------------------------------------------- batch

const SEQ_RE = /^[1-9][0-9]{0,15}$/;

export function parseSeq(text: string): number {
  if (!SEQ_RE.test(text) || !Number.isSafeInteger(Number(text))) fail("bad_request", "malformed seq in path");
  return Number(text);
}

function utcDayStart(ms: number): string {
  const d = new Date(ms);
  return instant(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
}

function secondsToNextUtcDay(ms: number): number {
  const d = new Date(ms);
  return Math.ceil((Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1) - ms) / 1000);
}

export async function putBatch(ctx: Ctx, deviceIdPath: string, seqPath: string): Promise<Response> {
  const seq = parseSeq(seqPath);
  const h = ctx.request.headers;
  const manifestB64 = h.get("HKA-Manifest");
  const sig = h.get("HKA-Signature");
  const manifestBytes = manifestB64 ? b64uDecode(manifestB64) : null;
  if (!manifestBytes || !sig || manifestBytes.length > 4096) fail("bad_request", "missing or malformed HKA-Manifest or HKA-Signature");

  // 1. Device and signature.
  const device = await getDevice(ctx, deviceIdPath);
  const key = await deviceKeyOr401(device);
  if (!(await verify(key, "manifest", manifestBytes, sig))) fail("bad_signature", "manifest signature does not verify");
  const dev = device as DeviceRow;

  // 2. Document and path agree.
  const m = parseManifest(manifestBytes);
  if (m.device_id !== dev.device_id || m.user_id !== dev.user_id || m.seq !== seq) {
    fail("bad_request", "manifest does not match the device or the path");
  }
  const manifestSha = await sha256Hex(manifestBytes);

  // 3. Idempotency.
  const recorded = await ctx.env.DB.prepare("SELECT manifest_sha256, received_at FROM batches WHERE device_id = ?1 AND seq = ?2")
    .bind(dev.device_id, seq).first<{ manifest_sha256: string; received_at: string }>();
  if (recorded) {
    if (recorded.manifest_sha256 === manifestSha) return json({ seq, received_at: recorded.received_at });
    fail("seq_conflict", "a different manifest is stored at this seq");
  }

  // 4. Status.
  if (dev.status !== "active") fail("device_inactive", "device is not active", { status: dev.status });

  // 5. Size.
  const limits = ctx.config.limits;
  const lengthHeader = h.get("Content-Length");
  if (lengthHeader === null || !/^(0|[1-9][0-9]*)$/.test(lengthHeader) || Number(lengthHeader) !== m.size) {
    fail("bad_request", "Content-Length must equal the manifest size");
  }
  if (m.size > limits.max_batch_bytes) fail("batch_too_large", "batch exceeds max_batch_bytes", { max_batch_bytes: limits.max_batch_bytes });

  // 6. Window.
  if (seq > dev.contiguous_seq + limits.seq_window) {
    fail("seq_out_of_window", "seq too far ahead", { contiguous_seq: dev.contiguous_seq });
  }

  // 7. Grant.
  const grant = await ctx.env.DB.prepare("SELECT sha256, recipients FROM grants WHERE device_id = ?1 AND grant_seq = ?2")
    .bind(dev.device_id, m.grant_seq).first<{ sha256: string; recipients: string }>();
  if (!grant) {
    const storedMax = (await maxGrant(ctx, dev.device_id))?.grant_seq ?? 0;
    fail("grant_unknown", "grant not stored yet", { grant_seq: storedMax });
  }
  if (grant.sha256 !== m.grant_sha256 || grant.recipients !== JSON.stringify(m.recipients)) {
    fail("grant_mismatch", "manifest does not match its grant");
  }

  // 8. Quotas.
  const usage = await ctx.env.DB.prepare(
    `SELECT
       (SELECT COALESCE(SUM(size), 0) FROM batches WHERE device_id = ?1 AND received_at >= ?2) AS today,
       (SELECT COALESCE(SUM(b.size), 0) FROM batches b JOIN devices d ON d.device_id = b.device_id
          WHERE d.user_id = ?3 AND d.status != 'purged') AS total`,
  ).bind(dev.device_id, utcDayStart(ctx.now), dev.user_id).first<{ today: number; total: number }>();
  if ((usage?.today ?? 0) + m.size > limits.device_daily_bytes) {
    const retry = secondsToNextUtcDay(ctx.now);
    fail("quota_exceeded", "daily device quota reached", { retry_after: retry }, { "Retry-After": String(retry) });
  }
  if ((usage?.total ?? 0) + m.size > limits.user_total_bytes) {
    fail("quota_total_exceeded", "user total quota reached", { user_total_bytes: limits.user_total_bytes });
  }

  // 9. Store the ciphertext, verified, without overwriting.
  const body = await readBounded(ctx.request, m.size);
  if (!body || body.length !== m.size) fail("bad_request", "body length differs from the manifest size");
  const digest = await sha256(body);
  if (hexEncode(digest) !== m.ciphertext_sha256) fail("hash_mismatch", "body does not match ciphertext_sha256");
  const key9 = objectKey(dev.user_id, dev.device_id, seq);
  const sameAsStored = async (): Promise<boolean> => {
    const head = await ctx.env.BATCHES.head(key9);
    const stored = head?.checksums.sha256 ? new Uint8Array(head.checksums.sha256) : null;
    return !!stored && bytesEqual(stored, digest);
  };
  // Look first as well as putting conditionally, so a platform that ignored the condition
  // still could not overwrite an object recorded for another manifest.
  const existing = await ctx.env.BATCHES.head(key9);
  if (existing && !(await sameAsStored())) throw new Error(`object ${key9} exists with different content`);
  const put = existing ? existing : await ctx.env.BATCHES.put(key9, body, {
    onlyIf: new Headers({ "If-None-Match": "*" }),
    sha256: m.ciphertext_sha256,
    httpMetadata: { contentType: "application/octet-stream" },
    customMetadata: { manifest_sha256: manifestSha },
  });
  if (put === null && !(await sameAsStored())) {
    // Another upload stored an object between our look and our put.
    throw new Error(`object ${key9} exists with different content`);
  }

  // 10. Record (commit point) and derive the device's counters.
  const at = instant(ctx.now);
  await ctx.env.DB.batch([
    // Recorded only while the device is still active: a purge or retire that landed during
    // this upload wins (the object, if new, is removed below).
    ctx.env.DB.prepare(
      `INSERT INTO batches (device_id, seq, manifest, sig, manifest_sha256, ciphertext_sha256, size, object_key, received_at)
       SELECT ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9
       WHERE EXISTS (SELECT 1 FROM devices WHERE device_id = ?1 AND status = 'active')
       ON CONFLICT (device_id, seq) DO NOTHING`,
    ).bind(dev.device_id, seq, b64uEncode(manifestBytes), sig, manifestSha, m.ciphertext_sha256, m.size, key9, at),
    ctx.env.DB.prepare(
      `UPDATE devices SET
         max_seq = (SELECT COALESCE(MAX(seq), 0) FROM batches WHERE device_id = ?1),
         contiguous_seq = (
           WITH RECURSIVE c(n) AS (
             SELECT contiguous_seq FROM devices WHERE device_id = ?1
             UNION ALL
             SELECT n + 1 FROM c WHERE EXISTS (SELECT 1 FROM batches WHERE device_id = ?1 AND seq = n + 1)
           ) SELECT MAX(n) FROM c)
       WHERE device_id = ?1`,
    ).bind(dev.device_id),
  ]);
  const row = await ctx.env.DB.prepare("SELECT manifest_sha256, received_at FROM batches WHERE device_id = ?1 AND seq = ?2")
    .bind(dev.device_id, seq).first<Pick<BatchRow, "manifest_sha256" | "received_at">>();
  if (!row) {
    const now = await getDevice(ctx, dev.device_id);
    if (!existing) {
      try {
        await ctx.env.BATCHES.delete(key9);
      } catch {
        // Retention lock: the purge job removes unrecorded objects under a purged device's prefix later.
      }
    }
    fail("device_inactive", "device is not active", { status: now?.status ?? "purged" });
  }
  if (row.manifest_sha256 !== manifestSha) fail("seq_conflict", "a different manifest is stored at this seq");
  return json({ seq, received_at: row.received_at });
}

/** Read a request body, giving up (null) as soon as it exceeds `max` bytes, whatever Content-Length says. */
export async function readBounded(request: Request, max: number): Promise<Uint8Array | null> {
  const declared = request.headers.get("Content-Length");
  if (declared !== null && Number(declared) > max) return null;
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > max) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  return concatBytes(...chunks);
}
