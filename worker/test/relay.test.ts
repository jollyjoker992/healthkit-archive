// Behaviour of the relay against the numbered rules in spec/README.md.
import { describe, expect, it } from "vitest";
import { b64uDecode, b64uEncode } from "../src/encoding";
import { runPurgeJob } from "../src/purge";
import { objectKey } from "../src/context";
import {
  DEVICE_1, DEVICE_2, KEY, NOW, ORIGIN, PROC_1, PROC_2, USER_ID, body, call, Chain, compact, enroll, grantDoc,
  makeInvite, putBatch, putGrant, setupDevice, sign, signedRequest, testEnv,
} from "./helpers";

const DAY = 24 * 3600 * 1000;

describe("enrolment (4.1)", () => {
  it("enrols, and a retry with the same body returns the original response even after expiry", async () => {
    const invite = await makeInvite();
    const first = await enroll("device 1", invite);
    expect(first.status).toBe(200);
    const r1 = await body(first);
    expect(r1).toMatchObject({ user_id: USER_ID, device_id: DEVICE_1.id, server_time: NOW / 1000 });
    expect(r1.limits.seq_window).toBe(64);
    const again = await enroll("device 1", invite, { now: NOW + 30 * DAY, ts: NOW / 1000 + 30 * 86400 });
    expect(again.status).toBe(200);
    expect(await body(again)).toEqual(r1);
  });

  it("rejects a used invite, a known device and a purged user", async () => {
    const invite = await makeInvite();
    await enroll("device 1", invite);
    expect((await body(await enroll("device 2", invite))).error).toBe("invite_used");
    expect((await body(await enroll("device 1", await makeInvite()))).error).toBe("device_exists");
    await signedRequest("admin 1", "DELETE", `/v1/admin/users/${USER_ID}`);
    expect((await body(await enroll("device 2", await makeInvite()))).error).toBe("user_purged");
  });

  it("stale ts is 401 stale_timestamp with server_time", async () => {
    const res = await enroll("device 1", await makeInvite(), { ts: NOW / 1000 - 301 });
    expect(res.status).toBe(401);
    expect(await body(res)).toMatchObject({ error: "stale_timestamp", server_time: NOW / 1000 });
  });

  it("rejects a body not signed by the device key", async () => {
    const bodyBytes = compact({ invite: await makeInvite(), device_pub: DEVICE_1.pub, device_name: "x", ts: NOW / 1000 });
    const res = await call(new Request(`${ORIGIN}/v1/enroll`, {
      method: "POST", body: bodyBytes, headers: { "HKA-Signature": await sign("device 2", "enroll", bodyBytes) },
    }));
    expect((await body(res)).error).toBe("bad_signature");
  });

  it("rate limits when the limiter says so", async () => {
    const limiter = { limit: async () => ({ success: false }) };
    const req = new Request(`${ORIGIN}/v1/enroll`, { method: "POST", body: "{}" });
    const res = await call(req, { env: { ENROLL_LIMITER: limiter } });
    expect(res.status).toBe(429);
    expect(res.headers.get("Retry-After")).toBe("60");
  });
});

