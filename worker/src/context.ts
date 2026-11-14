import type { Config, Env } from "./config";

export interface Ctx {
  env: Env;
  config: Config;
  request: Request;
  url: URL;
  /** Current time in milliseconds (injectable for tests). */
  now: number;
}

export function nowSeconds(ctx: Ctx): number {
  return Math.floor(ctx.now / 1000);
}

export interface DeviceRow {
  device_id: string;
  user_id: string;
  pub: string;
  name: string;
  status: "active" | "retired" | "purged";
  enrolled_at: string;
  contiguous_seq: number;
  max_seq: number;
}

export interface GrantRow {
  device_id: string;
  grant_seq: number;
  doc: string;
  sig: string;
  sha256: string;
  recipients: string;
  received_at: string;
}

export interface BatchRow {
  device_id: string;
  seq: number;
  manifest: string;
  sig: string;
  manifest_sha256: string;
  ciphertext_sha256: string;
  size: number;
  object_key: string;
  received_at: string;
}

export async function getDevice(ctx: Ctx, deviceId: string): Promise<DeviceRow | null> {
  return ctx.env.DB.prepare(
    "SELECT device_id, user_id, pub, name, status, enrolled_at, contiguous_seq, max_seq FROM devices WHERE device_id = ?1",
  ).bind(deviceId).first<DeviceRow>();
}

export async function maxGrant(ctx: Ctx, deviceId: string): Promise<GrantRow | null> {
  return ctx.env.DB.prepare("SELECT * FROM grants WHERE device_id = ?1 ORDER BY grant_seq DESC LIMIT 1")
    .bind(deviceId).first<GrantRow>();
}

export function objectKey(userId: string, deviceId: string, seq: number): string {
  return `v1/${userId}/${deviceId}/${String(seq).padStart(10, "0")}.age`;
}
