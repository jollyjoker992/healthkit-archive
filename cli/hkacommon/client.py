"""HTTP client for the relay with signed requests (spec section 5). Standard library only."""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .encoding import sha256_hex
from .keys import SigningKey

USER_AGENT = "hka/0.1"
MAX_JSON_BYTES = 16 * 1024 * 1024


@dataclass
class ApiError(Exception):
    status: int
    code: str
    message: str
    body: dict

    def __str__(self) -> str:
        return f"{self.status} {self.code}: {self.message}"


class Client:
    def __init__(self, origin: str, key: SigningKey, key_prefix: str, timeout: float = 60.0):
        self.origin = origin.rstrip("/")
        self.key = key
        self.key_id = key.id(key_prefix)
        self.timeout = timeout
        self.clock_skew = 0  # learned from server_time on a 401 stale_timestamp

    def _headers(self, method: str, target: str, body: bytes) -> dict[str, str]:
        ts = str(int(time.time()) + self.clock_skew)
        payload = f"{method}\n{self.origin}\n{target}\n{ts}\n{sha256_hex(body)}".encode()
        return {"HKA-Key": self.key_id, "HKA-Timestamp": ts, "HKA-Signature": self.key.sign("request", payload),
                "User-Agent": USER_AGENT}

    def request(self, method: str, target: str, *, raw: bool = False, max_bytes: int = MAX_JSON_BYTES,
                _retry: bool = True):
        """Signed request with an empty body. Returns parsed JSON, or bytes when raw=True.

        Reads at most max_bytes; a larger response is an error (the relay is not trusted)."""
        req = urllib.request.Request(self.origin + target, method=method, headers=self._headers(method, target, b""))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = resp.read(max_bytes + 1)
        except http.client.HTTPException as exc:  # truncated or malformed HTTP response
            raise ApiError(0, "bad_response", f"broken HTTP response: {exc!r}", {}) from None
        except urllib.error.HTTPError as exc:
            data = exc.read(64 * 1024)
            try:
                body = json.loads(data)
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            err = ApiError(exc.code, body.get("error", "http_error"), body.get("message", str(exc)), body)
            if err.code == "stale_timestamp" and _retry and isinstance(body.get("server_time"), int):
                self.clock_skew = body["server_time"] - int(time.time())
                return self.request(method, target, raw=raw, max_bytes=max_bytes, _retry=False)
            raise err from None
        if len(data) > max_bytes:
            raise ApiError(0, "response_too_large", f"response larger than {max_bytes} bytes", {})
        if raw:
            return data
        try:
            return json.loads(data)
        except (ValueError, RecursionError):
            raise ApiError(0, "bad_response", "the relay's response is not usable JSON", {}) from None

    def get(self, target: str):
        return self.request("GET", target)

    def get_bytes(self, target: str, max_bytes: int) -> bytes:
        return self.request("GET", target, raw=True, max_bytes=max_bytes)

    def post(self, target: str):
        return self.request("POST", target)

    def delete(self, target: str):
        return self.request("DELETE", target)
