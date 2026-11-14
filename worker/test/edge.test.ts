// Edge cases: races, partial failures, purge job side effects, limits.
import { describe, expect, it } from "vitest";
import { objectKey } from "../src/context";
import { b64uEncode } from "../src/encoding";
import { runPurgeJob } from "../src/purge";
import {
  DEVICE_1, NOW, ORIGIN, USER_ID, body, call, compact, enroll, grantDoc, makeInvite, putBatch, putGrant, setupDevice,
  sign, signedRequest, testEnv,
} from "./helpers";

describe("grant conflicts report the stored maximum", () => {
  it("conflict at grant 1 after grant 2 is stored", async () => {
    await setupDevice();
    await putGrant("device 1", grantDoc("device 1", 2, ["processor 1"]));
    const res = await putGrant("device 1", grantDoc("device 1", 1, ["processor 2"]));
    expect(await body(res)).toMatchObject({ error: "grant_conflict", grant_seq: 2 });
  });

  it("concurrent different grants: one wins, the other conflicts", async () => {
    await enroll("device 1", await makeInvite());
    const [a, b] = await Promise.all([
      putGrant("device 1", grantDoc("device 1", 1, ["processor 1"])),
      putGrant("device 1", grantDoc("device 1", 1, ["processor 2"])),
    ]);
    const codes = [a.status, b.status].sort();
    expect(codes).toEqual([200, 409]);
  });
});

describe("401 responses always carry server_time", () => {
  it("bad_signature and unknown_key on signed requests and uploads", async () => {
    const chain = await setupDevice();
    const t = `/v1/devices/${DEVICE_1.id}/state`;
    for (const res of [
      await signedRequest("device 2", "GET", t),
      await signedRequest("device 1", "GET", t, { origin: "https://x.example.test" }),
      await putBatch("device 1", await chain.seal(), { signer: "device 2" }),
      await putGrant("device 1", grantDoc("device 1", 2, ["processor 1"]), { signer: "device 2" }),
    ]) {
      expect(res.status).toBe(401);
      expect((await body(res)).server_time).toBe(NOW / 1000);
    }
  });
});

describe("enrolment races", () => {
  it("two devices racing for one invite: one enrols, the other gets invite_used", async () => {
    const invite = await makeInvite();
    const [a, b] = await Promise.all([enroll("device 1", invite), enroll("device 2", invite)]);
    const errors = [await body(a), await body(b)].map((r) => r.error ?? "ok").sort();
    expect(errors).toEqual(["invite_used", "ok"]);
  });

  it("the same device retrying concurrently gets the same answer", async () => {
    const invite = await makeInvite();
    const [a, b] = await Promise.all([enroll("device 1", invite), enroll("device 1", invite)]);
    expect(a.status).toBe(200);
    expect(b.status).toBe(200);
  });
});

describe("purge job keeps the index", () => {
  it("idempotent repeats and state stay correct after objects are deleted", async () => {
    const chain = await setupDevice();
    const s1 = await chain.seal();
    const first = await body(await putBatch("device 1", s1));
    await signedRequest("device 1", "POST", `/v1/devices/${DEVICE_1.id}/purge`);
    await runPurgeJob(testEnv, NOW);
    expect(await testEnv.BATCHES.head(objectKey(USER_ID, DEVICE_1.id, 1))).toBeNull();
    expect(await body(await putBatch("device 1", s1))).toEqual(first);
    const state = await body(await signedRequest("device 1", "GET", `/v1/devices/${DEVICE_1.id}/state`));
    expect(state).toMatchObject({ status: "purged", max_seq: 1, last_manifest_sha256: s1.manifestSha });
  });

  it("locked objects stay pending and the device is not marked done", async () => {
    const chain = await setupDevice();
    await putBatch("device 1", await chain.seal());
    await signedRequest("device 1", "POST", `/v1/devices/${DEVICE_1.id}/purge`);
    const locked = { ...testEnv, BATCHES: { ...testEnv.BATCHES, delete: async () => { throw new Error("locked"); } } as unknown as R2Bucket };
    expect(await runPurgeJob(locked, NOW)).toEqual({ deleted: 0, pending: 1 });
    const dev = await testEnv.DB.prepare("SELECT objects_deleted_at FROM devices WHERE device_id = ?1").bind(DEVICE_1.id).first();
    expect(dev?.objects_deleted_at).toBeNull();
    expect(await runPurgeJob(testEnv, NOW)).toEqual({ deleted: 1, pending: 0 });
  });
});

