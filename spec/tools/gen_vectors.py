#!/usr/bin/env python3
"""Generate the protocol test vectors in spec/vectors/.

This is an independent reference written directly against the spec with the
`cryptography` package; implementations are tested against its output, not
against each other. Signatures are deterministic (RFC 6979), so re-running the
script reproduces the same files. The age vector needs the reference `age`
CLI and is only regenerated with --age (its ciphertext is random).

Usage: python3 spec/tools/gen_vectors.py [--age] [--check]
  --check  regenerate in memory and fail if the committed files differ.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

VECTORS = Path(__file__).resolve().parent.parent / "vectors"
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def test_key(label: str) -> ec.EllipticCurvePrivateKey:
    scalar = int.from_bytes(sha256(b"hka test key " + label.encode()), "big") % (N - 1) + 1
    return ec.derive_private_key(scalar, ec.SECP256R1())


def pub_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def key_id(prefix: str, pub: bytes) -> str:
    return f"{prefix}_{sha256(pub)[:16].hex()}"


def fingerprint(pub: bytes) -> str:
    h = sha256(pub)[:10].hex().upper()
    return "-".join(h[i:i + 4] for i in range(0, 20, 4))


def sign(key: ec.EllipticCurvePrivateKey, purpose: str, payload: bytes) -> bytes:
    der = key.sign(b"hka-v1/" + purpose.encode() + b"\n" + payload,
                   ec.ECDSA(hashes.SHA256(), deterministic_signing=True))
    r, s = decode_dss_signature(der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def high_s(sig: bytes) -> bytes:
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    return r.to_bytes(32, "big") + (N - s).to_bytes(32, "big")


def compact(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()


def scalar_hex(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_numbers().private_value.to_bytes(32, "big").hex()


BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def bech32_encode(hrp: str, data: bytes) -> str:
    acc, bits, words = 0, 0, []
    for b in data:
        acc = (acc << 8) | b
        bits += 8
        while bits >= 5:
            bits -= 5
            words.append((acc >> bits) & 31)
    if bits:
        words.append((acc << (5 - bits)) & 31)
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    poly = _bech32_polymod(expanded + words + [0] * 6) ^ 1
    checksum = [(poly >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(BECH32[w] for w in words + checksum)


def age_test_identity(label: str) -> tuple[str, str]:
    """Deterministic age X25519 identity and recipient for a label (test only)."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import PublicFormat as PF, Encoding as E
    secret = sha256(b"hka test age " + label.encode())
    public = X25519PrivateKey.from_private_bytes(secret).public_key().public_bytes(E.Raw, PF.Raw)
    return bech32_encode("age-secret-key-", secret).upper(), bech32_encode("age", public)


# Fixed test material. These keys are public; never use them for real data.
DEVICE = test_key("device 1")
DEVICE2 = test_key("device 2")
PROCESSOR = test_key("processor 1")
PROCESSOR2 = test_key("processor 2")
ADMIN = test_key("admin 1")
AGE_IDENTITY_1, AGE_RECIPIENT_1 = age_test_identity("processor 1")
AGE_IDENTITY_2, AGE_RECIPIENT_2 = age_test_identity("processor 2")
USER_ID = "u_" + sha256(b"hka test user 1")[:16].hex()
SERVER = "https://hka.example.test"


NOW = 1794556800  # 2026-11-13T08:00:00Z, the reference "now" for time-dependent checks


def processor_fingerprint(sign_pub: bytes, age_recipient: str) -> str:
    return fingerprint(sign_pub + age_recipient.encode("ascii"))


