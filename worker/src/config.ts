// Worker bindings and configuration.
import { ID_RE, importPublicKey, keyId } from "./crypto";

export interface Env {
  DB: D1Database;
  BATCHES: R2Bucket;
  /** Relay origin, exactly as in invites: https://host[:port] */
  ORIGIN: string;
  /** JSON object {"a_<id>": "<b64u P-256 public key>"} */
  ADMIN_KEYS: string;
  MAX_BATCH_BYTES?: string;
  DEVICE_DAILY_BYTES?: string;
  USER_TOTAL_BYTES?: string;
  SEQ_WINDOW?: string;
  ENROLL_LIMITER?: { limit(options: { key: string }): Promise<{ success: boolean }> };
}

export interface Limits {
  max_batch_bytes: number;
  device_daily_bytes: number;
  user_total_bytes: number;
  seq_window: number;
}

export interface Config {
  origin: string;
  adminKeys: Map<string, CryptoKey>;
  limits: Limits;
}

const ORIGIN_RE = /^(https:\/\/[a-z0-9.-]+(:[0-9]{1,5})?|http:\/\/localhost(:[0-9]{1,5})?)$/;

function intVar(value: string | undefined, fallback: number, name: string): number {
  if (value === undefined || value === "") return fallback;
  const n = Number(value);
  if (!Number.isSafeInteger(n) || n <= 0) throw new Error(`config: ${name} must be a positive integer`);
  return n;
}

let cached: { key: string; config: Promise<Config> } | null = null;

/** Parse and validate configuration once per isolate (re-parsed if the variables change). */
export function loadConfig(env: Env): Promise<Config> {
  const key = [env.ORIGIN, env.ADMIN_KEYS, env.MAX_BATCH_BYTES, env.DEVICE_DAILY_BYTES, env.USER_TOTAL_BYTES, env.SEQ_WINDOW].join("\u0000");
  if (cached?.key !== key) cached = { key, config: parseConfig(env) };
  return cached.config;
}

async function parseConfig(env: Env): Promise<Config> {
  if (!ORIGIN_RE.test(env.ORIGIN ?? "")) throw new Error("config: ORIGIN must be https://host[:port] without a path");
  let admins: unknown;
  try {
    admins = JSON.parse(env.ADMIN_KEYS ?? "");
  } catch {
    throw new Error("config: ADMIN_KEYS must be a JSON object");
  }
  if (!admins || typeof admins !== "object" || Array.isArray(admins)) throw new Error("config: ADMIN_KEYS must be a JSON object");
  const adminKeys = new Map<string, CryptoKey>();
  for (const [id, pub] of Object.entries(admins as Record<string, unknown>)) {
    const imported = await importPublicKey(pub);
    if (!ID_RE.a.test(id) || !imported || (await keyId("a", imported.raw)) !== id) {
      throw new Error(`config: ADMIN_KEYS entry ${id} is not a valid admin id and matching public key`);
    }
    adminKeys.set(id, imported.key);
  }
  const limits: Limits = {
    max_batch_bytes: intVar(env.MAX_BATCH_BYTES, 8 * 1024 * 1024, "MAX_BATCH_BYTES"),
    device_daily_bytes: intVar(env.DEVICE_DAILY_BYTES, 512 * 1024 * 1024, "DEVICE_DAILY_BYTES"),
    user_total_bytes: intVar(env.USER_TOTAL_BYTES, 20 * 1024 * 1024 * 1024, "USER_TOTAL_BYTES"),
    seq_window: intVar(env.SEQ_WINDOW, 64, "SEQ_WINDOW"),
  };
  if (limits.max_batch_bytes > 8 * 1024 * 1024) throw new Error("config: MAX_BATCH_BYTES may not exceed 8 MiB");
  if (limits.device_daily_bytes < limits.max_batch_bytes) throw new Error("config: DEVICE_DAILY_BYTES must be at least MAX_BATCH_BYTES");
  return { origin: env.ORIGIN, adminKeys, limits };
}
