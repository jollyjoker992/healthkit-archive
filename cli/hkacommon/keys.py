"""P-256 signing keys, identifiers and fingerprints (spec section 2)."""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

from .encoding import FormatError, b64u_decode, b64u_encode, sha256

PURPOSES = ("invite", "enroll", "grant", "manifest", "request")


def signing_input(purpose: str, payload: bytes) -> bytes:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose {purpose}")
    return b"hka-v1/" + purpose.encode("ascii") + b"\n" + payload


def load_public_key(b64u: object) -> ec.EllipticCurvePublicKey:
    """SEC1 uncompressed, exactly 65 bytes, on the curve."""
    if not isinstance(b64u, str):
        raise FormatError("public key must be a string")
    raw = b64u_decode(b64u)
    if len(raw) != 65 or raw[0] != 0x04:
        raise FormatError("public key must be a 65-byte uncompressed P-256 point")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError as exc:
        raise FormatError("public key is not on the curve") from exc


def public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def key_id(prefix: str, pub: bytes) -> str:
    if prefix not in ("d", "p", "a"):
        raise ValueError(prefix)
    return f"{prefix}_{sha256(pub)[:16].hex()}"


def fingerprint(data: bytes) -> str:
    h = sha256(data)[:10].hex().upper()
    return "-".join(h[i:i + 4] for i in range(0, 20, 4))


def processor_fingerprint(sign_pub: bytes, age_recipient: str) -> str:
    return fingerprint(sign_pub + age_recipient.encode("ascii"))


def verify(key: ec.EllipticCurvePublicKey, purpose: str, payload: bytes, sig_b64u: object) -> bool:
    if not isinstance(sig_b64u, str):
        return False
    try:
        sig = b64u_decode(sig_b64u)
    except FormatError:
        return False
    if len(sig) != 64:
        return False
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    try:
        key.verify(der, signing_input(purpose, payload), ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False


class SigningKey:
    """A P-256 private key stored as PKCS#8 PEM (mode 600)."""

    def __init__(self, key: ec.EllipticCurvePrivateKey):
        self._key = key
        self.pub = public_bytes(key.public_key())

    @classmethod
    def generate(cls) -> "SigningKey":
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def load(cls, path: Path) -> "SigningKey":
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise FormatError(f"{path} is not a P-256 private key")
        return cls(key)

    @classmethod
    def from_scalar(cls, scalar: int) -> "SigningKey":
        return cls(ec.derive_private_key(scalar, ec.SECP256R1()))

    def save(self, path: Path) -> None:
        pem = self._key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption())
        write_secret(path, pem)

    @property
    def pub_b64u(self) -> str:
        return b64u_encode(self.pub)

    def id(self, prefix: str) -> str:
        return key_id(prefix, self.pub)

    def sign(self, purpose: str, payload: bytes) -> str:
        der = self._key.sign(signing_input(purpose, payload), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return b64u_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def write_secret(path: Path, data: bytes) -> None:
    """Create a file readable only by the owner; refuse to overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