def keys_vectors() -> dict:
    out = []
    for label, key, prefix, age_rec in [("device 1", DEVICE, "d", None), ("device 2", DEVICE2, "d", None),
                                        ("processor 1", PROCESSOR, "p", AGE_RECIPIENT_1),
                                        ("processor 2", PROCESSOR2, "p", AGE_RECIPIENT_2),
                                        ("admin 1", ADMIN, "a", None)]:
        pub = pub_bytes(key)
        entry = {"label": label, "private_scalar_hex": scalar_hex(key), "pub": b64u(pub),
                 "id": key_id(prefix, pub)}
        if age_rec:
            entry["age_recipient"] = age_rec
            entry["fingerprint"] = processor_fingerprint(pub, age_rec)
        else:
            entry["fingerprint"] = fingerprint(pub)
        out.append(entry)
    return {"description": "P-256 test keys, their public keys (SEC1 uncompressed, b64u), ids and fingerprints "
                           "(processor fingerprints cover sign_pub || age_recipient); age X25519 test identities",
            "keys": out,
            "age": [{"label": "processor 1", "identity": AGE_IDENTITY_1, "recipient": AGE_RECIPIENT_1},
                    {"label": "processor 2", "identity": AGE_IDENTITY_2, "recipient": AGE_RECIPIENT_2}]}


def encoding_vectors() -> dict:
    valid = [(b"", ""), (b"A", "QQ"), (b"AB", "QUI"), (b"ABC", "QUJD"), (bytes([0xFB, 0xFF]), "-_8")]
    return {
        "description": "b64u decoders must accept 'valid' and reject 'invalid_b64u'; hex decoders must reject 'invalid_hex'",
        "valid": [{"b64u": e, "hex": d.hex()} for d, e in valid],
        "invalid_b64u": [
            {"note": "padding", "input": "QQ=="},
            {"note": "length 1 mod 4", "input": "QUJDR"},
            {"note": "non-zero trailing bits", "input": "QR"},
            {"note": "non-zero trailing bits (3-char group)", "input": "QUJ"},
            {"note": "standard alphabet '+'", "input": "+_8"},
            {"note": "standard alphabet '/'", "input": "-/8"},
            {"note": "whitespace", "input": "QU JD"},
            {"note": "newline", "input": "QUJD\n"},
        ],
        "invalid_hex": [
            {"note": "uppercase", "input": "ABCD"},
            {"note": "odd length", "input": "abc"},
            {"note": "non-hex character", "input": "abcg"},
        ],
    }


def signature_vectors() -> dict:
    pub = b64u(pub_bytes(DEVICE))
    payload = b'{"v":1,"t":"example"}'
    good = sign(DEVICE, "manifest", payload)
    cases = [
        {"note": "valid", "purpose": "manifest", "payload": b64u(payload), "sig": b64u(good), "valid": True},
        {"note": "valid high-S form of the same signature", "purpose": "manifest", "payload": b64u(payload),
         "sig": b64u(high_s(good)), "valid": True},
        {"note": "same signature, different purpose", "purpose": "grant", "payload": b64u(payload),
         "sig": b64u(good), "valid": False},
        {"note": "payload changed by one byte", "purpose": "manifest", "payload": b64u(payload[:-1] + b"]"),
         "sig": b64u(good), "valid": False},
        {"note": "signature bit flipped", "purpose": "manifest", "payload": b64u(payload),
         "sig": b64u(good[:-1] + bytes([good[-1] ^ 1])), "valid": False},
    ]
    r, s = int.from_bytes(good[:32], "big"), int.from_bytes(good[32:], "big")
    malformed = [
        {"note": "DER encoding instead of raw r||s", "sig": b64u(encode_dss_signature(r, s))},
        {"note": "63 bytes", "sig": b64u(good[:63])},
        {"note": "padded base64url", "sig": b64u(good) + "=="},
        {"note": "character outside the base64url alphabet", "sig": "+" + b64u(good)[1:]},
    ]
    bad_keys = [
        {"note": "compressed point (33 bytes)", "pub": b64u(DEVICE.public_key().public_bytes(
            Encoding.X962, PublicFormat.CompressedPoint))},
        {"note": "point not on curve", "pub": b64u(b"\x04" + bytes(31) + b"\x01" + bytes(31) + b"\x01")},
        {"note": "64 bytes, missing 0x04 prefix", "pub": b64u(pub_bytes(DEVICE)[1:])},
    ]
    return {"description": "Signing input is 'hka-v1/' + purpose + '\\n' + payload; signatures are raw r||s b64u",
            "pub": pub, "cases": cases, "malformed_signatures": malformed, "malformed_public_keys": bad_keys}