describe("grants (4.2)", () => {
  it("stores max+1, is idempotent, rejects conflicts and skips", async () => {
    await enroll("device 1", await makeInvite());
    const g1 = grantDoc("device 1", 1, ["processor 1"]);
    expect((await putGrant("device 1", g1)).status).toBe(200);
    expect((await putGrant("device 1", g1)).status).toBe(200);
    const g1b = grantDoc("device 1", 1, ["processor 2"]);
    expect(await body(await putGrant("device 1", g1b))).toMatchObject({ error: "grant_conflict", grant_seq: 1 });
    const g3 = grantDoc("device 1", 3, ["processor 1"]);
    expect(await body(await putGrant("device 1", g3))).toMatchObject({ error: "grant_out_of_order", grant_seq: 1 });
    expect((await putGrant("device 1", grantDoc("device 1", 2, ["processor 2"]))).status).toBe(200);
    expect((await putGrant("device 1", g3)).status).toBe(200);
  });

  it("grant from an unknown device is bad_signature", async () => {
    const res = await putGrant("device 2", grantDoc("device 2", 1, ["processor 1"]));
    expect(res.status).toBe(401);
    expect((await body(res)).error).toBe("bad_signature");
  });

  it("inactive device: new grants refused, stored repeats still 200", async () => {
    const chain = await setupDevice();
    await signedRequest("admin 1", "POST", `/v1/admin/devices/${DEVICE_1.id}/retire`);
    expect((await putGrant("device 1", chain.grant.doc)).status).toBe(200);
    expect(await body(await putGrant("device 1", grantDoc("device 1", 2, ["processor 1"]))))
      .toMatchObject({ error: "device_inactive", status: "retired" });
  });

  it("rejects a processor whose id does not match its key, and 9 processors", async () => {
    await enroll("device 1", await makeInvite());
    const bad = compact({
      v: 1, t: "grant", user_id: USER_ID, device_id: DEVICE_1.id, grant_seq: 1,
      processors: [{ id: PROC_2.id, name: "x", age_recipient: PROC_1.age_recipient, sign_pub: PROC_1.pub }],
      created_at: "2026-11-13T08:00:00.000Z",
    });
    expect((await body(await putGrant("device 1", bad))).error).toBe("bad_request");
    const many = grantDoc("device 1", 1, Array(9).fill("processor 1"));
    expect((await body(await putGrant("device 1", many))).error).toBe("bad_request");
  });
});

