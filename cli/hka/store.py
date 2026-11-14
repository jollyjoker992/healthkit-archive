"""The processor's data directory: config, identity, pins, inbox, lock, atomic writes."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyrage

from hkacommon.documents import Card, ORIGIN_RE
from hkacommon.encoding import b64u_decode, format_instant
from hkacommon.keys import SigningKey, write_secret

EXIT_OK, EXIT_PARTIAL, EXIT_CONFIG, EXIT_LOCKED = 0, 1, 2, 3


class ConfigError(Exception):
    pass


class Locked(Exception):
    pass


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json(path: Path, value: object) -> None:
    atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode())


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def age_identity_file(identity: pyrage.x25519.Identity, created: datetime) -> bytes:
    """The format of `age-keygen`, so the stock `age -d -i` can use it."""
    return (f"# created: {created.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            f"# public key: {identity.to_public()}\n{identity}\n").encode()


def read_age_identity(path: Path) -> pyrage.x25519.Identity:
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip().startswith("AGE-SECRET-KEY-")]
    if len(lines) != 1:
        raise ConfigError(f"{path} must contain exactly one AGE-SECRET-KEY line")
    return pyrage.x25519.Identity.from_str(lines[0])


@dataclass
class Identity:
    name: str
    sign: SigningKey
    age: pyrage.x25519.Identity

    @property
    def processor_id(self) -> str:
        return self.sign.id("p")

    @property
    def age_recipient(self) -> str:
        return str(self.age.to_public())

    @property
    def card(self) -> Card:
        return Card(self.name, self.age_recipient, self.sign.pub_b64u)


def create_identity(keys_dir: Path, name: str, now: datetime) -> Identity:
    if (keys_dir / "sign.pem").exists() or (keys_dir / "age.txt").exists():
        raise ConfigError(f"{keys_dir} already holds an identity; refusing to overwrite it")
    sign = SigningKey.generate()
    age = pyrage.x25519.Identity.generate()
    sign.save(keys_dir / "sign.pem")
    write_secret(keys_dir / "age.txt", age_identity_file(age, now))
    write_secret(keys_dir / "name.txt", (name + "\n").encode())
    return Identity(name, sign, age)


def load_identity(keys_dir: Path) -> Identity:
    sign_path, age_path = keys_dir / "sign.pem", keys_dir / "age.txt"
    if not sign_path.exists() or not age_path.exists():
        raise ConfigError(f"no processor identity in {keys_dir}; run `hka init`")
    name_path = keys_dir / "name.txt"
    name = name_path.read_text().strip() if name_path.exists() else "processor"
    return Identity(name, SigningKey.load(sign_path), read_age_identity(age_path))


class Store:
    def __init__(self, root: Path):
        self.root = root

    @property
    def keys_dir(self) -> Path:
        return self.root / "keys"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    def config(self) -> dict:
        return read_json(self.config_path, {})

    def server(self, override: str | None = None) -> str:
        server = override or os.environ.get("HKA_SERVER") or self.config().get("server")
        if not server or not ORIGIN_RE.fullmatch(server):
            raise ConfigError("relay origin not set: pass --server https://… once with `hka init` or set HKA_SERVER")
        return server

    def identity(self) -> Identity:
        return load_identity(self.keys_dir)

    def check_identity(self, identity: Identity) -> None:
        """The data directory belongs to one processor: refuse to run with another key, which would
        skip every batch not addressed to it and lose them for the real key."""
        conf = self.config()
        bound = conf.get("processor_id")
        if bound is None:
            # First run of a data directory made before binding: accept the key only if the stored
            # grants (when there are any) were addressed to it.
            listed = set()
            for grant_file in (self.root / "inbox").glob("*/*/grants/*.json"):
                doc = json.loads(b64u_decode(json.loads(grant_file.read_text())["doc"]))
                listed.update(p["id"] for p in doc["processors"])
            if listed and identity.processor_id not in listed:
                raise ConfigError(f"keys/ holds {identity.processor_id}, which no stored grant lists; wrong key?")
            conf["processor_id"] = identity.processor_id
            write_json(self.config_path, conf)
        elif bound != identity.processor_id:
            raise ConfigError(f"{self.root} belongs to processor {bound}, but keys/ holds {identity.processor_id}")

    # pins -------------------------------------------------------------------
    @property
    def pins_path(self) -> Path:
        return self.root / "pins.json"

    def pins(self) -> dict:
        return read_json(self.pins_path, {})

    def save_pins(self, pins: dict) -> None:
        write_json(self.pins_path, pins)

    # inbox -------------------------------------------------------------------
    def device_dir(self, user_id: str, device_id: str) -> Path:
        return self.root / "inbox" / user_id / device_id

    def user_dir(self, user_id: str) -> Path:
        return self.root / "users" / user_id

    def inbox_devices(self):
        inbox = self.root / "inbox"
        if not inbox.is_dir():
            return
        for user in sorted(p for p in inbox.iterdir() if p.is_dir()):
            for dev in sorted(p for p in user.iterdir() if p.is_dir()):
                yield user.name, dev.name

    @contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Locked("another hka run holds the lock") from None
            yield
        finally:
            os.close(fd)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def instant(dt: datetime) -> str:
    return format_instant(dt)