def request_payload(method: str, origin: str, target: str, ts: int, body: bytes) -> bytes:
    return f"{method}\n{origin}\n{target}\n{ts}\n{sha256(body).hex()}".encode()


def request_vectors() -> dict:
    device_id = key_id("d", pub_bytes(DEVICE))
    cases = []
    for label, key, prefix, method, target, ts, body in [
        ("processor lists batches", PROCESSOR, "p", "GET", f"/v1/devices/{device_id}/batches?after=41&limit=500",
         NOW, b""),
        ("device reads its state", DEVICE, "d", "GET", f"/v1/devices/{device_id}/state", NOW, b""),
        ("admin retires a device", ADMIN, "a", "POST",
         "/v1/admin/devices/" + key_id("d", pub_bytes(DEVICE2)) + "/retire", NOW + 61, b""),
    ]:
        payload = request_payload(method, SERVER, target, ts, body)
        cases.append({"note": label, "key_id": key_id(prefix, pub_bytes(key)), "pub": b64u(pub_bytes(key)),
                      "method": method, "origin": SERVER, "target": target, "timestamp": ts, "body": b64u(body),
                      "payload": payload.decode(), "sig": b64u(sign(key, "request", payload))})
    base = cases[0]
    invalid = [
        {"note": "different origin (another deployment)", **{**base, "origin": "https://other.example.test"}},
        {"note": "different target", **{**base, "target": base["target"].replace("after=41", "after=0")}},
        {"note": "different timestamp", **{**base, "timestamp": NOW + 1}},
        {"note": "different body", **{**base, "body": b64u(b"{}")}},
        {"note": "different method", **{**base, "method": "POST"}},
    ]
    for case in invalid:
        case.pop("payload", None)
    return {"description": "Request signatures: payload = METHOD\\nORIGIN\\nTARGET\\nTIMESTAMP\\nhex(sha256(body)). "
                           "'invalid' cases reuse the first case's signature with one input changed; the verifier "
                           "rebuilds the payload from the inputs and MUST reject them.",
            "server_now": NOW, "cases": cases, "invalid": invalid}


def make_invite(**over) -> tuple[bytes, str]:
    fields = {"v": 1, "t": "invite", "server": SERVER, "admin_id": key_id("a", pub_bytes(ADMIN)),
              "user_id": USER_ID, "user_name": "Mẹ", "exp": "2026-11-20T00:00:00.000Z",
              "nonce": b64u(sha256(b"hka test nonce 1")[:16])}
    signer = over.pop("_signer", ADMIN)
    fields.update(over)
    doc = compact(fields)
    return doc, f"hka-invite:{b64u(doc)}.{b64u(sign(signer, 'invite', doc))}"


