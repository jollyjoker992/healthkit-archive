"""A software stand-in for the iOS app: enrols, signs grants and manifests, encrypts batches.

Used by the CLI tests and the local end-to-end run. It follows the device rules of
spec/README.md 4.2 and 4.3 (grants and manifests are signed once; the chain is local).
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request
import uuid as uuidlib
from dataclasses import dataclass, field

import pyrage

from hkacommon.documents import Card
from hkacommon.encoding import b64u_encode, compact_json, sha256_hex
from hkacommon.keys import SigningKey


def sample(type_: str, start: str, end: str | None = None, value=1, unit="count", **extra) -> dict:
    rec = {"k": "sample", "kind": "quantity", "type": type_, "uuid": str(uuidlib.uuid4()).upper(), "start": start,
           "end": end or start, "value": value, "unit": unit,
           "source": {"bundle_id": "com.apple.health.TEST", "name": "Test Watch"}, "device": None, "metadata": {}}
    rec.update(extra)
    return rec


@dataclass
class FakeDevice:
    user_id: str
    tz: str = "Asia/Ho_Chi_Minh"
    key: SigningKey = field(default_factory=SigningKey.generate)
    seq: int = 0
    prev: str | None = None
    grant_seq: int = 0
    grant_raw: bytes | None = None
    processors: list[Card] = field(default_factory=list)

    @property
    def device_id(self) -> str:
        return self.key.id("d")

    # ------------------------------------------------------------------ documents
    def enroll_body(self, invite: str, ts: int | None = None, name: str = "Test iPhone") -> tuple[bytes, str]:
        body = compact_json({"invite": invite, "device_pub": self.key.pub_b64u, "device_name": name,
                             "ts": int(time.time()) if ts is None else ts})
        return body, self.key.sign("enroll", body)

    def make_grant(self, cards: list[Card]) -> tuple[bytes, str]:
        self.grant_seq += 1
        self.processors = cards
        raw = compact_json({"v": 1, "t": "grant", "user_id": self.user_id, "device_id": self.device_id,
                            "grant_seq": self.grant_seq, "processors": [c.grant_entry() for c in cards],
                            "created_at": "2026-11-13T08:00:00.000Z"})
        self.grant_raw = raw
        return raw, self.key.sign("grant", raw)

    def seal(self, records: list[dict], reason: str = "sync") -> tuple[int, bytes, str, bytes]:
        """Returns (seq, manifest bytes, manifest signature, ciphertext)."""
        counts: dict[str, int] = {}
        for r in records:
            if r.get("k") in ("sample", "workout") and isinstance(r.get("type"), str):
                counts[r["type"]] = counts.get(r["type"], 0) + 1
        header = {"k": "header", "v": 1, "user_id": self.user_id, "device_id": self.device_id,
                  "created_at": "2026-11-13T08:00:00.000Z", "tz": self.tz, "app_version": "test",
                  "os_version": "test", "reason": reason, "counts": counts}
        plain = gzip.compress(b"".join(compact_json(r) + b"\n" for r in [header, *records]))
        recipients = [pyrage.x25519.Recipient.from_str(c.age_recipient) for c in self.processors]
        ciphertext = pyrage.encrypt(plain, recipients)
        self.seq += 1
        manifest = compact_json({
            "v": 1, "t": "manifest", "user_id": self.user_id, "device_id": self.device_id, "seq": self.seq,
            "prev": self.prev, "ciphertext_sha256": sha256_hex(ciphertext), "size": len(ciphertext),
            "recipients": [c.processor_id for c in self.processors], "grant_seq": self.grant_seq,
            "grant_sha256": sha256_hex(self.grant_raw), "created_at": "2026-11-13T08:00:00.000Z"})
        self.prev = sha256_hex(manifest)
        return self.seq, manifest, self.key.sign("manifest", manifest), ciphertext

    # ------------------------------------------------------------------ HTTP (for a real relay)
    def http(self, origin: str, method: str, path: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
        req = urllib.request.Request(origin + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def enroll(self, origin: str, invite: str) -> tuple[int, dict]:
        body, sig = self.enroll_body(invite)
        return self.http(origin, "POST", "/v1/enroll", body, {"HKA-Signature": sig, "Content-Type": "application/json"})

    def upload_grant(self, origin: str, raw: bytes, sig: str) -> tuple[int, dict]:
        body = compact_json({"doc": b64u_encode(raw), "sig": sig})
        return self.http(origin, "PUT", f"/v1/devices/{self.device_id}/grant", body, {})

    def upload(self, origin: str, sealed: tuple[int, bytes, str, bytes]) -> tuple[int, dict]:
        seq, manifest, sig, ciphertext = sealed
        return self.http(origin, "PUT", f"/v1/devices/{self.device_id}/batches/{seq}", ciphertext, {
            "HKA-Manifest": b64u_encode(manifest), "HKA-Signature": sig,
            "Content-Type": "application/octet-stream"})
