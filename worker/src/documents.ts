// Parsing and validation of the signed documents (spec/README.md sections 3 and 4).
import { ID_RE, importPublicKey, keyId } from "./crypto";
import { b64uDecode, isHex, isInstant, isName, isSafeInt, parseJsonObject } from "./encoding";
import { fail } from "./errors";

const AGE_RECIPIENT_RE = /^age1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}$/;

export interface Invite {
  bytes: Uint8Array;
  sig: string;
  server: string;
  admin_id: string;
  user_id: string;
  user_name: string;
  exp: string;
  nonce: string;
}

export interface Processor {
  id: string;
  name: string;
  age_recipient: string;
  sign_pub: string;
}

export interface Grant {
  user_id: string;
  device_id: string;
  grant_seq: number;
  processors: Processor[];
}

export interface Manifest {
  user_id: string;
  device_id: string;
  seq: number;
  prev: string | null;
  ciphertext_sha256: string;
  size: number;
  recipients: string[];
  grant_seq: number;
  grant_sha256: string;
}

function bad(message: string): never {
  fail("bad_request", message);
}

function checkType(doc: Record<string, unknown>, t: string): void {
  if (doc.v !== 1) bad(`${t}: unsupported version`);
  if (doc.t !== t) bad(`expected a ${t} document`);
}

/** Parse `hka-invite:<b64u doc>.<b64u sig>`. Signature is verified by the caller. */
export function parseInvite(text: unknown): Invite {
  if (typeof text !== "string" || text.length > 1024 || !text.startsWith("hka-invite:")) bad("invite: malformed");
  const parts = text.slice("hka-invite:".length).split(".");
  if (parts.length !== 2) bad("invite: malformed");
  const bytes = b64uDecode(parts[0]);
  const sig = b64uDecode(parts[1]);
  if (!bytes || !sig || sig.length !== 64) bad("invite: malformed");
  const doc = parseJsonObject(bytes);
  if (!doc) bad("invite: not a JSON object");
  checkType(doc, "invite");
  const { server, admin_id, user_id, user_name, exp, nonce } = doc;
  if (typeof server !== "string") bad("invite: server");
  if (typeof admin_id !== "string" || !ID_RE.a.test(admin_id)) bad("invite: admin_id");
  if (typeof user_id !== "string" || !ID_RE.u.test(user_id)) bad("invite: user_id");
  if (!isName(user_name)) bad("invite: user_name");
  if (!isInstant(exp)) bad("invite: exp");
  const nonceBytes = typeof nonce === "string" ? b64uDecode(nonce) : null;
  if (!nonceBytes || nonceBytes.length !== 16) bad("invite: nonce");
  return { bytes, sig: parts[1], server, admin_id, user_id, user_name, exp, nonce: nonce as string };
}

export async function parseGrant(bytes: Uint8Array): Promise<Grant> {
  if (bytes.length > 16 * 1024) bad("grant: too large");
  const doc = parseJsonObject(bytes);
  if (!doc) bad("grant: not a JSON object");
  checkType(doc, "grant");
  const { user_id, device_id, grant_seq, processors, created_at } = doc;
  if (typeof user_id !== "string" || !ID_RE.u.test(user_id)) bad("grant: user_id");
  if (typeof device_id !== "string" || !ID_RE.d.test(device_id)) bad("grant: device_id");
  if (!isSafeInt(grant_seq, 1)) bad("grant: grant_seq");
  if (!isInstant(created_at)) bad("grant: created_at");
  if (!Array.isArray(processors) || processors.length < 1 || processors.length > 8) bad("grant: processors");
  const seen = new Set<string>();
  const out: Processor[] = [];
  for (const p of processors) {
    if (!p || typeof p !== "object" || Array.isArray(p)) bad("grant: processor entry");
    const { id, name, age_recipient, sign_pub } = p as Record<string, unknown>;
    if (typeof id !== "string" || !ID_RE.p.test(id) || seen.has(id)) bad("grant: processor id");
    if (!isName(name)) bad("grant: processor name");
    if (typeof age_recipient !== "string" || !AGE_RECIPIENT_RE.test(age_recipient)) bad("grant: age_recipient");
    const key = await importPublicKey(sign_pub);
    if (!key || (await keyId("p", key.raw)) !== id) bad("grant: processor id does not match sign_pub");
    seen.add(id);
    out.push({ id, name, age_recipient, sign_pub: sign_pub as string });
  }
  return { user_id, device_id, grant_seq, processors: out };
}

export function parseManifest(bytes: Uint8Array): Manifest {
  const doc = parseJsonObject(bytes);
  if (!doc) bad("manifest: not a JSON object");
  checkType(doc, "manifest");
  const { user_id, device_id, seq, prev, ciphertext_sha256, size, recipients, grant_seq, grant_sha256, created_at } = doc;
  if (typeof user_id !== "string" || !ID_RE.u.test(user_id)) bad("manifest: user_id");
  if (typeof device_id !== "string" || !ID_RE.d.test(device_id)) bad("manifest: device_id");
  if (!isSafeInt(seq, 1)) bad("manifest: seq");
  if (seq === 1 ? prev !== null : !isHex(prev, 32)) bad("manifest: prev");
  if (!isHex(ciphertext_sha256, 32)) bad("manifest: ciphertext_sha256");
  if (!isSafeInt(size, 1)) bad("manifest: size");
  if (!Array.isArray(recipients) || recipients.length < 1 || recipients.length > 8 ||
      !recipients.every((r) => typeof r === "string" && ID_RE.p.test(r))) bad("manifest: recipients");
  if (!isSafeInt(grant_seq, 1)) bad("manifest: grant_seq");
  if (!isHex(grant_sha256, 32)) bad("manifest: grant_sha256");
  if (!isInstant(created_at)) bad("manifest: created_at");
  return { user_id, device_id, seq, prev: prev as string | null, ciphertext_sha256, size, recipients: recipients as string[], grant_seq, grant_sha256 };
}