def document_vectors() -> dict:
    dev_pub, proc_pub, proc2_pub, admin_pub = (pub_bytes(k) for k in (DEVICE, PROCESSOR, PROCESSOR2, ADMIN))
    device_id = key_id("d", dev_pub)

    card = compact({"v": 1, "t": "card", "name": "Test Mac", "age_recipient": AGE_RECIPIENT_1,
                    "sign_pub": b64u(proc_pub)})
    _, invite_text = make_invite()
    enroll = compact({"invite": invite_text, "device_pub": b64u(dev_pub), "device_name": "Test iPhone", "ts": NOW})

    processors = [
        {"id": key_id("p", proc_pub), "name": "Test Mac", "age_recipient": AGE_RECIPIENT_1, "sign_pub": b64u(proc_pub)},
        {"id": key_id("p", proc2_pub), "name": "Recovery", "age_recipient": AGE_RECIPIENT_2, "sign_pub": b64u(proc2_pub)},
    ]

    def grant_doc(**over) -> bytes:
        fields = {"v": 1, "t": "grant", "user_id": USER_ID, "device_id": device_id, "grant_seq": 1,
                  "processors": processors, "created_at": "2026-11-13T08:00:00.000Z"}
        fields.update(over)
        return compact(fields)

    grant = grant_doc()
    grant_hash = sha256(grant).hex()

    def manifest_doc(n: int, prev, ciphertext: bytes, **over) -> bytes:
        fields = {"v": 1, "t": "manifest", "user_id": USER_ID, "device_id": device_id, "seq": n,
                  "prev": prev, "ciphertext_sha256": sha256(ciphertext).hex(), "size": len(ciphertext),
                  "recipients": [p["id"] for p in processors], "grant_seq": 1, "grant_sha256": grant_hash,
                  "created_at": f"2026-11-13T08:0{n % 10}:00.000Z"}
        fields.update(over)
        return compact(fields)

    manifests, prev = [], None
    for seq in (1, 2, 3):
        ciphertext = sha256(f"ciphertext {seq}".encode()) * (seq * 3)
        m = manifest_doc(seq, prev, ciphertext)
        manifests.append({"seq": seq, "manifest": b64u(m), "sig": b64u(sign(DEVICE, "manifest", m)),
                          "manifest_sha256": sha256(m).hex(), "ciphertext": b64u(ciphertext)})
        prev = sha256(m).hex()

    ct4 = sha256(b"ciphertext 4") * 12
    bad_proc = [dict(processors[0], id=key_id("p", proc2_pub))]
    invalid = []

    def add_invite(note, expect, **over):
        _, text = make_invite(**over)
        invalid.append({"note": note, "kind": "invite", "text": text, "expect": expect})

    add_invite("server is another origin", "invite_invalid", server="https://other.example.test")
    add_invite("expired", "invite_invalid", exp="2026-11-12T23:59:59.000Z")
    add_invite("exp more than 7 days ahead", "invite_invalid", exp="2026-11-20T08:00:00.001Z")
    add_invite("signed by a device key, claims the admin id", "invite_invalid", _signer=DEVICE)
    add_invite("unknown admin id", "invite_invalid", admin_id="a_" + "0" * 32)
    add_invite("wrong document type", "bad_request", t="card")

    def add_grant(note, expect, doc, signer=DEVICE, purpose="grant"):
        invalid.append({"note": note, "kind": "grant", "path": f"/v1/devices/{device_id}/grant", "doc": b64u(doc),
                        "sig": b64u(sign(signer, purpose, doc)), "expect": expect})

    add_grant("processor id does not match its sign_pub", "bad_request", grant_doc(processors=bad_proc))
    add_grant("no processors", "bad_request", grant_doc(processors=[]))
    add_grant("duplicate processor", "bad_request", grant_doc(processors=[processors[0], processors[0]]))
    add_grant("grant_seq 0", "bad_request", grant_doc(grant_seq=0))
    add_grant("grant_seq is a string", "bad_request", grant_doc(grant_seq="1"))
    add_grant("signed with purpose manifest", "bad_signature", grant, purpose="manifest")
    add_grant("signed by another device", "bad_signature", grant, signer=DEVICE2)
    add_grant("device_id of another device", "bad_request", grant_doc(device_id=key_id("d", pub_bytes(DEVICE2))))
    add_grant("grant_seq 3 while only grant 1 is stored", "grant_out_of_order", grant_doc(grant_seq=3))
    add_grant("grant_seq 1 again with different bytes", "grant_conflict",
              grant_doc(created_at="2026-11-13T08:00:00.001Z"))

    def add_manifest(note, expect, checked_by, doc, signer=DEVICE, ciphertext=ct4):
        invalid.append({"note": note, "kind": "manifest", "path": f"/v1/devices/{device_id}/batches/4",
                        "manifest": b64u(doc),
                        "sig": b64u(sign(signer, "manifest", doc)), "ciphertext": b64u(ciphertext),
                        "expect": expect, "checked_by": checked_by})

    last = manifests[-1]["manifest_sha256"]
    add_manifest("prev does not match manifest 3 (seq 4)", "chain_break", "processor",
                 manifest_doc(4, "0" * 64, ct4))
    add_manifest("grant_sha256 wrong", "grant_mismatch", "server and processor",
                 manifest_doc(4, last, ct4, grant_sha256="1" * 64))
    add_manifest("recipients in a different order than the grant", "grant_mismatch", "server and processor",
                 manifest_doc(4, last, ct4, recipients=[p["id"] for p in reversed(processors)]))
    add_manifest("size does not match ciphertext", "bad_request", "server and processor",
                 manifest_doc(4, last, ct4, size=len(ct4) + 1))
    add_manifest("ciphertext_sha256 does not match ciphertext", "hash_mismatch", "server and processor",
                 manifest_doc(4, last, ct4, ciphertext_sha256="2" * 64))
    add_manifest("seq is a string", "bad_request", "server and processor", manifest_doc(4, last, ct4, seq="4"))
    add_manifest("wrong document type", "bad_request", "server and processor", manifest_doc(4, last, ct4, t="grant"))
    add_manifest("signed by another device", "bad_signature", "server and processor",
                 manifest_doc(4, last, ct4), signer=DEVICE2)

    return {
        "description": "Documents for one test user, device and two processors. 'invalid' cases are each a single "
                       "change against a valid state where manifests 1-3 and grant 1 are stored; 'expect' is the "
                       "error code (or 'chain_break' for a processor-side check).",
        "server": SERVER,
        "server_now": NOW,
        "card": {"text": "hka-card:" + b64u(card), "processor_id": key_id("p", proc_pub),
                 "fingerprint": processor_fingerprint(proc_pub, AGE_RECIPIENT_1)},
        "invite": {"text": invite_text, "admin_pub": b64u(admin_pub)},
        "enroll": {"body": b64u(enroll), "signature": b64u(sign(DEVICE, "enroll", enroll)), "device_id": device_id},
        "grant": {"doc": b64u(grant), "sig": b64u(sign(DEVICE, "grant", grant)), "grant_sha256": grant_hash},
        "manifests": manifests,
        "invalid": invalid,
    }