describe("batch upload (4.3)", () => {
  it("is idempotent and keeps received_at", async () => {
    const chain = await setupDevice();
    const s = await chain.seal();
    const a = await body(await putBatch("device 1", s));
    const b2 = await body(await putBatch("device 1", s, { now: NOW + 1000 }));
    expect(b2).toEqual(a);
    expect(a.received_at).toBe(new Date(NOW).toISOString());
  });

  it("a different manifest at a stored seq is seq_conflict", async () => {
    const chain = await setupDevice();
    const s = await chain.seal();
    await putBatch("device 1", s);
    const other = new Chain("device 1", chain.grant);
    expect((await body(await putBatch("device 1", await other.seal()))).error).toBe("seq_conflict");
  });

  it("accepts out-of-order arrival and tracks contiguous and max seq", async () => {
    const chain = await setupDevice();
    const s1 = await chain.seal();
    const s2 = await chain.seal();
    const s3 = await chain.seal();
    expect((await putBatch("device 1", s3)).status).toBe(200);
    let state = await body(await signedRequest("device 1", "GET", `/v1/devices/${DEVICE_1.id}/state`));
    expect(state).toMatchObject({ contiguous_seq: 0, max_seq: 3, last_manifest_sha256: s3.manifestSha });
    await putBatch("device 1", s1);
    await putBatch("device 1", s2);
    state = await body(await signedRequest("device 1", "GET", `/v1/devices/${DEVICE_1.id}/state`));
    expect(state).toMatchObject({ contiguous_seq: 3, max_seq: 3 });
  });

  it("refuses seq beyond the window", async () => {
    const chain = await setupDevice();
    chain.seq = 64;
    const s = await chain.seal(100, { prev: "0".repeat(64) });
    expect(s.seq).toBe(65);
    expect(await body(await putBatch("device 1", s))).toMatchObject({ error: "seq_out_of_window", contiguous_seq: 0 });
    chain.seq = 63;
    expect((await putBatch("device 1", await chain.seal(100, { prev: "0".repeat(64) }))).status).toBe(200);
  });

  it("grant_unknown is retryable and clears after the grant upload", async () => {
    const chain = await setupDevice();
    const g2 = grantDoc("device 1", 2, ["processor 1"]);
    chain.grant = { seq: 2, doc: g2, processors: ["processor 1"] };
    const s = await chain.seal();
    expect(await body(await putBatch("device 1", s))).toMatchObject({ error: "grant_unknown", grant_seq: 1 });
    await putGrant("device 1", g2);
    expect((await putBatch("device 1", s)).status).toBe(200);
  });

  it("grant_mismatch for wrong recipients", async () => {
    const chain = await setupDevice();
    const s = await chain.seal(100, { recipients: [PROC_1.id] });
    expect((await body(await putBatch("device 1", s))).error).toBe("grant_mismatch");
  });

  it("checks size, limits and quotas", async () => {
    const chain = await setupDevice();
    const small = { MAX_BATCH_BYTES: "1000", DEVICE_DAILY_BYTES: "1500", USER_TOTAL_BYTES: "2500" };
    expect(await body(await putBatch("device 1", await chain.seal(1001), { env: small })))
      .toMatchObject({ error: "batch_too_large", max_batch_bytes: 1000 });
    chain.seq = 0; chain.prev = null;
    expect((await putBatch("device 1", await chain.seal(1000), { env: small })).status).toBe(200);
    const over = await putBatch("device 1", await chain.seal(600), { env: small });
    expect(over.status).toBe(413);
    expect(await body(over)).toMatchObject({ error: "quota_exceeded", retry_after: 16 * 3600 });
    expect(over.headers.get("Retry-After")).toBe(String(16 * 3600));
    // Next UTC day the daily quota resets; the user total does not.
    chain.seq = 1;
    expect((await putBatch("device 1", await chain.seal(1000, { prev: "0".repeat(64) }), { env: small, now: NOW + DAY })).status).toBe(200);
    expect((await body(await putBatch("device 1", await chain.seal(600), { env: small, now: NOW + 2 * DAY }))).error)
      .toBe("quota_total_exceeded");
  });

  it("hash_mismatch stores nothing", async () => {
    const chain = await setupDevice();
    const s = await chain.seal();
    const res = await putBatch("device 1", { ...s, ciphertext: s.ciphertext.map((x) => x ^ 1) });
    expect((await body(res)).error).toBe("hash_mismatch");
    expect(await testEnv.BATCHES.head(objectKey(USER_ID, DEVICE_1.id, 1))).toBeNull();
    expect((await putBatch("device 1", s)).status).toBe(200);
  });

  it("recovers when the object was stored but not recorded; refuses different content", async () => {
    const chain = await setupDevice();
    const s = await chain.seal();
    await testEnv.BATCHES.put(objectKey(USER_ID, DEVICE_1.id, 1), s.ciphertext, { sha256: (await crypto.subtle.digest("SHA-256", s.ciphertext)) });
    expect((await putBatch("device 1", s)).status).toBe(200);
    const s2 = await chain.seal();
    await testEnv.BATCHES.put(objectKey(USER_ID, DEVICE_1.id, 2), new Uint8Array(100), { sha256: await crypto.subtle.digest("SHA-256", new Uint8Array(100)) });
    expect((await putBatch("device 1", s2)).status).toBe(500);
  });

  it("unknown device, foreign signer and path mismatch", async () => {
    const chain = await setupDevice();
    const s = await chain.seal();
    expect((await body(await putBatch("device 1", s, { signer: "device 2" }))).error).toBe("bad_signature");
    expect((await body(await putBatch("device 1", s, { path: `/v1/devices/${DEVICE_1.id}/batches/2` }))).error).toBe("bad_request");
    expect((await body(await putBatch("device 1", s, { path: `/v1/devices/${DEVICE_2.id}/batches/1` }))).error).toBe("bad_signature");
  });

  it("retired device: new batches refused, stored repeats 200", async () => {
    const chain = await setupDevice();
    const s1 = await chain.seal();
    await putBatch("device 1", s1);
    await signedRequest("admin 1", "POST", `/v1/admin/devices/${DEVICE_1.id}/retire`);
    expect((await putBatch("device 1", s1)).status).toBe(200);
    expect(await body(await putBatch("device 1", await chain.seal()))).toMatchObject({ error: "device_inactive", status: "retired" });
  });
});

