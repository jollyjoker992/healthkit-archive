"""hkacommon against spec/vectors."""
from __future__ import annotations

import base64
import gzip
import json
import unittest
from pathlib import Path

import pyrage

from hka.build import decrypt_batch
from hka.store import Identity
from hkacommon.documents import parse_card, parse_grant, parse_manifest, parse_receipt, verify_signed
from hkacommon.encoding import FormatError, b64u_decode, is_hex, sha256_hex
from hkacommon.keys import SigningKey, fingerprint, key_id, load_public_key, processor_fingerprint, public_bytes, verify

VECTORS = Path(__file__).resolve().parents[2] / "spec" / "vectors"


def load(name: str) -> dict:
    return json.loads((VECTORS / name).read_text())


class Encodings(unittest.TestCase):
    def test_valid(self):
        for v in load("encodings.json")["valid"]:
            self.assertEqual(b64u_decode(v["b64u"]).hex(), v["hex"])

    def test_invalid(self):
        v = load("encodings.json")
        for c in v["invalid_b64u"]:
            with self.subTest(c["note"]), self.assertRaises(FormatError):
                b64u_decode(c["input"])
        for c in v["invalid_hex"]:
            self.assertFalse(is_hex(c["input"]), c["note"])


class Keys(unittest.TestCase):
    def test_ids_and_fingerprints(self):
        for k in load("keys.json")["keys"]:
            pub = public_bytes(load_public_key(k["pub"]))
            self.assertEqual(key_id(k["id"][0], pub), k["id"])
            fp = processor_fingerprint(pub, k["age_recipient"]) if "age_recipient" in k else fingerprint(pub)
            self.assertEqual(fp, k["fingerprint"])
            self.assertEqual(SigningKey.from_scalar(int(k["private_scalar_hex"], 16)).pub_b64u, k["pub"])

    def test_age_identities(self):
        for a in load("keys.json")["age"]:
            self.assertEqual(str(pyrage.x25519.Identity.from_str(a["identity"]).to_public()), a["recipient"])

    def test_signatures(self):
        v = load("signatures.json")
        key = load_public_key(v["pub"])
        for c in v["cases"]:
            self.assertEqual(verify(key, c["purpose"], b64u_decode(c["payload"]), c["sig"]), c["valid"], c["note"])
        payload = b64u_decode(v["cases"][0]["payload"])
        for c in v["malformed_signatures"]:
            self.assertFalse(verify(key, "manifest", payload, c["sig"]), c["note"])
        for c in v["malformed_public_keys"]:
            with self.subTest(c["note"]), self.assertRaises(FormatError):
                load_public_key(c["pub"])

    def test_signing_roundtrip(self):
        k = SigningKey.generate()
        self.assertTrue(verify(load_public_key(k.pub_b64u), "grant", b"x", k.sign("grant", b"x")))


class Requests(unittest.TestCase):
    def check(self, c) -> bool:
        payload = f"{c['method']}\n{c['origin']}\n{c['target']}\n{c['timestamp']}\n{sha256_hex(b64u_decode(c['body']))}"
        return verify(load_public_key(c["pub"]), "request", payload.encode(), c["sig"])

    def test_cases(self):
        v = load("requests.json")
        for c in v["cases"]:
            self.assertTrue(self.check(c), c["note"])
        for c in v["invalid"]:
            self.assertFalse(self.check(c), c["note"])