EXAMPLE_RECORDS = [
    {"k": "header", "v": 1, "user_id": USER_ID, "device_id": key_id("d", pub_bytes(DEVICE)),
     "created_at": "2026-11-13T08:00:00.000Z", "tz": "Asia/Ho_Chi_Minh", "app_version": "0.1 (1)",
     "os_version": "iOS 26.1", "reason": "sync",
     "counts": {"HKQuantityTypeIdentifierHeartRate": 1, "HKCategoryTypeIdentifierSleepAnalysis": 1}},
    {"k": "sample", "kind": "quantity", "type": "HKQuantityTypeIdentifierHeartRate",
     "uuid": "0B6E8F1C-2D3A-4B5C-8D9E-0F1A2B3C4D5E", "start": "2026-11-12T23:59:30.000Z",
     "end": "2026-11-12T23:59:30.000Z", "value": 58, "unit": "count/min",
     "source": {"bundle_id": "com.apple.health.81A1B9C8", "name": "Apple Watch"}, "device": None,
     "metadata": {"HKMetadataKeyHeartRateMotionContext": 1}},
    {"k": "sample", "kind": "category", "type": "HKCategoryTypeIdentifierSleepAnalysis",
     "uuid": "1C7F902D-3E4B-4C6D-9EAF-102B3C4D5E6F", "start": "2026-11-12T16:30:00.000Z",
     "end": "2026-11-12T17:10:00.000Z", "value": 4, "value_name": "asleepDeep",
     "source": {"bundle_id": "com.apple.health.81A1B9C8", "name": "Apple Watch"}, "device": None,
     "metadata": {"HKMetadataKeyTimeZone": "Asia/Ho_Chi_Minh"}},
    {"k": "deleted", "uuid": "2D80A13E-4F5C-4D7E-AFB0-213C4D5E6F70", "type": "HKQuantityTypeIdentifierStepCount",
     "observed_at": "2026-11-13T07:59:59.000Z"},
]


