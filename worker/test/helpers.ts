import { env } from "cloudflare:test";
import { handle } from "../src/app";
import type { Env } from "../src/config";
import { b64uDecode, b64uEncode, concatBytes, encodeUtf8, hexEncode, sha256 } from "../src/encoding";
import keys from "../../spec/vectors/keys.json";
import documents from "../../spec/vectors/documents.json";

export const ORIGIN = documents.server;
export const NOW = documents.server_now * 1000;
export const testEnv = env as unknown as Env;

export const KEY = Object.fromEntries(keys.keys.map((k) => [k.label, k]));
export const DEVICE_1 = KEY["device 1"];
export const DEVICE_2 = KEY["device 2"];
export const PROC_1 = KEY["processor 1"];
export const PROC_2 = KEY["processor 2"];
export const ADMIN = KEY["admin 1"];
export const USER_ID = JSON.parse(new TextDecoder().decode(b64uDecode(documents.grant.doc)!)).user_id as string;

export function b(text: string): Uint8Array {
  const out = b64uDecode(text);
  if (!out) throw new Error(`not b64u: ${text}`);
  return out;
}

export function utf8(text: string): Uint8Array {
  return encodeUtf8(text);
}

export function compact(obj: unknown): Uint8Array {
  return encodeUtf8(JSON.stringify(obj));
}

const keyCache = new Map<string, CryptoKey>();

