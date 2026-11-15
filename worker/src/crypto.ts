// P-256 keys, signatures and identifiers (spec/README.md section 2).
import { b64uDecode, concatBytes, encodeUtf8, hexEncode, sha256 } from "./encoding";

export type Purpose = "invite" | "enroll" | "grant" | "manifest" | "request" | "receipt";
export type IdPrefix = "d" | "p" | "a";

export const ID_RE = { d: /^d_[0-9a-f]{32}$/, p: /^p_[0-9a-f]{32}$/, a: /^a_[0-9a-f]{32}$/, u: /^u_[0-9a-f]{32}$/ };

/** Decode a b64u SEC1 uncompressed P-256 public key; null if malformed or not on the curve. */
export async function importPublicKey(b64u: unknown): Promise<{ raw: Uint8Array; key: CryptoKey } | null> {
  if (typeof b64u !== "string") return null;
  const raw = b64uDecode(b64u);
  if (!raw || raw.length !== 65 || raw[0] !== 0x04) return null;
  try {
    const key = await crypto.subtle.importKey("raw", raw, { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
    return { raw, key };
  } catch {
    return null;
  }
}

export async function keyId(prefix: IdPrefix, rawPub: Uint8Array): Promise<string> {
  return `${prefix}_${hexEncode((await sha256(rawPub)).slice(0, 16))}`;
}

export function signingInput(purpose: Purpose, payload: Uint8Array): Uint8Array {
  return concatBytes(encodeUtf8(`hka-v1/${purpose}\n`), payload);
}

/** Verify a b64u raw r||s signature. Malformed signatures are simply invalid. */
export async function verify(key: CryptoKey, purpose: Purpose, payload: Uint8Array, sigB64u: unknown): Promise<boolean> {
  if (typeof sigB64u !== "string") return false;
  const sig = b64uDecode(sigB64u);
  if (!sig || sig.length !== 64) return false;
  try {
    return await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, sig, signingInput(purpose, payload));
  } catch {
    return false;
  }
}