class Documents(unittest.TestCase):
    def test_valid_documents(self):
        d = load("documents.json")
        card = parse_card(d["card"]["text"])
        self.assertEqual(card.processor_id, d["card"]["processor_id"])
        self.assertEqual(card.fingerprint, d["card"]["fingerprint"])
        keys = {k["label"]: k for k in load("keys.json")["keys"]}
        dev = keys["device 1"]["pub"]
        g = parse_grant(b64u_decode(d["grant"]["doc"]))
        self.assertTrue(verify_signed(dev, "grant", g.raw, d["grant"]["sig"]))
        self.assertEqual(g.sha256, d["grant"]["grant_sha256"])
        prev = None
        for m in d["manifests"]:
            man = parse_manifest(b64u_decode(m["manifest"]))
            self.assertTrue(verify_signed(dev, "manifest", man.raw, m["sig"]))
            self.assertEqual(man.prev, prev)
            self.assertEqual(man.grant_sha256, g.sha256)
            self.assertEqual(sha256_hex(b64u_decode(m["ciphertext"])), man.ciphertext_sha256)
            prev = man.sha256
        rc = d["receipt"]
        r = parse_receipt(b64u_decode(rc["doc"]))
        self.assertEqual((r.processor_id, r.seq, r.device_id), (rc["processor_id"], rc["seq"], g.device_id))
        self.assertTrue(verify_signed(keys["processor 1"]["pub"], "receipt", r.raw, rc["sig"]))
        self.assertFalse(verify_signed(keys["processor 1"]["pub"], "grant", r.raw, rc["sig"]))

    def test_invalid_documents_rejected_by_parsing_or_checks(self):
        d = load("documents.json")
        keys = {k["label"]: k for k in load("keys.json")["keys"]}
        dev = keys["device 1"]["pub"]
        grant = parse_grant(b64u_decode(d["grant"]["doc"]))
        last = d["manifests"][-1]["manifest_sha256"]
        for c in d["invalid"]:
            with self.subTest(c["note"]):
                if c["kind"] == "grant":
                    raw = b64u_decode(c["doc"])
                    sig_ok = verify_signed(dev, "grant", raw, c["sig"])
                    try:
                        g = parse_grant(raw)
                        bad = (not sig_ok or g.device_id != grant.device_id
                               or c["expect"] in ("grant_out_of_order", "grant_conflict"))
                    except FormatError:
                        bad = True
                    self.assertTrue(bad)
                elif c["kind"] == "receipt":
                    # What a device checks (spec 4.5): parses, signed by that listed processor, its own device,
                    # seq at most what it sealed (3 here).
                    raw = b64u_decode(c["doc"])
                    try:
                        r = parse_receipt(raw)
                    except FormatError:
                        continue
                    listed = {p["id"]: p["sign_pub"] for p in grant.processors}
                    ok = (r.processor_id in listed and verify_signed(listed[r.processor_id], "receipt", raw, c["sig"])
                          and r.device_id == grant.device_id and r.seq <= 3)
                    self.assertFalse(ok)
                elif c["kind"] == "manifest":
                    raw = b64u_decode(c["manifest"])
                    ct = b64u_decode(c["ciphertext"])
                    try:
                        m = parse_manifest(raw)
                    except FormatError:
                        continue
                    ok = (verify_signed(dev, "manifest", raw, c["sig"]) and m.prev == last
                          and m.grant_sha256 == grant.sha256 and list(m.recipients) == grant.processor_ids
                          and m.size == len(ct) and m.ciphertext_sha256 == sha256_hex(ct))
                    self.assertFalse(ok)


class AgeVector(unittest.TestCase):
    def test_decrypt_batch(self):
        v = load("age.json")
        ct = base64.urlsafe_b64decode(v["ciphertext"] + "=" * (-len(v["ciphertext"]) % 4))
        example = (VECTORS.parent / "examples" / "batch.ndjson").read_bytes()
        for ident in v["identities"]:
            identity = Identity("t", SigningKey.generate(), pyrage.x25519.Identity.from_str(ident))
            records = decrypt_batch(ct, identity)
            self.assertEqual([ln.raw for ln in records], example.splitlines())

    def test_decrypt_rejects_missing_final_newline(self):
        ident = pyrage.x25519.Identity.generate()
        ct = pyrage.encrypt(gzip.compress(b'{"k":"header"}'), [ident.to_public()])
        from hka.build import BatchError
        with self.assertRaises(BatchError):
            decrypt_batch(ct, Identity("t", SigningKey.generate(), ident))


if __name__ == "__main__":
    unittest.main()