async function privateKey(label: string): Promise<CryptoKey> {
  const cached = keyCache.get(label);
  if (cached) return cached;
  const k = KEY[label];
  const pub = b(k.pub);
  const d = Uint8Array.from(k.private_scalar_hex.match(/../g)!.map((h) => parseInt(h, 16)));
  const jwk = { kty: "EC", crv: "P-256", x: b64uEncode(pub.slice(1, 33)), y: b64uEncode(pub.slice(33)), d: b64uEncode(d) };
  const key = await crypto.subtle.importKey("jwk", jwk, { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  keyCache.set(label, key);
  return key;
}

export async function sign(label: string, purpose: string, payload: Uint8Array): Promise<string> {
  const data = concatBytes(utf8(`hka-v1/${purpose}\n`), payload);
  const sig = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, await privateKey(label), data);
  return b64uEncode(new Uint8Array(sig));
}

export async function sha256Hex(data: Uint8Array): Promise<string> {
  return hexEncode(await sha256(data));
}

export async function call(req: Request, opts: { now?: number; env?: Partial<Env> } = {}): Promise<Response> {
  return handle(req, { ...testEnv, ENROLL_LIMITER: undefined, ...opts.env } as Env, opts.now ?? NOW);
}

export async function signedRequest(
  label: string, method: string, target: string,
  opts: { now?: number; ts?: number; origin?: string; env?: Partial<Env> } = {},
): Promise<Response> {
  const ts = opts.ts ?? Math.floor((opts.now ?? NOW) / 1000);
  const payload = utf8(`${method}\n${opts.origin ?? ORIGIN}\n${target}\n${ts}\n${await sha256Hex(new Uint8Array())}`);
  const req = new Request(ORIGIN + target, {
    method,
    headers: { "HKA-Key": KEY[label].id, "HKA-Timestamp": String(ts), "HKA-Signature": await sign(label, "request", payload) },
  });
  return call(req, opts);
}

// ---------------------------------------------------------------------------- documents

export interface InviteFields {
  server?: string; admin_id?: string; user_id?: string; user_name?: string; exp?: string; nonce?: string; t?: string;
}

export async function makeInvite(fields: InviteFields = {}, signer = "admin 1"): Promise<string> {
  const doc = compact({
    v: 1, t: fields.t ?? "invite", server: fields.server ?? ORIGIN, admin_id: fields.admin_id ?? ADMIN.id,
    user_id: fields.user_id ?? USER_ID, user_name: fields.user_name ?? "Mẹ",
    exp: fields.exp ?? "2026-11-20T00:00:00.000Z",
    nonce: fields.nonce ?? b64uEncode(crypto.getRandomValues(new Uint8Array(16))),
  });
  return `hka-invite:${b64uEncode(doc)}.${await sign(signer, "invite", doc)}`;
}

export async function enroll(label: string, invite: string, opts: { ts?: number; name?: string; now?: number } = {}) {
  const body = compact({
    invite, device_pub: KEY[label].pub, device_name: opts.name ?? "Test iPhone",
    ts: opts.ts ?? Math.floor((opts.now ?? NOW) / 1000),
  });
  return call(new Request(`${ORIGIN}/v1/enroll`, {
    method: "POST", body, headers: { "content-type": "application/json", "HKA-Signature": await sign(label, "enroll", body) },
  }), { now: opts.now });
}

export function processorEntry(label: string) {
  const k = KEY[label];
  return { id: k.id, name: label, age_recipient: k.age_recipient, sign_pub: k.pub };
}

export function grantDoc(deviceLabel: string, grantSeq: number, processors: string[], extra: Record<string, unknown> = {}): Uint8Array {
  return compact({
    v: 1, t: "grant", user_id: USER_ID, device_id: KEY[deviceLabel].id, grant_seq: grantSeq,
    processors: processors.map(processorEntry), created_at: "2026-11-13T08:00:00.000Z", ...extra,
  });
}

export async function putGrant(deviceLabel: string, doc: Uint8Array, opts: { signer?: string; path?: string; env?: Partial<Env> } = {}) {
  const body = compact({ doc: b64uEncode(doc), sig: await sign(opts.signer ?? deviceLabel, "grant", doc) });
  return call(new Request(ORIGIN + (opts.path ?? `/v1/devices/${KEY[deviceLabel].id}/grant`), { method: "PUT", body }), { env: opts.env });
}

export interface Sealed {
  seq: number;
  manifest: Uint8Array;
  manifestSha: string;
  ciphertext: Uint8Array;
}

/** Build a device's chain of sealed batches, like the app does. */
export class Chain {
  prev: string | null = null;
  seq = 0;
  constructor(readonly device: string, public grant: { seq: number; doc: Uint8Array; processors: string[] }) {}

  async seal(size = 100, over: Record<string, unknown> = {}): Promise<Sealed> {
    this.seq += 1;
    const ciphertext = crypto.getRandomValues(new Uint8Array(size));
    const manifest = compact({
      v: 1, t: "manifest", user_id: USER_ID, device_id: KEY[this.device].id, seq: this.seq, prev: this.prev,
      ciphertext_sha256: await sha256Hex(ciphertext), size, recipients: this.grant.processors.map((p) => KEY[p].id),
      grant_seq: this.grant.seq, grant_sha256: await sha256Hex(this.grant.doc), created_at: "2026-11-13T08:00:00.000Z",
      ...over,
    });
    const manifestSha = await sha256Hex(manifest);
    this.prev = manifestSha;
    return { seq: this.seq, manifest, manifestSha, ciphertext };
  }
}

export async function putBatch(
  device: string, s: { seq: number; manifest: Uint8Array; ciphertext: Uint8Array },
  opts: { signer?: string; path?: string; contentLength?: number; env?: Partial<Env>; now?: number } = {},
) {
  const req = new Request(ORIGIN + (opts.path ?? `/v1/devices/${KEY[device].id}/batches/${s.seq}`), {
    method: "PUT",
    body: s.ciphertext,
    headers: {
      "HKA-Manifest": b64uEncode(s.manifest),
      "HKA-Signature": await sign(opts.signer ?? device, "manifest", s.manifest),
      "Content-Type": "application/octet-stream",
      "Content-Length": String(opts.contentLength ?? s.ciphertext.length),
    },
  });
  return call(req, { env: opts.env, now: opts.now });
}

/** Enrol a device, upload grant 1 for the given processors, return a chain ready to seal. */
export async function setupDevice(label = "device 1", processors = ["processor 1", "processor 2"]): Promise<Chain> {
  const res = await enroll(label, await makeInvite());
  if (res.status !== 200) throw new Error(`enroll failed: ${await res.text()}`);
  const doc = grantDoc(label, 1, processors);
  const g = await putGrant(label, doc);
  if (g.status !== 200) throw new Error(`grant failed: ${await g.text()}`);
  return new Chain(label, { seq: 1, doc, processors });
}

export async function body(res: Response): Promise<Record<string, any>> {
  return (await res.json()) as Record<string, any>;
}
