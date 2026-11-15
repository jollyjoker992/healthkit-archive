// The Worker against spec/vectors.
import { describe, expect, it } from "vitest";
import { importPublicKey, keyId, verify, type Purpose } from "../src/crypto";
import { parseInvite } from "../src/documents";
import { b64uDecode, encodeUtf8, hexEncode, isHex, sha256Hex } from "../src/encoding";
import encodings from "../../spec/vectors/encodings.json";
import signatures from "../../spec/vectors/signatures.json";
import requests from "../../spec/vectors/requests.json";
import documents from "../../spec/vectors/documents.json";
import keys from "../../spec/vectors/keys.json";
import {
  ADMIN, DEVICE_1, KEY, b, body, call, enroll, putBatch, putGrant, setupDevice, sign, signedRequest,
} from "./helpers";

describe("encodings", () => {
  it("decodes valid b64u", () => {
    for (const v of encodings.valid) expect(hexEncode(b64uDecode(v.b64u)!)).toBe(v.hex);
  });
  it("rejects invalid b64u", () => {
    for (const v of encodings.invalid_b64u) expect(b64uDecode(v.input), v.note).toBeNull();
  });
  it("rejects invalid hex", () => {
    for (const v of encodings.invalid_hex) expect(isHex(v.input), v.note).toBe(false);
  });
});

describe("keys and signatures", () => {
  it("derives ids from public keys", async () => {
    for (const k of keys.keys) {
      const imported = await importPublicKey(k.pub);
      expect(imported).not.toBeNull();
      expect(await keyId(k.id[0] as "d" | "p" | "a", imported!.raw)).toBe(k.id);
    }
  });
  it("verifies signature cases", async () => {
    const key = (await importPublicKey(signatures.pub))!.key;
    for (const c of signatures.cases) {
      expect(await verify(key, c.purpose as Purpose, b(c.payload), c.sig), c.note).toBe(c.valid);
    }
  });
  it("rejects malformed signatures", async () => {
    const key = (await importPublicKey(signatures.pub))!.key;
    const payload = b(signatures.cases[0].payload);
    for (const c of signatures.malformed_signatures) expect(await verify(key, "manifest", payload, c.sig), c.note).toBe(false);
  });
  it("rejects malformed public keys", async () => {
    for (const c of signatures.malformed_public_keys) expect(await importPublicKey(c.pub), c.note).toBeNull();
  });
});

describe("request vectors", () => {
  async function check(c: { pub: string; method: string; origin: string; target: string; timestamp: number; body: string; sig: string }) {
    const payload = encodeUtf8(`${c.method}\n${c.origin}\n${c.target}\n${c.timestamp}\n${await sha256Hex(b(c.body))}`);
    return verify((await importPublicKey(c.pub))!.key, "request", payload, c.sig);
  }
  it("verifies valid cases and the payload text", async () => {
    for (const c of requests.cases) {
      expect(await check(c), c.note).toBe(true);
      expect(`${c.method}\n${c.origin}\n${c.target}\n${c.timestamp}\n${await sha256Hex(b(c.body))}`).toBe(c.payload);
    }
  });
  it("rejects invalid cases", async () => {
    for (const c of requests.invalid) expect(await check(c), c.note).toBe(false);
  });
});

