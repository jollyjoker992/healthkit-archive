// Signed requests (spec/README.md section 5).
import { ID_RE, importPublicKey, verify } from "./crypto";
import { encodeUtf8, sha256Hex } from "./encoding";
import { fail } from "./errors";
import { type Ctx, nowSeconds } from "./context";

export type Caller =
  | { kind: "device"; id: string }
  | { kind: "processor"; id: string }
  | { kind: "admin"; id: string };

const TS_RE = /^(0|[1-9][0-9]{0,15})$/;

export async function authenticate(ctx: Ctx, body: Uint8Array): Promise<Caller> {
  const h = ctx.request.headers;
  const keyIdHeader = h.get("HKA-Key");
  const tsHeader = h.get("HKA-Timestamp");
  const sig = h.get("HKA-Signature");
  if (!keyIdHeader || !tsHeader || !sig || !TS_RE.test(tsHeader)) {
    fail("bad_request", "missing or malformed HKA-Key, HKA-Timestamp or HKA-Signature");
  }

  let caller: Caller;
  let key: CryptoKey | null = null;
  if (ID_RE.d.test(keyIdHeader)) {
    caller = { kind: "device", id: keyIdHeader };
    const row = await ctx.env.DB.prepare("SELECT pub FROM devices WHERE device_id = ?1").bind(keyIdHeader).first<{ pub: string }>();
    key = row ? (await importPublicKey(row.pub))?.key ?? null : null;
  } else if (ID_RE.p.test(keyIdHeader)) {
    caller = { kind: "processor", id: keyIdHeader };
    const row = await ctx.env.DB.prepare("SELECT sign_pub FROM grant_processors WHERE processor_id = ?1 LIMIT 1")
      .bind(keyIdHeader).first<{ sign_pub: string }>();
    key = row ? (await importPublicKey(row.sign_pub))?.key ?? null : null;
  } else if (ID_RE.a.test(keyIdHeader)) {
    caller = { kind: "admin", id: keyIdHeader };
    key = ctx.config.adminKeys.get(keyIdHeader) ?? null;
  } else {
    fail("bad_request", "malformed HKA-Key");
  }
  if (!key) fail("unknown_key", "unknown key");

  const target = ctx.url.pathname + ctx.url.search;
  const payload = encodeUtf8(
    `${ctx.request.method}\n${ctx.config.origin}\n${target}\n${tsHeader}\n${await sha256Hex(body)}`,
  );
  if (!(await verify(key, "request", payload, sig))) fail("bad_signature", "request signature does not verify");
  if (Math.abs(Number(tsHeader) - nowSeconds(ctx)) > 300) fail("stale_timestamp", "timestamp outside the allowed window");
  return caller;
}

export function requireKind<K extends Caller["kind"]>(caller: Caller, kind: K): Extract<Caller, { kind: K }> {
  if (caller.kind !== kind) fail("forbidden", `this endpoint needs a ${kind} key`);
  return caller as Extract<Caller, { kind: K }>;
}