describe("status changes during an upload", () => {
  it("a device purged between the checks and the commit records nothing and leaves no object", async () => {
    const chain = await setupDevice();
    const s = await chain.seal();
    // Purge lands while the upload is between step 4 and step 10: simulate with a DB wrapper
    // that flips the status just before the commit batch.
    const db = testEnv.DB;
    const wrapped = new Proxy(db, {
      get(target, prop) {
        if (prop === "batch") {
          return async (stmts: D1PreparedStatement[]) => {
            await db.prepare("UPDATE devices SET status = 'purged' WHERE device_id = ?1").bind(DEVICE_1.id).run();
            return target.batch(stmts);
          };
        }
        const v = Reflect.get(target, prop);
        return typeof v === "function" ? v.bind(target) : v;
      },
    });
    const res = await putBatch("device 1", s, { env: { DB: wrapped as D1Database } });
    expect(await body(res)).toMatchObject({ error: "device_inactive", status: "purged" });
    expect(await testEnv.BATCHES.head(objectKey(USER_ID, DEVICE_1.id, 1))).toBeNull();
  });

  it("a retired device's batches are still served and listed as retired", async () => {
    const chain = await setupDevice();
    await putBatch("device 1", await chain.seal());
    await signedRequest("admin 1", "POST", `/v1/admin/devices/${DEVICE_1.id}/retire`);
    expect((await signedRequest("processor 1", "GET", `/v1/devices/${DEVICE_1.id}/batches/1`)).status).toBe(200);
    expect((await body(await signedRequest("processor 1", "GET", "/v1/streams"))).streams[0].status).toBe("retired");
  });
});

describe("sizes and paths", () => {
  it("manifest size must equal the body length", async () => {
    const chain = await setupDevice();
    const s = await chain.seal(100);
    const res = await putBatch("device 1", { ...s, ciphertext: s.ciphertext.slice(0, 99) });
    expect((await body(res)).error).toBe("bad_request");
  });

  it("a streamed body without Content-Length is refused", async () => {
    const chain = await setupDevice();
    const s = await chain.seal(100);
    const stream = new ReadableStream({ start(c) { c.enqueue(s.ciphertext); c.close(); } });
    const req = new Request(`${ORIGIN}/v1/devices/${DEVICE_1.id}/batches/1`, {
      method: "PUT", body: stream, headers: {
        "HKA-Manifest": b64uEncode(s.manifest),
        "HKA-Signature": await sign("device 1", "manifest", s.manifest),
      },
    });
    expect((await body(await call(req))).error).toBe("bad_request");
  });

  it("oversized enrolment and grant bodies are refused", async () => {
    const big = compact({ invite: "x".repeat(5000) });
    const res = await call(new Request(`${ORIGIN}/v1/enroll`, { method: "POST", body: big, headers: { "HKA-Signature": "x" } }));
    expect((await body(res)).error).toBe("bad_request");
    await enroll("device 1", await makeInvite());
    const huge = grantDoc("device 1", 1, ["processor 1"], { pad: "x".repeat(17 * 1024) });
    expect((await body(await putGrant("device 1", huge))).error).toBe("bad_request");
  });

  it("seq path values 0 and with leading zeros are bad requests", async () => {
    const chain = await setupDevice();
    const s = await chain.seal();
    await putBatch("device 1", s);
    for (const seq of ["0", "01", "-1", "1.0"]) {
      expect((await body(await putBatch("device 1", s, { path: `/v1/devices/${DEVICE_1.id}/batches/${seq}` }))).error, seq).toBe("bad_request");
      expect((await body(await signedRequest("processor 1", "GET", `/v1/devices/${DEVICE_1.id}/batches/${seq}`))).error, seq).toBe("bad_request");
    }
  });
});