def example_plaintext() -> bytes:
    return gzip.compress(b"".join(compact(r) + b"\n" for r in EXAMPLE_RECORDS), mtime=0)


def age_vector() -> dict:
    age = shutil.which("age") or str(Path.home() / "go/bin/age")
    keygen = shutil.which("age-keygen") or str(Path.home() / "go/bin/age-keygen")
    plaintext = example_plaintext()
    identities, recipients = [AGE_IDENTITY_1, AGE_IDENTITY_2], [AGE_RECIPIENT_1, AGE_RECIPIENT_2]
    with tempfile.TemporaryDirectory() as tmp:
        for ident, rec in zip(identities, recipients):
            path = Path(tmp) / "id"
            path.write_text(ident + "\n")
            derived = subprocess.run([keygen, "-y", str(path)], check=True, capture_output=True, text=True).stdout.strip()
            if derived != rec:
                raise SystemExit(f"age recipient mismatch: {derived} != {rec}")
        args = [age, "--encrypt"]
        for r in recipients:
            args += ["-r", r]
        ct = subprocess.run(args, input=plaintext, check=True, capture_output=True).stdout
    return {"description": "Reference age CLI output: binary age v1 file to two X25519 recipients; "
                           "plaintext is the gzip NDJSON in examples/batch.ndjson (gzip mtime 0)",
            "generated_by": subprocess.run([age, "--version"], capture_output=True, text=True).stdout.strip(),
            "identities": identities, "recipients": recipients,
            "ciphertext": b64u(ct), "plaintext_sha256": sha256(plaintext).hex()}


def check_age_vector(path: Path) -> list[str]:
    """Decrypt age.json with pyrage and compare with the current example plaintext."""
    try:
        import pyrage
    except ImportError:
        return ["pyrage is not installed; cannot check age.json"]
    errors = []
    for ident, rec in [(AGE_IDENTITY_1, AGE_RECIPIENT_1), (AGE_IDENTITY_2, AGE_RECIPIENT_2)]:
        if str(pyrage.x25519.Identity.from_str(ident).to_public()) != rec:
            errors.append(f"bech32 mismatch for {rec}")
    v = json.loads(path.read_text())
    ct = base64.urlsafe_b64decode(v["ciphertext"] + "=" * (-len(v["ciphertext"]) % 4))
    expected = example_plaintext()
    for ident in v["identities"]:
        pt = pyrage.decrypt(ct, [pyrage.x25519.Identity.from_str(ident)])
        if pt != expected or sha256(pt).hex() != v["plaintext_sha256"]:
            errors.append("age.json plaintext differs from the current examples; regenerate with --age")
            break
    return errors


def render(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--age", action="store_true", help="regenerate the age vector (needs the age CLI)")
    ap.add_argument("--check", action="store_true", help="fail if files differ from a fresh generation")
    args = ap.parse_args()

    files = {
        "keys.json": render(keys_vectors()),
        "encodings.json": render(encoding_vectors()),
        "signatures.json": render(signature_vectors()),
        "requests.json": render(request_vectors()),
        "documents.json": render(document_vectors()),
        "../examples/batch.ndjson": "".join(compact(r).decode() + "\n" for r in EXAMPLE_RECORDS),
    }
    age_path = VECTORS / "age.json"
    if args.age or (not age_path.exists() and not args.check):
        files["age.json"] = render(age_vector())

    status = 0
    for name, text in files.items():
        path = (VECTORS / name).resolve()
        if args.check:
            if not path.exists() or path.read_text() != text:
                print(f"out of date: {path}", file=sys.stderr)
                status = 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print(f"wrote {path}")
    if args.check:
        problems = ["age.json missing"] if not age_path.exists() else check_age_vector(age_path)
        for p in problems:
            print(p, file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
