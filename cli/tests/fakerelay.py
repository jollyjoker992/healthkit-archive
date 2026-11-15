"""An in-memory relay that serves whatever the test puts in it, honest or not.

It implements the processor endpoints, with the client's interface (`get`, `get_bytes`,
`put_document`), so pull logic can be tested against a lying relay.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from hkacommon.client import ApiError
from hkacommon.encoding import b64u_encode


@dataclass
class Stream:
    device_pub: str
    user_id: str
    user_name: str = "Mom"
    device_name: str = "Mom's iPhone"
    status: str = "active"
    granted: bool = True
    grants: list[dict] = field(default_factory=list)
    batches: dict[int, dict] = field(default_factory=dict)
    ciphertexts: dict[int, bytes] = field(default_factory=dict)


class FakeRelay:
    def __init__(self):
        self.streams: dict[str, Stream] = {}
        self.unknown = False
        self.not_granted: set[str] = set()
        self.receipts: list[tuple[str, bytes, str]] = []  # (target, doc, sig) as received
        self.receipt_error: ApiError | None = None

    def add_grant(self, device_id: str, raw: bytes, sig: str) -> None:
        s = self.streams[device_id]
        seq = len(s.grants) + 1
        s.grants.append({"grant_seq": seq, "doc": b64u_encode(raw), "sig": sig, "received_at": "2026-11-13T08:00:00.000Z"})

    def add_batch(self, device_id: str, sealed, received_at: str = "2026-11-13T08:00:00.000Z") -> None:
        seq, manifest, sig, ct = sealed
        s = self.streams[device_id]
        s.batches[seq] = {"seq": seq, "manifest": b64u_encode(manifest), "sig": sig, "size": len(ct),
                          "received_at": received_at}
        s.ciphertexts[seq] = ct

    def _contiguous(self, s: Stream) -> int:
        n = 0
        while n + 1 in s.batches:
            n += 1
        return n

    def put_document(self, target: str, doc: bytes, sig: str):
        if self.receipt_error:
            raise self.receipt_error
        self.receipts.append((target, doc, sig))
        return {}

    def get(self, target: str):
        if target == "/v1/streams":
            if self.unknown:
                raise ApiError(401, "unknown_key", "unknown key", {})
            return {"streams": [{
                "user_id": s.user_id, "user_name": s.user_name, "device_id": did, "device_name": s.device_name,
                "device_pub": s.device_pub, "status": s.status, "granted": s.granted,
                "contiguous_seq": self._contiguous(s), "max_seq": max(s.batches, default=0),
                "grant_seq": len(s.grants)} for did, s in self.streams.items()]}
        m = re.fullmatch(r"/v1/devices/(d_[0-9a-f]{32})/grants", target)
        if m:
            return {"grants": self.streams[m[1]].grants}
        m = re.fullmatch(r"/v1/devices/(d_[0-9a-f]{32})/batches\?after=(\d+)&limit=(\d+)", target)
        if m:
            s = self.streams[m[1]]
            if m[1] in self.not_granted or not s.granted:
                raise ApiError(403, "not_granted", "not granted", {})
            after, limit = int(m[2]), int(m[3])
            seqs = sorted(q for q in s.batches if q > after)[:limit]
            return {"batches": [s.batches[q] for q in seqs], "contiguous_seq": self._contiguous(s),
                    "max_seq": max(s.batches, default=0)}
        raise AssertionError(f"unexpected GET {target}")

    def get_bytes(self, target: str, max_bytes: int) -> bytes:
        m = re.fullmatch(r"/v1/devices/(d_[0-9a-f]{32})/batches/(\d+)", target)
        assert m, target
        return self.streams[m[1]].ciphertexts[int(m[2])]