/** A D1 wrapper that runs `before` once, just before the first batch() or run() it intercepts. */
function interceptDb(before: () => Promise<void>, on: "batch" | "run" = "batch", failWith?: Error): D1Database {
  const db = testEnv.DB;
  let fired = false;
  const wrapStmt = (stmt: D1PreparedStatement): D1PreparedStatement => new Proxy(stmt, {
    get(target, prop) {
      if (prop === "bind") return (...args: unknown[]) => wrapStmt(target.bind(...args));
      if (prop === "run" && on === "run" && !fired) {
        return async () => { fired = true; await before(); return target.run(); };
      }
      const v = Reflect.get(target, prop);
      return typeof v === "function" ? v.bind(target) : v;
    },
  });
  return new Proxy(db, {
    get(target, prop) {
      if (prop === "batch" && on === "batch") {
        return async (stmts: D1PreparedStatement[]) => {
          if (!fired) {
            fired = true;
            await before();
            if (failWith) throw failWith;
          }
          return target.batch(stmts);
        };
      }
      if (prop === "prepare") return (sql: string) => wrapStmt(target.prepare(sql));
      const v = Reflect.get(target, prop);
      return typeof v === "function" ? v.bind(target) : v;
    },
  }) as D1Database;
}

const purgeDevice1 = () => testEnv.DB.prepare("UPDATE devices SET status = 'purged', purged_at = '2026-11-12T08:00:00.000Z' WHERE device_id = ?1").bind(DEVICE_1.id).run().then(() => undefined);

describe("round 2: orphans, grant races, untested branches", () => {
  it("purge during an upload under a retention lock: device_inactive now, object removed by the purge job later", async () => {
    const chain = await setupDevice();
    const s = await chain.seal();
    const lockedBucket = new Proxy(testEnv.BATCHES, {
      get(target, prop) {
        if (prop === "delete") return async () => { throw new Error("locked"); };
        const v = Reflect.get(target, prop);
        return typeof v === "function" ? v.bind(target) : v;
      },
    });
    const res = await putBatch("device 1", s, { env: { DB: interceptDb(purgeDevice1), BATCHES: lockedBucket as R2Bucket } });
    expect(await body(res)).toMatchObject({ error: "device_inactive", status: "purged" });
    expect(await testEnv.BATCHES.head(objectKey(USER_ID, DEVICE_1.id, 1))).not.toBeNull(); // orphan, no row
    expect(await runPurgeJob(testEnv, NOW)).toEqual({ deleted: 1, pending: 0 });
    expect(await testEnv.BATCHES.head(objectKey(USER_ID, DEVICE_1.id, 1))).toBeNull();
    const dev = await testEnv.DB.prepare("SELECT objects_deleted_at FROM devices WHERE device_id = ?1").bind(DEVICE_1.id).first();
    expect(dev?.objects_deleted_at).not.toBeNull();
  });

  it("an orphan that cannot be deleted yet keeps the device unmarked", async () => {
    await setupDevice();
    await testEnv.BATCHES.put(objectKey(USER_ID, DEVICE_1.id, 7), new Uint8Array(10));
    await purgeDevice1();
    const locked = { ...testEnv, BATCHES: new Proxy(testEnv.BATCHES, {
      get(target, prop) {
        if (prop === "delete") return async () => { throw new Error("locked"); };
        const v = Reflect.get(target, prop);
        return typeof v === "function" ? v.bind(target) : v;
      },
    }) as R2Bucket };
    expect(await runPurgeJob(locked, NOW)).toEqual({ deleted: 0, pending: 1 });
    const dev = await testEnv.DB.prepare("SELECT objects_deleted_at FROM devices WHERE device_id = ?1").bind(DEVICE_1.id).first();
    expect(dev?.objects_deleted_at).toBeNull();
  });

  it("a grant racing a retire is refused and stores nothing", async () => {
    await setupDevice();
    const retire = () => testEnv.DB.prepare("UPDATE devices SET status = 'retired' WHERE device_id = ?1").bind(DEVICE_1.id).run().then(() => undefined);
    const res = await putGrant("device 1", grantDoc("device 1", 2, ["processor 1"]), { env: { DB: interceptDb(retire) } });
    expect(await body(res)).toMatchObject({ error: "device_inactive", status: "retired" });
    const n = await testEnv.DB.prepare("SELECT COUNT(*) AS n FROM grant_processors WHERE grant_seq = 2").first<{ n: number }>();
    expect(n?.n).toBe(0);
  });

  it("enrolment: a transient D1 failure is 500 internal, and a retry succeeds", async () => {
    const invite = await makeInvite();
    const res = await call(await enrollRequest("device 1", invite), { env: { DB: interceptDb(async () => {}, "batch", new Error("D1 overloaded")) } });
    expect(res.status).toBe(500);
    expect((await body(res)).error).toBe("internal");
    expect((await enroll("device 1", invite)).status).toBe(200);
  });

  it("enrolment racing a user purge: user_purged and no device", async () => {
    await enroll("device 2", await makeInvite());
    const purgeUser = () => testEnv.DB.prepare("UPDATE users SET status = 'purged' WHERE user_id = ?1").bind(USER_ID).run().then(() => undefined);
    const res = await call(await enrollRequest("device 1", await makeInvite()), { env: { DB: interceptDb(purgeUser) } });
    expect((await body(res)).error).toBe("user_purged");
    expect(await testEnv.DB.prepare("SELECT 1 FROM devices WHERE device_id = ?1").bind(DEVICE_1.id).first()).toBeNull();
  });

  it("purge racing a retire answers device_inactive; retire racing a purge answers device_inactive", async () => {
    await setupDevice();
    const retire = () => testEnv.DB.prepare("UPDATE devices SET status = 'retired' WHERE device_id = ?1").bind(DEVICE_1.id).run().then(() => undefined);
    const p = await signedRequest("device 1", "POST", `/v1/devices/${DEVICE_1.id}/purge`, { env: { DB: interceptDb(retire, "run") } });
    expect(await body(p)).toMatchObject({ error: "device_inactive", status: "retired" });
    await testEnv.DB.prepare("UPDATE devices SET status = 'active' WHERE device_id = ?1").bind(DEVICE_1.id).run();
    const r = await signedRequest("admin 1", "POST", `/v1/admin/devices/${DEVICE_1.id}/retire`, { env: { DB: interceptDb(purgeDevice1, "run") } });
    expect(await body(r)).toMatchObject({ error: "device_inactive", status: "purged" });
  });
});