describe("signed requests (5)", () => {
  it("rejects stale, unknown, wrong origin and bad signatures", async () => {
    await setupDevice();
    const target = `/v1/devices/${DEVICE_1.id}/state`;
    const stale = await signedRequest("device 1", "GET", target, { ts: NOW / 1000 - 301 });
    expect(await body(stale)).toMatchObject({ error: "stale_timestamp", server_time: NOW / 1000 });
    expect((await body(await signedRequest("device 2", "GET", target))).error).toBe("unknown_key");
    expect((await body(await signedRequest("device 1", "GET", target, { origin: "https://other.example.test" }))).error)
      .toBe("bad_signature");
    const res = await call(new Request(ORIGIN + target, { headers: { "HKA-Key": DEVICE_1.id } }));
    expect((await body(res)).error).toBe("bad_request");
  });

  it("scopes keys to their endpoints", async () => {
    await setupDevice();
    await enroll("device 2", await makeInvite());
    expect((await body(await signedRequest("device 2", "GET", `/v1/devices/${DEVICE_1.id}/state`))).error).toBe("forbidden");
    expect((await body(await signedRequest("processor 1", "GET", "/v1/admin/users"))).error).toBe("forbidden");
    expect((await body(await signedRequest("device 1", "GET", "/v1/streams"))).error).toBe("forbidden");
    expect((await body(await signedRequest("admin 1", "GET", `/v1/devices/${DEVICE_1.id}/state`))).error).toBe("forbidden");
  });
});