describe("document vectors", () => {
  it("valid invite, enrolment, grant and manifests verify", async () => {
    const invite = parseInvite(documents.invite.text);
    const admin = (await importPublicKey(documents.invite.admin_pub))!.key;
    expect(await verify(admin, "invite", invite.bytes, invite.sig)).toBe(true);
    const dev = (await importPublicKey(DEVICE_1.pub))!.key;
    expect(await verify(dev, "enroll", b(documents.enroll.body), documents.enroll.signature)).toBe(true);
    expect(await verify(dev, "grant", b(documents.grant.doc), documents.grant.sig)).toBe(true);
    expect(await sha256Hex(b(documents.grant.doc))).toBe(documents.grant.grant_sha256);
    for (const m of documents.manifests) {
      expect(await verify(dev, "manifest", b(m.manifest), m.sig)).toBe(true);
      expect(await sha256Hex(b(m.manifest))).toBe(m.manifest_sha256);
    }
  });

  it("the server accepts the vector enrolment, grant and manifests", async () => {
    const enrolled = await call(new Request(`${documents.server}/v1/enroll`, {
      method: "POST", body: b(documents.enroll.body), headers: { "HKA-Signature": documents.enroll.signature },
    }));
    expect(enrolled.status).toBe(200);
    expect((await body(enrolled)).device_id).toBe(documents.enroll.device_id);
    const g = await call(new Request(`${documents.server}/v1/devices/${DEVICE_1.id}/grant`, {
      method: "PUT", body: JSON.stringify({ doc: documents.grant.doc, sig: documents.grant.sig }),
    }));
    expect(g.status).toBe(200);
    for (const m of documents.manifests) {
      const res = await putBatch("device 1", { seq: m.seq, manifest: b(m.manifest), ciphertext: b(m.ciphertext) });
      expect(res.status, await res.clone().text()).toBe(200);
    }
  });

  async function storeValidState() {
    await call(new Request(`${documents.server}/v1/enroll`, {
      method: "POST", body: b(documents.enroll.body), headers: { "HKA-Signature": documents.enroll.signature },
    }));
    await call(new Request(`${documents.server}/v1/devices/${DEVICE_1.id}/grant`, {
      method: "PUT", body: JSON.stringify({ doc: documents.grant.doc, sig: documents.grant.sig }),
    }));
    for (const m of documents.manifests) {
      await putBatch("device 1", { seq: m.seq, manifest: b(m.manifest), ciphertext: b(m.ciphertext) });
    }
  }

  it("accepts the valid receipt and returns it in state", async () => {
    await storeValidState();
    const res = await call(new Request(`${documents.server}/v1/devices/${DEVICE_1.id}/receipt`, {
      method: "PUT", body: JSON.stringify({ doc: documents.receipt.doc, sig: documents.receipt.sig }),
    }));
    expect(await body(res)).toEqual({ seq: documents.receipt.seq });
    const state = await body(await signedRequest("device 1", "GET", `/v1/devices/${DEVICE_1.id}/state`));
    expect(state.receipts).toMatchObject([{ doc: documents.receipt.doc, sig: documents.receipt.sig }]);
  });

  for (const c of documents.invalid) {
    it(`invalid ${c.kind}: ${c.note}`, async () => {
      let res: Response;
      if (c.kind === "invite") {
        res = await enroll("device 2", c.text!);
      } else if (c.kind === "grant" || c.kind === "receipt") {
        await storeValidState();
        res = await call(new Request(documents.server + c.path!, { method: "PUT", body: JSON.stringify({ doc: c.doc, sig: c.sig }) }));
      } else {
        await storeValidState();
        const ct = b(c.ciphertext!);
        res = await call(new Request(documents.server + c.path!, {
          method: "PUT", body: ct,
          headers: { "HKA-Manifest": c.manifest!, "HKA-Signature": c.sig!, "Content-Length": String(ct.length) },
        }));
      }
      if (c.checked_by === "processor") {
        expect(res.status, await res.clone().text()).toBe(200); // the relay does not check prev
      } else {
        expect((await body(res)).error).toBe(c.expect);
      }
    });
  }
});

describe("test helpers sign like the vectors", () => {
  it("helper signatures verify", async () => {
    const payload = encodeUtf8("x");
    for (const label of ["device 1", "processor 1", "admin 1"]) {
      const key = (await importPublicKey(KEY[label].pub))!.key;
      expect(await verify(key, "request", payload, await sign(label, "request", payload))).toBe(true);
    }
    expect(ADMIN.id.startsWith("a_")).toBe(true);
  });
  it("setupDevice works", async () => {
    const chain = await setupDevice();
    const res = await putBatch("device 1", await chain.seal());
    expect(res.status).toBe(200);
    expect(putGrant).toBeTypeOf("function");
  });
});
