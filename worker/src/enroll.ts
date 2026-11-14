// POST /v1/enroll (spec/README.md section 4.1).
import { importPublicKey, keyId, verify } from "./crypto";
import { parseInvite } from "./documents";
import { instant, isName, isSafeInt, parseJsonObject } from "./encoding";
import { fail, json } from "./errors";
import { type Ctx, nowSeconds } from "./context";

const MAX_INVITE_LIFETIME_MS = 7 * 24 * 3600 * 1000;

export async function enroll(ctx: Ctx, body: Uint8Array): Promise<Response> {
  const limiter = ctx.env.ENROLL_LIMITER;
  if (limiter) {
    const ip = ctx.request.headers.get("CF-Connecting-IP") ?? "unknown";
    if (!(await limiter.limit({ key: ip })).success) {
      fail("rate_limited", "too many enrolment attempts", { retry_after: 60 }, { "Retry-After": "60" });
    }
  }

  // 1. Body size, JSON and field formats.
  if (body.length > 4096) fail("bad_request", "enrolment body larger than 4 KiB");
  const doc = parseJsonObject(body);
  if (!doc) fail("bad_request", "body is not a JSON object");
  const { invite: inviteText, device_pub, device_name, ts } = doc;
  if (!isName(device_name)) fail("bad_request", "device_name");
  if (!isSafeInt(ts)) fail("bad_request", "ts");
  const device = await importPublicKey(device_pub);
  if (!device) fail("bad_request", "device_pub");
  const deviceSig = ctx.request.headers.get("HKA-Signature");
  if (!deviceSig) fail("bad_request", "missing HKA-Signature");

  // 2. Invite parses, admin is configured, invite signature verifies.
  const invite = parseInvite(inviteText);
  const adminKey = ctx.config.adminKeys.get(invite.admin_id);
  if (!adminKey) fail("invite_invalid", "invite from an unknown admin");
  if (!(await verify(adminKey, "invite", invite.bytes, invite.sig))) fail("invite_invalid", "invite signature does not verify");

  // 3. Device signature.
  if (!(await verify(device.key, "enroll", body, deviceSig))) fail("bad_signature", "device signature does not verify");
  const deviceId = await keyId("d", device.raw);

  // 4. Idempotent retry by the same device.
  const used = await ctx.env.DB.prepare("SELECT device_id, response FROM invites_used WHERE nonce = ?1")
    .bind(invite.nonce).first<{ device_id: string; response: string }>();
  if (used && used.device_id === deviceId) return json(JSON.parse(used.response));

  // 5. Server, expiry, freshness.
  if (invite.server !== ctx.config.origin) fail("invite_invalid", "invite is for another server");
  const exp = Date.parse(invite.exp);
  if (exp <= ctx.now) fail("invite_invalid", "invite expired");
  if (exp > ctx.now + MAX_INVITE_LIFETIME_MS) fail("invite_invalid", "invite expiry is more than 7 days ahead");
  if (Math.abs(ts - nowSeconds(ctx)) > 300) fail("stale_timestamp", "ts outside the allowed window");

  // 6. Conflicts.
  if (used) fail("invite_used", "invite already used by another device");
  const existing = await ctx.env.DB.prepare("SELECT 1 FROM devices WHERE device_id = ?1").bind(deviceId).first();
  if (existing) fail("device_exists", "device key already enrolled");
  const user = await ctx.env.DB.prepare("SELECT status FROM users WHERE user_id = ?1").bind(invite.user_id)
    .first<{ status: string }>();
  if (user?.status === "purged") fail("user_purged", "user was purged");

  // 7. Create user (if absent) and device, consume the nonce, atomically.
  const at = instant(ctx.now);
  const response = { user_id: invite.user_id, device_id: deviceId, server_time: nowSeconds(ctx), limits: ctx.config.limits };
  let results: D1Result[];
  try {
    results = await ctx.env.DB.batch([
      ctx.env.DB.prepare("INSERT OR IGNORE INTO users (user_id, name, status, created_at) VALUES (?1, ?2, 'active', ?3)")
        .bind(invite.user_id, invite.user_name, at),
      // Only while the user is active, so a concurrent user purge cannot be undone by an enrolment.
      ctx.env.DB.prepare(
        `INSERT INTO devices (device_id, user_id, pub, name, status, enrolled_at)
         SELECT ?1, ?2, ?3, ?4, 'active', ?5 WHERE EXISTS (SELECT 1 FROM users WHERE user_id = ?2 AND status = 'active')`,
      ).bind(deviceId, invite.user_id, device_pub, device_name, at),
      ctx.env.DB.prepare(
        `INSERT INTO invites_used (nonce, device_id, response, used_at)
         SELECT ?1, ?2, ?3, ?4 WHERE EXISTS (SELECT 1 FROM devices WHERE device_id = ?2)`,
      ).bind(invite.nonce, deviceId, JSON.stringify(response), at),
    ]);
  } catch (err) {
    // A unique constraint lost a race with a concurrent enrolment: report what is now true.
    const again = await ctx.env.DB.prepare("SELECT device_id, response FROM invites_used WHERE nonce = ?1")
      .bind(invite.nonce).first<{ device_id: string; response: string }>();
    if (again?.device_id === deviceId) return json(JSON.parse(again.response));
    if (again) fail("invite_used", "invite already used by another device");
    if (await ctx.env.DB.prepare("SELECT 1 FROM devices WHERE device_id = ?1").bind(deviceId).first()) {
      fail("device_exists", "device key already enrolled");
    }
    throw err; // transient: 500 internal, retryable
  }
  if (results[1].meta.changes === 0) fail("user_purged", "user was purged");
  return json(response);
}