describe("state and processor reads (6.1)", () => {
  it("state has null grant fields before a grant", async () => {
    await enroll("device 1", await makeInvite());
    const state = await body(await signedRequest("device 1", "GET", `/v1/devices/${DEVICE_1.id}/state`));
    expect(state).toMatchObject({ status: "active", contiguous_seq: 0, max_seq: 0, last_manifest_sha256: null, grant_seq: null, grant_sha256: null,
      batches: 0, bytes: 0, last_received_at: null, user_bytes: 0 });
  });

  it("keeps the highest receipt per processor and returns them in state", async () => {
    const chain = await setupDevice();
    for (let i = 0; i < 3; i++) await putBatch("device 1", await chain.seal());
    const put = async (label: string, seq: number) => {
      const doc = compact({ v: 1, t: "receipt", device_id: DEVICE_1.id, processor_id: KEY[label].id, seq,
        created_at: new Date(NOW).toISOString() });
      return call(new Request(`${ORIGIN}/v1/devices/${DEVICE_1.id}/receipt`, {
        method: "PUT", body: JSON.stringify({ doc: b64uEncode(doc), sig: await sign(label, "receipt", doc) }),
      }));
    };
    expect(await body(await put("processor 1", 2))).toEqual({ seq: 2 });
    expect(await body(await put("processor 1", 3))).toEqual({ seq: 3 });
    expect(await body(await put("processor 1", 1))).toEqual({ seq: 3 }); // a lower one changes nothing
    expect(await body(await put("processor 2", 1))).toEqual({ seq: 1 });
    const state = await body(await signedRequest("device 1", "GET", `/v1/devices/${DEVICE_1.id}/state`));
    const seqs = state.receipts.map((r: { doc: string }) => JSON.parse(new TextDecoder().decode(b64uDecode(r.doc)!)).seq);
    expect(seqs.sort()).toEqual([1, 3]);
    // A processor removed by a later grant is still listed, so it can still say what it holds.
    await putGrant("device 1", grantDoc("device 1", 2, ["processor 1"]));
    expect(await body(await put("processor 2", 2))).toEqual({ seq: 2 });
  });

  it("state counts the device's stored batches and the user's total", async () => {
    const one = await setupDevice("device 1");
    const two = await setupDevice("device 2");
    await putBatch("device 1", await one.seal(100), { now: NOW });
    await putBatch("device 1", await one.seal(50), { now: NOW + 60_000 });
    await putBatch("device 2", await two.seal(30));
    const state = await body(await signedRequest("device 1", "GET", `/v1/devices/${DEVICE_1.id}/state`));
    expect(state).toMatchObject({ batches: 2, bytes: 150, last_received_at: new Date(NOW + 60_000).toISOString(), user_bytes: 180 });
    // A purged device's bytes no longer count for the user.
    await signedRequest("device 2", "POST", `/v1/devices/${DEVICE_2.id}/purge`);
    expect((await body(await signedRequest("device 1", "GET", `/v1/devices/${DEVICE_1.id}/state`))).user_bytes).toBe(150);
  });

  it("streams, grants, batch list and download", async () => {
    const chain = await setupDevice();
    const sealed = [await chain.seal(10), await chain.seal(20), await chain.seal(30)];
    await putBatch("device 1", sealed[0]);
    await putBatch("device 1", sealed[2]);
    const streams = await body(await signedRequest("processor 1", "GET", "/v1/streams"));
    expect(streams.streams).toEqual([{
      user_id: USER_ID, user_name: "Mẹ", device_id: DEVICE_1.id, device_name: "Test iPhone", device_pub: DEVICE_1.pub,
      status: "active", granted: true, contiguous_seq: 1, max_seq: 3, grant_seq: 1,
    }]);
    const grants = await body(await signedRequest("processor 1", "GET", `/v1/devices/${DEVICE_1.id}/grants`));
    expect(grants.grants[0]).toMatchObject({ grant_seq: 1, doc: b64uEncode(chain.grant.doc) });
    const list = await body(await signedRequest("processor 1", "GET", `/v1/devices/${DEVICE_1.id}/batches?after=0&limit=10`));
    expect(list.batches.map((x: { seq: number }) => x.seq)).toEqual([1, 3]);
    expect(list).toMatchObject({ contiguous_seq: 1, max_seq: 3 });
    expect(list.batches[0]).toMatchObject({ manifest: b64uEncode(sealed[0].manifest), size: 10 });
    const after1 = await body(await signedRequest("processor 1", "GET", `/v1/devices/${DEVICE_1.id}/batches?after=1`));
    expect(after1.batches.map((x: { seq: number }) => x.seq)).toEqual([3]);
    const dl = await signedRequest("processor 1", "GET", `/v1/devices/${DEVICE_1.id}/batches/3`);
    expect(dl.status).toBe(200);
    expect(new Uint8Array(await dl.arrayBuffer())).toEqual(sealed[2].ciphertext);
    expect((await body(await signedRequest("processor 1", "GET", `/v1/devices/${DEVICE_1.id}/batches/2`))).error).toBe("not_found");
  });

  it("validates the batch list query", async () => {
    await setupDevice();
    for (const q of ["?limit=0", "?limit=1001", "?after=-1", "?after=01", "?foo=1", "?after=1&after=2", "?limit=x"]) {
      expect((await body(await signedRequest("processor 1", "GET", `/v1/devices/${DEVICE_1.id}/batches${q}`))).error, q).toBe("bad_request");
    }
  });

  it("a removed processor keeps reading grants but not batches; others see not_granted", async () => {
    await setupDevice("device 1", ["processor 1", "processor 2"]);
    await putGrant("device 1", grantDoc("device 1", 2, ["processor 1"]));
    const streams = await body(await signedRequest("processor 2", "GET", "/v1/streams"));
    expect(streams.streams[0]).toMatchObject({ granted: false, grant_seq: 2 });
    expect((await signedRequest("processor 2", "GET", `/v1/devices/${DEVICE_1.id}/grants`)).status).toBe(200);
    expect((await body(await signedRequest("processor 2", "GET", `/v1/devices/${DEVICE_1.id}/batches`))).error).toBe("not_granted");
    await enroll("device 2", await makeInvite());
    await putGrant("device 2", grantDoc("device 2", 1, ["processor 2"]));
    expect((await body(await signedRequest("processor 1", "GET", `/v1/devices/${DEVICE_2.id}/grants`))).error).toBe("not_granted");
    expect((await body(await signedRequest("processor 1", "GET", `/v1/devices/d_${"0".repeat(32)}/grants`))).error).toBe("not_found");
  });
});