describe("purge job budget", () => {
  it("a truncated sweep leaves the device unmarked until a later run finishes", async () => {
    await setupDevice();
    for (let i = 1; i <= 45; i++) await testEnv.BATCHES.put(objectKey(USER_ID, DEVICE_1.id, i), new Uint8Array(1));
    await purgeDevice1();
    const first = await runPurgeJob(testEnv, NOW);
    expect(first.deleted).toBeLessThan(45);
    const marked = () => testEnv.DB.prepare("SELECT objects_deleted_at AS at FROM devices WHERE device_id = ?1").bind(DEVICE_1.id).first<{ at: string | null }>();
    expect((await marked())?.at).toBeNull();
    const second = await runPurgeJob(testEnv, NOW);
    expect(first.deleted + second.deleted).toBe(45);
    expect((await testEnv.BATCHES.list()).objects).toHaveLength(0);
    if (!(await marked())?.at) await runPurgeJob(testEnv, NOW);
    expect((await marked())?.at).not.toBeNull();
  });

  it("does not sweep a device purged less than a day ago", async () => {
    await setupDevice();
    await testEnv.BATCHES.put(objectKey(USER_ID, DEVICE_1.id, 1), new Uint8Array(1));
    await testEnv.DB.prepare("UPDATE devices SET status = 'purged', purged_at = ?2 WHERE device_id = ?1")
      .bind(DEVICE_1.id, new Date(NOW - 3600 * 1000).toISOString()).run();
    expect(await runPurgeJob(testEnv, NOW)).toEqual({ deleted: 0, pending: 0 });
    expect(await runPurgeJob(testEnv, NOW + 24 * 3600 * 1000)).toEqual({ deleted: 1, pending: 0 });
  });
});

async function enrollRequest(label: string, invite: string): Promise<Request> {
  const k = (await import("./helpers")).KEY[label];
  const bodyBytes = compact({ invite, device_pub: k.pub, device_name: "Test iPhone", ts: NOW / 1000 });
  return new Request(`${ORIGIN}/v1/enroll`, { method: "POST", body: bodyBytes, headers: { "HKA-Signature": await sign(label, "enroll", bodyBytes) } });
}
