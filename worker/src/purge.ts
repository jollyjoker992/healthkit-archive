// Scheduled deletion of purged devices' objects (spec 6.4).
// Index rows are kept (marked object_deleted_at) so idempotent repeats and `state` stay correct.
// Objects under an R2 bucket lock cannot be deleted yet; the job retries on the next run.
// Oldest objects first: they are the ones a retention lock has released.
import type { Env } from "./config";
import { instant } from "./encoding";

// Each R2 call is a subrequest; stay well inside the free plan's 50 per invocation.
// The cron runs hourly, so this handles up to ~960 objects a day.
const PER_RUN = 40;
// An upload that passed its status check before the purge may still store its object for a
// while (slow background uploads); sweep a device's prefix only after this long.
const SWEEP_GRACE_MS = 24 * 3600 * 1000;

export async function runPurgeJob(env: Env, now: number): Promise<{ deleted: number; pending: number }> {
  let budget = PER_RUN;
  let deleted = 0;
  let pending = 0;
  const at = instant(now);

  // 1. Recorded objects of purged devices.
  const rows = await env.DB.prepare(
    `SELECT b.device_id, b.seq, b.object_key FROM batches b JOIN devices d ON d.device_id = b.device_id
     WHERE d.status = 'purged' AND b.object_deleted_at IS NULL
     ORDER BY b.received_at, b.device_id, b.seq LIMIT ?1`,
  ).bind(budget).all<{ device_id: string; seq: number; object_key: string }>();
  const marks = [];
  for (const row of rows.results) {
    budget--;
    try {
      await env.BATCHES.delete(row.object_key); // one by one: a locked object must not block the others
    } catch {
      pending++;
      continue;
    }
    marks.push(env.DB.prepare("UPDATE batches SET object_deleted_at = ?3 WHERE device_id = ?1 AND seq = ?2")
      .bind(row.device_id, row.seq, at));
    deleted++;
  }
  if (marks.length) await env.DB.batch(marks);

  // 2. Devices with every recorded object gone: sweep their prefix for objects that were stored
  //    but never recorded (an upload interrupted before its commit), then mark them done.
  const done = await env.DB.prepare(
    `SELECT device_id, user_id FROM devices
     WHERE status = 'purged' AND objects_deleted_at IS NULL AND purged_at <= ?1
       AND NOT EXISTS (SELECT 1 FROM batches b WHERE b.device_id = devices.device_id AND b.object_deleted_at IS NULL)
     ORDER BY purged_at LIMIT 5`,
  ).bind(instant(now - SWEEP_GRACE_MS)).all<{ device_id: string; user_id: string }>();
  for (const dev of done.results) {
    if (budget <= 1) break;
    budget--;
    const listed = await env.BATCHES.list({ prefix: `v1/${dev.user_id}/${dev.device_id}/`, limit: Math.max(1, budget - 1) });
    let left = listed.truncated;
    for (const obj of listed.objects) {
      budget--;
      try {
        await env.BATCHES.delete(obj.key);
        deleted++;
      } catch {
        pending++;
        left = true;
      }
    }
    if (!left) {
      await env.DB.prepare("UPDATE devices SET objects_deleted_at = ?2 WHERE device_id = ?1").bind(dev.device_id, at).run();
    }
  }
  return { deleted, pending };
}