describe("status, retire and purge (6.4)", () => {
  it("device purge: idempotent, stops serving batches, keeps grants, streams show it", async () => {
    const chain = await setupDevice();
    await putBatch("device 1", await chain.seal());
    expect(await body(await signedRequest("device 1", "POST", `/v1/devices/${DEVICE_1.id}/purge`))).toEqual({ status: "purged" });
    expect((await signedRequest("device 1", "POST", `/v1/devices/${DEVICE_1.id}/purge`)).status).toBe(200);
    expect((await body(await signedRequest("processor 1", "GET", `/v1/devices/${DEVICE_1.id}/batches`))).error).toBe("device_purged");
    expect((await signedRequest("processor 1", "GET", `/v1/devices/${DEVICE_1.id}/grants`)).status).toBe(200);
    expect((await body(await signedRequest("processor 1", "GET", "/v1/streams"))).streams[0].status).toBe("purged");
    expect((await body(await signedRequest("device 1", "GET", `/v1/devices/${DEVICE_1.id}/state`))).status).toBe("purged");
  });

  it("a retired device cannot purge; retire is idempotent; retiring a purged device is device_inactive", async () => {
    await setupDevice();
    expect(await body(await signedRequest("admin 1", "POST", `/v1/admin/devices/${DEVICE_1.id}/retire`))).toEqual({ status: "retired" });
    expect((await signedRequest("admin 1", "POST", `/v1/admin/devices/${DEVICE_1.id}/retire`)).status).toBe(200);
    expect((await body(await signedRequest("device 1", "POST", `/v1/devices/${DEVICE_1.id}/purge`))).error).toBe("device_inactive");
    await signedRequest("admin 1", "DELETE", `/v1/admin/users/${USER_ID}`);
    expect((await body(await signedRequest("admin 1", "POST", `/v1/admin/devices/${DEVICE_1.id}/retire`))).error).toBe("device_inactive");
    expect((await body(await signedRequest("admin 1", "POST", `/v1/admin/devices/d_${"0".repeat(32)}/retire`))).error).toBe("not_found");
  });

  it("admin user listing and purge", async () => {
    const chain = await setupDevice();
    await putBatch("device 1", await chain.seal(50));
    const users = await body(await signedRequest("admin 1", "GET", "/v1/admin/users"));
    expect(users.users[0]).toMatchObject({ user_id: USER_ID, name: "Mẹ", status: "active" });
    expect(users.users[0].devices[0]).toMatchObject({ device_id: DEVICE_1.id, batches: 1, bytes: 50, grant_seq: 1, max_seq: 1 });
    expect((await signedRequest("admin 1", "DELETE", `/v1/admin/users/${USER_ID}`)).status).toBe(200);
    expect((await signedRequest("admin 1", "DELETE", `/v1/admin/users/${USER_ID}`)).status).toBe(200);
    const after = await body(await signedRequest("admin 1", "GET", "/v1/admin/users"));
    expect(after.users[0].status).toBe("purged");
    expect(after.users[0].devices[0].status).toBe("purged");
  });

  it("the purge job deletes objects and records, then marks the device", async () => {
    const chain = await setupDevice();
    await putBatch("device 1", await chain.seal());
    await putBatch("device 1", await chain.seal());
    await signedRequest("device 1", "POST", `/v1/devices/${DEVICE_1.id}/purge`);
    expect(await runPurgeJob(testEnv, NOW)).toEqual({ deleted: 2, pending: 0 });
    expect((await testEnv.BATCHES.list()).objects).toHaveLength(0);
    const marked = async () => (await testEnv.DB.prepare("SELECT objects_deleted_at AS at FROM devices WHERE device_id = ?1")
      .bind(DEVICE_1.id).first<{ at: string | null }>())?.at;
    expect(await marked()).toBeNull(); // the prefix sweep waits a day for in-flight uploads
    expect(await runPurgeJob(testEnv, NOW + DAY)).toEqual({ deleted: 0, pending: 0 });
    expect(await marked()).toBe(new Date(NOW + DAY).toISOString());
  });
});

describe("routing", () => {
  it("unknown paths are not_found", async () => {
    expect((await body(await call(new Request(`${ORIGIN}/v1/nope`)))).error).toBe("not_found");
    expect((await body(await call(new Request(`${ORIGIN}/v1/enroll`)))).error).toBe("not_found");
  });
  it("uses fresh keys in tests", () => {
    expect(KEY["device 1"].id).toBe(DEVICE_1.id);
  });
});
