// Strict encodings from spec/README.md section 1.

const B64U_RE = /^[A-Za-z0-9_-]*$/;
const HEX_RE = /^(?:[0-9a-f]{2})*$/;

export function b64uEncode(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Canonical base64url without padding; returns null when the input is not canonical. */
export function b64uDecode(text: string): Uint8Array | null {
  if (!B64U_RE.test(text) || text.length % 4 === 1) return null;
  const std = text.replace(/-/g, "+").replace(/_/g, "/");
  let bin: string;
  try {
    bin = atob(std + "=".repeat((4 - (std.length % 4)) % 4));
  } catch {
    return null;
  }
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  // Re-encoding must give the input back (rejects non-zero trailing bits).
  return b64uEncode(out) === text ? out : null;
}

export function hexEncode(bytes: Uint8Array): string {
  let out = "";
  for (let i = 0; i < bytes.length; i++) out += bytes[i].toString(16).padStart(2, "0");
  return out;
}

export function isHex(text: unknown, bytes?: number): text is string {
  return typeof text === "string" && HEX_RE.test(text) && (bytes === undefined || text.length === bytes * 2);
}

export async function sha256(data: Uint8Array | ArrayBuffer): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", data));
}

export async function sha256Hex(data: Uint8Array | ArrayBuffer): Promise<string> {
  return hexEncode(await sha256(data));
}

const utf8Strict = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false });
const utf8 = new TextEncoder();

export function encodeUtf8(text: string): Uint8Array {
  return utf8.encode(text);
}

/** Decode strict UTF-8 and parse a JSON object. Returns null on any error. */
export function parseJsonObject(bytes: Uint8Array): Record<string, unknown> | null {
  let text: string;
  try {
    text = utf8Strict.decode(bytes);
  } catch {
    return null;
  }
  try {
    const value: unknown = JSON.parse(text);
    if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
    return value as Record<string, unknown>;
  } catch {
    return null;
  }
}

const INSTANT_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;

export function isInstant(value: unknown): value is string {
  if (typeof value !== "string" || !INSTANT_RE.test(value)) return false;
  const t = Date.parse(value);
  return Number.isFinite(t) && new Date(t).toISOString() === value;
}

export function instant(ms: number): string {
  return new Date(ms).toISOString();
}

export function isSafeInt(value: unknown, min = 0): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= min;
}

/** Length in Unicode scalar values. */
export function scalarLength(text: string): number {
  let n = 0;
  for (const _ of text) n++;
  return n;
}

export function isName(value: unknown): value is string {
  return typeof value === "string" && scalarLength(value) >= 1 && scalarLength(value) <= 64;
}

export function concatBytes(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let off = 0;
  for (const p of parts) {
    out.set(p, off);
    off += p.length;
  }
  return out;
}

export function bytesEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}
