"""Strict encodings from spec/README.md section 1."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone

_B64U_RE = re.compile(r"[A-Za-z0-9_-]*")
_HEX_RE = re.compile(r"(?:[0-9a-f]{2})*")
_INSTANT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


class FormatError(ValueError):
    """Input does not follow the protocol's encoding rules."""


def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    """Canonical base64url without padding; raises FormatError otherwise."""
    if not isinstance(text, str) or not _B64U_RE.fullmatch(text) or len(text) % 4 == 1:
        raise FormatError("not canonical base64url")
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64u_encode(data) != text:
        raise FormatError("not canonical base64url (trailing bits)")
    return data


def is_hex(text: object, nbytes: int | None = None) -> bool:
    return (isinstance(text, str) and _HEX_RE.fullmatch(text) is not None
            and (nbytes is None or len(text) == 2 * nbytes))


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_instant(value: object) -> bool:
    if not isinstance(value, str) or not _INSTANT_RE.fullmatch(value):
        return False
    try:
        parsed = parse_instant(value)
    except ValueError:
        return False
    return format_instant(parsed) == value


def parse_instant(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def format_instant(dt: datetime) -> str:
    """UTC, three fractional digits, truncated."""
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def now_instant() -> str:
    return format_instant(datetime.now(timezone.utc))


def is_int(value: object, minimum: int = 0) -> bool:
    """A JSON integer (never a bool or float) within the safe range."""
    return type(value) is int and minimum <= value <= 2**53 - 1


def is_name(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 64  # Python len counts code points


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise FormatError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _reject_constant(name: str):
    raise FormatError(f"{name} is not allowed in JSON")


def parse_json_object(data: bytes) -> dict:
    """Strict UTF-8, no duplicate keys, no NaN/Infinity, must be an object."""
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FormatError("invalid UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise FormatError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise FormatError("not a JSON object")
    return value


def compact_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
