"""Cards, invites, grants and manifests (spec sections 3 and 4)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .encoding import (FormatError, b64u_decode, b64u_encode, compact_json, is_hex, is_instant, is_int, is_name,
                       parse_json_object, sha256_hex)
from .keys import SigningKey, key_id, load_public_key, processor_fingerprint, public_bytes, verify

ID_RE = {p: re.compile(rf"{p}_[0-9a-f]{{32}}") for p in ("d", "p", "a", "u")}
AGE_RECIPIENT_RE = re.compile(r"age1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}")
ORIGIN_RE = re.compile(r"https://[a-z0-9.-]+(:[0-9]{1,5})?|http://localhost(:[0-9]{1,5})?")


def _check(cond: bool, message: str) -> None:
    if not cond:
        raise FormatError(message)


def _check_type(doc: dict, t: str) -> None:
    _check(doc.get("v") == 1 and type(doc.get("v")) is int, f"{t}: unsupported version")
    _check(doc.get("t") == t, f"expected a {t} document")


def new_user_id() -> str:
    return "u_" + os.urandom(16).hex()


# ---------------------------------------------------------------------------- card

@dataclass(frozen=True)
class Card:
    name: str
    age_recipient: str
    sign_pub: str

    @property
    def processor_id(self) -> str:
        return key_id("p", b64u_decode(self.sign_pub))

    @property
    def fingerprint(self) -> str:
        return processor_fingerprint(b64u_decode(self.sign_pub), self.age_recipient)

    def text(self) -> str:
        return "hka-card:" + b64u_encode(compact_json(
            {"v": 1, "t": "card", "name": self.name, "age_recipient": self.age_recipient, "sign_pub": self.sign_pub}))

    def grant_entry(self) -> dict:
        return {"id": self.processor_id, "name": self.name, "age_recipient": self.age_recipient, "sign_pub": self.sign_pub}


def parse_card(text: str) -> Card:
    _check(isinstance(text, str) and text.startswith("hka-card:") and len(text) <= 1024, "card: malformed")
    doc = parse_json_object(b64u_decode(text[len("hka-card:"):]))
    _check_type(doc, "card")
    _check(is_name(doc.get("name")), "card: name")
    _check(isinstance(doc.get("age_recipient"), str) and AGE_RECIPIENT_RE.fullmatch(doc["age_recipient"]) is not None,
           "card: age_recipient")
    load_public_key(doc.get("sign_pub"))
    return Card(doc["name"], doc["age_recipient"], doc["sign_pub"])


# ---------------------------------------------------------------------------- invite

def make_invite(admin: SigningKey, server: str, user_id: str, user_name: str, exp: str) -> str:
    _check(ORIGIN_RE.fullmatch(server) is not None, "server must be https://host[:port] without a path")
    _check(ID_RE["u"].fullmatch(user_id) is not None, "user_id")
    _check(is_name(user_name), "user_name must be 1 to 64 characters")
    _check(is_instant(exp), "exp")
    doc = compact_json({"v": 1, "t": "invite", "server": server, "admin_id": admin.id("a"), "user_id": user_id,
                        "user_name": user_name, "exp": exp, "nonce": b64u_encode(os.urandom(16))})
    text = f"hka-invite:{b64u_encode(doc)}.{admin.sign('invite', doc)}"
    _check(len(text) <= 1024, "invite longer than 1 KiB (shorten the name)")
    return text


# ---------------------------------------------------------------------------- grant

@dataclass(frozen=True)
class Grant:
    raw: bytes
    sha256: str
    user_id: str
    device_id: str
    grant_seq: int
    processors: tuple[dict, ...]

    @property
    def processor_ids(self) -> list[str]:
        return [p["id"] for p in self.processors]


def parse_grant(raw: bytes) -> Grant:
    _check(len(raw) <= 16 * 1024, "grant: too large")
    doc = parse_json_object(raw)
    _check_type(doc, "grant")
    _check(isinstance(doc.get("user_id"), str) and ID_RE["u"].fullmatch(doc["user_id"]) is not None, "grant: user_id")
    _check(isinstance(doc.get("device_id"), str) and ID_RE["d"].fullmatch(doc["device_id"]) is not None, "grant: device_id")
    _check(is_int(doc.get("grant_seq"), 1), "grant: grant_seq")
    _check(is_instant(doc.get("created_at")), "grant: created_at")
    procs = doc.get("processors")
    _check(isinstance(procs, list) and 1 <= len(procs) <= 8, "grant: processors")
    seen: set[str] = set()
    for p in procs:
        _check(isinstance(p, dict), "grant: processor entry")
        _check(isinstance(p.get("id"), str) and ID_RE["p"].fullmatch(p["id"]) is not None and p["id"] not in seen,
               "grant: processor id")
        _check(is_name(p.get("name")), "grant: processor name")
        _check(isinstance(p.get("age_recipient"), str) and AGE_RECIPIENT_RE.fullmatch(p["age_recipient"]) is not None,
               "grant: age_recipient")
        _check(key_id("p", public_bytes(load_public_key(p.get("sign_pub")))) == p["id"],
               "grant: processor id does not match sign_pub")
        seen.add(p["id"])
    return Grant(raw, sha256_hex(raw), doc["user_id"], doc["device_id"], doc["grant_seq"], tuple(procs))


# ---------------------------------------------------------------------------- manifest

@dataclass(frozen=True)
class Manifest:
    raw: bytes
    sha256: str
    user_id: str
    device_id: str
    seq: int
    prev: str | None
    ciphertext_sha256: str
    size: int
    recipients: tuple[str, ...]
    grant_seq: int
    grant_sha256: str
    created_at: str


def parse_manifest(raw: bytes) -> Manifest:
    _check(len(raw) <= 4096, "manifest: too large")
    doc = parse_json_object(raw)
    _check_type(doc, "manifest")
    _check(isinstance(doc.get("user_id"), str) and ID_RE["u"].fullmatch(doc["user_id"]) is not None, "manifest: user_id")
    _check(isinstance(doc.get("device_id"), str) and ID_RE["d"].fullmatch(doc["device_id"]) is not None,
           "manifest: device_id")
    _check(is_int(doc.get("seq"), 1), "manifest: seq")
    seq = doc["seq"]
    _check(doc.get("prev") is None if seq == 1 else is_hex(doc.get("prev"), 32), "manifest: prev")
    _check(is_hex(doc.get("ciphertext_sha256"), 32), "manifest: ciphertext_sha256")
    _check(is_int(doc.get("size"), 1), "manifest: size")
    rec = doc.get("recipients")
    _check(isinstance(rec, list) and 1 <= len(rec) <= 8
           and all(isinstance(r, str) and ID_RE["p"].fullmatch(r) for r in rec), "manifest: recipients")
    _check(is_int(doc.get("grant_seq"), 1), "manifest: grant_seq")
    _check(is_hex(doc.get("grant_sha256"), 32), "manifest: grant_sha256")
    _check(is_instant(doc.get("created_at")), "manifest: created_at")
    return Manifest(raw, sha256_hex(raw), doc["user_id"], doc["device_id"], seq, doc.get("prev"),
                    doc["ciphertext_sha256"], doc["size"], tuple(rec), doc["grant_seq"], doc["grant_sha256"],
                    doc["created_at"])


def verify_signed(pub_b64u: str, purpose: str, raw: bytes, sig: str) -> bool:
    return verify(load_public_key(pub_b64u), purpose, raw, sig)
