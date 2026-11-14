// Admin endpoints (spec 6.1, 6.4).
import { type Caller, requireKind } from "./auth";
import { fail, json } from "./errors";
import { type Ctx, getDevice } from "./context";
import { instant } from "./encoding";

export async function listUsers(ctx: Ctx, caller: Caller): Promise<Response> {
  requireKind(caller, "admin");
  const users = await ctx.env.DB.prepare("SELECT user_id, name, status, created_at FROM users ORDER BY created_at, user_id")
    .all<{ user_id: string; name: string; status: string; created_at: string }>();
  const devices = await ctx.env.DB.prepare(
    `SELECT d.user_id, d.device_id, d.name, d.status, d.enrolled_at, d.contiguous_seq, d.max_seq,
            (SELECT COUNT(*) FROM batches b WHERE b.device_id = d.device_id) AS batches,
            (SELECT COALESCE(SUM(size), 0) FROM batches b WHERE b.device_id = d.device_id) AS bytes,
            (SELECT MAX(grant_seq) FROM grants g WHERE g.device_id = d.device_id) AS grant_seq
     FROM devices d ORDER BY d.enrolled_at, d.device_id`,
  ).all<{
    user_id: string; device_id: string; name: string; status: string; enrolled_at: string;
    contiguous_seq: number; max_seq: number; batches: number; bytes: number; grant_seq: number | null;
  }>();
  return json({
    users: users.results.map((u) => ({
      ...u,
      devices: devices.results.filter((d) => d.user_id === u.user_id).map(({ user_id: _, ...d }) => d),
    })),
  });
}

export async function retireDevice(ctx: Ctx, caller: Caller, deviceId: string): Promise<Response> {
  requireKind(caller, "admin");
  const dev = await getDevice(ctx, deviceId);
  if (!dev) fail("not_found", "no such device");
  if (dev.status === "purged") fail("device_inactive", "device was purged", { status: dev.status });
  if (dev.status === "active") {
    const res = await ctx.env.DB.prepare("UPDATE devices SET status = 'retired', retired_at = ?2 WHERE device_id = ?1 AND status = 'active'")
      .bind(deviceId, instant(ctx.now)).run();
    if (res.meta.changes === 0) {
      const now = await getDevice(ctx, deviceId);
      if (now?.status === "purged") fail("device_inactive", "device was purged", { status: now.status });
    }
  }
  return json({ status: "retired" });
}

export async function purgeUser(ctx: Ctx, caller: Caller, userId: string): Promise<Response> {
  requireKind(caller, "admin");
  const user = await ctx.env.DB.prepare("SELECT status FROM users WHERE user_id = ?1").bind(userId).first<{ status: string }>();
  if (!user) fail("not_found", "no such user");
  const at = instant(ctx.now);
  await ctx.env.DB.batch([
    ctx.env.DB.prepare("UPDATE users SET status = 'purged', purged_at = COALESCE(purged_at, ?2) WHERE user_id = ?1").bind(userId, at),
    ctx.env.DB.prepare("UPDATE devices SET status = 'purged', purged_at = COALESCE(purged_at, ?2) WHERE user_id = ?1").bind(userId, at),
  ]);
  return json({ status: "purged" });
}
