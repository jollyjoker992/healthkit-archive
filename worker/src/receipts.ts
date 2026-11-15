// Processor receipts (spec 4.5): "this processor holds the device's batches 1 to seq".
import { type Ctx, getDevice } from "./context";
import { importPublicKey, verify } from "./crypto";
import { parseReceipt } from "./documents";
import { b64uDecode, instant, parseJsonObject } from "./encoding";
import { fail, json } from "./errors";

export interface ReceiptRow {
  processor_id: string;
  doc: string;
  sig: string;
  received_at: string;
}

export async function putReceipt(ctx: Ctx, deviceIdPath: string, body: Uint8Array): Promise<Response> {
  // 1. Formats.
  const envelope = parseJsonObject(body);
  if (!envelope || typeof envelope.doc !== "string" || typeof envelope.sig !== "string") {
    fail("bad_request", "body must be {\"doc\": …, \"sig\": …}");
  }
  const docBytes = b64uDecode(envelope.doc);
  if (!docBytes) fail("bad_request", "doc is not b64u");
  const receipt = parseReceipt(docBytes);
  if (receipt.device_id !== deviceIdPath) fail("bad_request", "receipt names another device");

  // 2. Device.
  const dev = await getDevice(ctx, deviceIdPath);
  if (!dev) fail("not_found", "no such device");

  // 3. A listed processor, signing with the key the device's grants give for it.
  const listed = await ctx.env.DB.prepare(
    "SELECT sign_pub FROM grant_processors WHERE device_id = ?1 AND processor_id = ?2 LIMIT 1",
  ).bind(dev.device_id, receipt.processor_id).first<{ sign_pub: string }>();
  if (!listed) fail("not_granted", "this processor is not listed by the device");
  const key = await importPublicKey(listed.sign_pub);
  if (!key || !(await verify(key.key, "receipt", docBytes, envelope.sig))) {
    fail("bad_signature", "receipt signature does not verify");
  }

  // 4. Nothing beyond the stored prefix can have been verified.
  if (receipt.seq > dev.contiguous_seq) fail("bad_request", "seq is beyond the stored batches");

  // 5. Keep the highest.
  await ctx.env.DB.prepare(
    `INSERT INTO receipts (device_id, processor_id, seq, doc, sig, received_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)
     ON CONFLICT (device_id, processor_id) DO UPDATE
       SET seq = excluded.seq, doc = excluded.doc, sig = excluded.sig, received_at = excluded.received_at
       WHERE excluded.seq > receipts.seq`,
  ).bind(dev.device_id, receipt.processor_id, receipt.seq, envelope.doc, envelope.sig, instant(ctx.now)).run();
  const stored = await ctx.env.DB.prepare("SELECT seq FROM receipts WHERE device_id = ?1 AND processor_id = ?2")
    .bind(dev.device_id, receipt.processor_id).first<{ seq: number }>();
  return json({ seq: stored?.seq ?? receipt.seq });
}

/** Every stored receipt of a device, for its `state` (spec 6.1). */
export async function deviceReceipts(ctx: Ctx, deviceId: string) {
  const rows = await ctx.env.DB.prepare(
    "SELECT processor_id, doc, sig, received_at FROM receipts WHERE device_id = ?1 ORDER BY processor_id",
  ).bind(deviceId).all<ReceiptRow>();
  return rows.results.map((r) => ({ doc: r.doc, sig: r.sig, received_at: r.received_at }));
}
