"""`hka backup` / `hka restore`: an encrypted copy of the day archives in the cloud.

A target is enabled by its credentials in the environment (or `.env`):

  aws     HKA_AWS_ACCESS_KEY_ID, HKA_AWS_SECRET_ACCESS_KEY, HKA_AWS_BUCKET (bucket[/prefix]),
          HKA_AWS_REGION (default us-east-1), HKA_AWS_STORAGE_CLASS (archives only; optional)
  r2      HKA_R2_ACCOUNT_ID, HKA_R2_ACCESS_KEY_ID, HKA_R2_SECRET_ACCESS_KEY, HKA_R2_BUCKET (bucket[/prefix])
  gdrive  HKA_GDRIVE_TOKEN (JSON from `rclone authorize drive ID SECRET`), HKA_GDRIVE_CLIENT_ID,
          HKA_GDRIVE_CLIENT_SECRET, HKA_GDRIVE_FOLDER (default healthkit-archive)
  rclone  HKA_BACKUP_RCLONE_REMOTE: any remote:path from your own rclone configuration

Every file leaves this computer age-encrypted to the processor's own key (keys/age.txt) and to any
HKA_BACKUP_RECIPIENTS (e.g. the offline recovery identity), so the provider only sees ciphertext.
Only final days are uploaded (unchanged for HKA_SETTLE_DAYS, default 7).
Remote layout per target, under users/<user_id>/: archives/YYYY/YYYY-MM-DD.<key>.tar.gz.age (one per
final day), extras/<profile.ndjson, undated/…, other/…>.<key>.age, and manifest.json.age: the
user's manifest plus, per day and extra file, the newest version this target holds (a reopened day
keeps its last final archive). <key> changes with the content and the recipients, so files are never
overwritten and manifest.json.age, written last, is the only file that changes. Restore needs only
age.txt: `age -d -i keys/age.txt FILE.age | tar xz`. age does not authenticate the sender, so
restore checks every name in the index against the pattern it must have.

Uploads go through rclone; the vendor targets are defined with RCLONE_CONFIG_* environment variables
(no secret on a command line) against a private rclone.conf in backup/, so the user's own rclone
configuration is neither read nor written. Each run lists the targets; backup/state.json caches what
they hold and is rebuilt from the listing and the previous index if lost. Nothing is ever deleted from a target.
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import pyrage

from hkacommon.encoding import sha256_hex
from .store import ConfigError, Store, atomic_write, instant, read_age_identity, read_json, write_json

RCLONE_DIR_NOT_FOUND = (3, 4)
RETRIES = ["--retries", "3", "--low-level-retries", "20"]
MANIFEST = "manifest.json.age"
DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
SHA_RE = re.compile(r"[0-9a-f]{64}")
INDEX_FORMAT = 2
SAFE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,200}", re.ASCII)
NAME_KEY = r"[0-9a-f]{16}\.[0-9a-f]{8}"  # HMAC of (content, recipients) . recipients key
ARCHIVE_NAME_RE = re.compile(rf"archives/(\d{{4}})/(\d{{4}}-\d{{2}}-\d{{2}})\.{NAME_KEY}\.tar\.gz\.age", re.ASCII)
EXTRA_NAME_RE = re.compile(rf"extras/(.+)\.{NAME_KEY}\.age", re.ASCII | re.DOTALL)
MAX_ARCHIVE_PLAINTEXT = 256 << 20  # a day archive is a few hundred KB; refuse to inflate a bomb


def file_name_ok(name: str) -> bool:
    """A file name build can write (days.file_name: Unicode letters and digits, _-.) that is safe to
    join to a folder: one component, not hidden (build_archive skips those too)."""
    return isinstance(name, str) and 0 < len(name) <= 255 and "/" not in name and "\\" not in name \
        and not name.startswith(".") and all(ord(c) >= 32 and ord(c) != 127 for c in name)


def extra_path_ok(path: str) -> bool:
    sub, _, name = path.partition("/") if isinstance(path, str) else ("", "", "")
    return path == "profile.ndjson" or (sub in ("undated", "other") and file_name_ok(name))


class BackupError(Exception):
    pass


class Listed(NamedTuple):
    """A file on a target, as rclone lists it."""
    mtime: str      # the provider's modification time, as an hka instant
    size: int
    tier: str       # S3 storage class ("" where the backend has none)

    @property
    def cold(self) -> bool:
        """In S3 Glacier: cannot be downloaded without a restore request, whatever the setting is now."""
        return self.tier in ("GLACIER", "DEEP_ARCHIVE")


@dataclass
class Target:
    name: str
    remote: str                       # rclone remote:path that holds users/
    env: dict = field(default_factory=dict)
    private_config: bool = True       # vendor targets; False for HKA_BACKUP_RCLONE_REMOTE
    archive_flags: list = field(default_factory=list)
    cold: bool = False                # archives cannot be read back without a restore request (S3 Glacier)


def _need(env: dict, name: str, keys: list[str]) -> list[str]:
    missing = [k for k in keys if not env.get(k)]
    if missing:
        raise ConfigError(f"{name} backup: set {', '.join(missing)} (or remove the other HKA_{name.upper()}_* settings)")
    return [env[k] for k in keys]


def _bucket_path(value: str, var: str) -> str:
    path = value.strip().strip("/")
    if not path or ":" in path:
        raise ConfigError(f"{var} must be bucket or bucket/prefix")
    return path


def targets_from_env(env: dict | None = None, only: str | None = None) -> dict[str, Target]:
    """Targets whose credentials are set. A target with only some of its settings is a ConfigError.
    With ``only``, the other targets are neither built nor checked (restore needs one)."""
    env = os.environ if env is None else env
    out: dict[str, Target] = {}
    if only in (None, "aws") and any(env.get(k) for k in ("HKA_AWS_ACCESS_KEY_ID", "HKA_AWS_SECRET_ACCESS_KEY", "HKA_AWS_BUCKET")):
        key, secret, bucket = _need(env, "aws", ["HKA_AWS_ACCESS_KEY_ID", "HKA_AWS_SECRET_ACCESS_KEY", "HKA_AWS_BUCKET"])
        t = Target("aws", f"hkaaws:{_bucket_path(bucket, 'HKA_AWS_BUCKET')}", {
            "TYPE": "s3", "PROVIDER": "AWS", "ENV_AUTH": "false", "ACCESS_KEY_ID": key,
            "SECRET_ACCESS_KEY": secret, "REGION": env.get("HKA_AWS_REGION") or "us-east-1",
            "NO_CHECK_BUCKET": "true"})
        if env.get("HKA_AWS_STORAGE_CLASS"):
            t.archive_flags = ["--s3-storage-class", env["HKA_AWS_STORAGE_CLASS"]]
            t.cold = env["HKA_AWS_STORAGE_CLASS"].upper() in ("GLACIER", "DEEP_ARCHIVE")
        out["aws"] = t
    if only in (None, "r2") and any(env.get(k) for k in ("HKA_R2_ACCESS_KEY_ID", "HKA_R2_SECRET_ACCESS_KEY", "HKA_R2_BUCKET", "HKA_R2_ACCOUNT_ID")):
        account, key, secret, bucket = _need(env, "r2", ["HKA_R2_ACCOUNT_ID", "HKA_R2_ACCESS_KEY_ID",
                                                         "HKA_R2_SECRET_ACCESS_KEY", "HKA_R2_BUCKET"])
        if not re.fullmatch(r"[0-9a-f]{32}", account):
            raise ConfigError("HKA_R2_ACCOUNT_ID must be the 32-character Cloudflare account id")
        out["r2"] = Target("r2", f"hkar2:{_bucket_path(bucket, 'HKA_R2_BUCKET')}", {
            "TYPE": "s3", "PROVIDER": "Cloudflare", "ENV_AUTH": "false", "ACCESS_KEY_ID": key,
            "SECRET_ACCESS_KEY": secret, "ENDPOINT": f"https://{account}.r2.cloudflarestorage.com",
            "REGION": "auto", "ACL": "private", "NO_CHECK_BUCKET": "true"})
    if only in (None, "gdrive") and any(env.get(k) for k in ("HKA_GDRIVE_TOKEN", "HKA_GDRIVE_CLIENT_ID", "HKA_GDRIVE_CLIENT_SECRET")):
        token, cid, csecret = _need(env, "gdrive", ["HKA_GDRIVE_TOKEN", "HKA_GDRIVE_CLIENT_ID", "HKA_GDRIVE_CLIENT_SECRET"])
        try:
            if not json.loads(token).get("refresh_token"):
                raise ValueError
        except (ValueError, AttributeError):
            raise ConfigError("HKA_GDRIVE_TOKEN must be the JSON token printed by `rclone authorize drive`, "
                              "with a refresh_token") from None
        folder = (env.get("HKA_GDRIVE_FOLDER") or "healthkit-archive").strip("/")
        if ":" in folder:
            raise ConfigError("HKA_GDRIVE_FOLDER must be a folder path in My Drive")
        out["gdrive"] = Target("gdrive", f"hkagdrive:{folder}", {
            "TYPE": "drive", "SCOPE": "drive", "TOKEN": token, "CLIENT_ID": cid, "CLIENT_SECRET": csecret})
    if only in (None, "rclone") and env.get("HKA_BACKUP_RCLONE_REMOTE"):
        out["rclone"] = Target("rclone", env["HKA_BACKUP_RCLONE_REMOTE"].rstrip("/") or "/", private_config=False)
    return out


def recipients(store: Store, env: dict | None = None) -> list:
    env = os.environ if env is None else env
    out = [store.identity().age.to_public()]
    for text in re.split(r"[\s,]+", env.get("HKA_BACKUP_RECIPIENTS", "").strip()):
        if not text:
            continue
        try:
            out.append(pyrage.x25519.Recipient.from_str(text))
        except Exception:
            raise ConfigError(f"HKA_BACKUP_RECIPIENTS: {text[:12]}… is not an age1… recipient") from None
    return out


def _instant(text: object) -> str:
    """rclone's ModTime (RFC 3339, up to nanoseconds) as an hka instant; "" when absent."""
    m = re.fullmatch(r"(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)", str(text or ""))
    if not m:
        return ""
    try:
        dt = datetime.fromisoformat(f"{m[1]}.{(m[2] or '0')[:6].ljust(6, '0')}{'+00:00' if m[3] == 'Z' else m[3]}")
    except ValueError:
        return ""
    return instant(dt)


def rjoin(remote: str, rel: str) -> str:
    """Path of ``rel`` under an rclone remote; a bare ``name:`` takes no slash."""
    return f"{remote}{rel}" if remote.endswith(":") else f"{remote}/{rel}"


class Rclone:
    def __init__(self, store: Store, bin_: str | None = None):
        self.bin = bin_ or os.environ.get("HKA_RCLONE_BIN") or "rclone"
        if not shutil.which(self.bin):
            raise ConfigError(f"rclone ({self.bin!r}) not found: install it (https://rclone.org/install/) "
                              "or set HKA_RCLONE_BIN")
        self.config = store.root / "backup" / "rclone.conf"

    def env(self, target: Target) -> dict:
        env = dict(os.environ)
        if target.private_config:
            # rclone saves a refreshed Drive token into the config file: keep that in our own file.
            if not self.config.exists():
                atomic_write(self.config, b"")
            env["RCLONE_CONFIG"] = str(self.config)
            section = f"RCLONE_CONFIG_{target.remote.partition(':')[0].upper()}_"
            env.update({section + k: v for k, v in target.env.items()})
        return env

    def run(self, target: Target, *args: str, timeout: int = 3600) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([self.bin, *args, *RETRIES], capture_output=True, text=True,
                                  timeout=timeout, env=self.env(target))
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 124, "", f"rclone timed out after {timeout} s")

    @staticmethod
    def error(result: subprocess.CompletedProcess) -> str:
        tail = [ln for ln in (result.stderr or "").strip().splitlines() if ln.strip()][-2:]
        return f"rclone exited {result.returncode}: {' | '.join(tail)}"

    def listing(self, target: Target, path: str) -> dict[str, "Listed"]:
        """{relative path: Listed(mtime, size, tier)} of the files under ``path``; empty when it does
        not exist yet."""
        r = self.run(target, "lsjson", "--recursive", "--files-only", "--use-server-modtime",  # no HEAD per object
                     rjoin(target.remote, path), timeout=600)
        if r.returncode in RCLONE_DIR_NOT_FOUND:
            return {}
        if r.returncode != 0:
            raise BackupError(self.error(r))
        return {e["Path"]: Listed(_instant(e.get("ModTime")), int(e.get("Size", -1)), str(e.get("Tier") or "").upper())
                for e in json.loads(r.stdout or "[]")}

    def _copy(self, target: Target, src: str, dest: str, rels: list[str], flags=()) -> str | None:
        """rclone copy of exactly ``rels`` (relative to ``src``). Returns an error, or None."""
        self.config.parent.mkdir(parents=True, exist_ok=True)
        fd, listing = tempfile.mkstemp(prefix=".files-from-", dir=self.config.parent)
        try:
            with os.fdopen(fd, "w") as f:
                f.write("".join(r + "\n" for r in rels))
            r = self.run(target, "copy", src, dest, "--files-from-raw", listing, "--no-traverse",
                         "--ignore-times", *flags, timeout=4 * 3600)
        finally:
            os.unlink(listing)
        return None if r.returncode == 0 else self.error(r)

    def copy(self, target: Target, src: Path, dest: str, rels: list[str], flags=()) -> str | None:
        """Upload ``rels`` (relative to the local ``src``) to ``dest`` on the target."""
        return self._copy(target, str(src), rjoin(target.remote, dest), rels, flags)

    def copy_from(self, target: Target, src: str, dest: Path, rels: list[str]) -> str | None:
        """Download ``rels`` (relative to ``src`` on the target) into the local ``dest``."""
        dest.mkdir(parents=True, exist_ok=True)
        return self._copy(target, rjoin(target.remote, src), str(dest), rels)


# ---------------------------------------------------------------------------- what to upload
#
# Every uploaded file has a content-addressed name: a new version of a day (a reopened day that became
# final again) or of an extra file gets a new name, and the old one stays. So a file on a target is
# never overwritten, a name that is listed is complete (rclone and the providers publish an object
# only once it is whole), and manifest.json.age, written last, never names a file the target lacks.
# The name is an HMAC keyed with a secret derived from the processor's private key: without it, the
# provider could confirm a guess at a small file (a profile) by hashing it with the public recipients.

def names_key_of(identity) -> bytes:
    """The secret behind remote names, derived from the processor's age identity."""
    return hashlib.sha256(b"hka-backup-names\0" + str(identity).encode()).digest()


def content_key(names_key: bytes, user: str, what: str, sha: str, rkey: str) -> str:
    mac = hmac.new(names_key, f"{user}|{what}|{sha}|{rkey}".encode(), hashlib.sha256).hexdigest()[:16]
    return f"{mac}.{rkey}"


@dataclass
class Checked:
    """What a backup needs, checked before pulling: preflight()."""
    rclone: Rclone
    to: list                          # age recipients
    identity: object                  # the processor's age identity (reads a target's previous index)
    names_key: bytes                  # HMAC key for remote names

    @property
    def recipients_key(self) -> str:
        return sha256_hex("\n".join(sorted(str(r) for r in self.to)).encode())[:16]

    def content_key(self, user: str, what: str, sha: str, rkey: str | None = None) -> str:
        """Part of the remote name, ``<hmac>.<recipients key>``. The HMAC binds the user, the day or
        file, the content and the recipients, so a copy of the file under another user's or day's name
        is not accepted; the recipients key is shown so that files made for earlier recipients can
        still be checked as this processor's own (and re-encrypted)."""
        rkey = self.recipients_key[:8] if rkey is None else rkey
        return content_key(self.names_key, user, what, sha, rkey)

    def remote_name(self, user: str, what: str, sha: str) -> str:
        kind, _, key = what.partition(":")
        if kind == "day":
            return f"archives/{key[:4]}/{key}.{self.content_key(user, what, sha)}.tar.gz.age"
        return f"extras/{key}.{self.content_key(user, what, sha)}.age"


def preflight(store: Store, env: dict | None = None, rclone: Rclone | None = None) -> Checked:
    """Everything a backup needs that can be wrong in the configuration; raises ConfigError."""
    identity = store.identity().age
    return Checked(rclone or Rclone(store), recipients(store, env), identity,
                   names_key_of(identity))


@dataclass
class Item:
    what: str           # "day:YYYY-MM-DD" or "extra:<path under users/<u>/>"
    rel: str            # remote path under users/<user_id>/
    sha256: str         # of the plaintext
    load: object        # () -> plaintext bytes


def archive_pattern(day: str) -> str:
    return rf"archives/{day[:4]}/{day}\.{NAME_KEY}\.tar\.gz\.age"


def extra_pattern(path: str) -> str:
    return rf"extras/{re.escape(path)}\.{NAME_KEY}\.age"


def extras_paths(user_root: Path) -> list[str]:
    out = ["profile.ndjson"] if (user_root / "profile.ndjson").is_file() else []
    for sub in ("undated", "other"):
        d = user_root / sub
        if d.is_dir():
            out += [f"{sub}/{f.name}" for f in sorted(d.iterdir()) if f.is_file() and file_name_ok(f.name)]
    return out


def read_manifest(user_root: Path) -> dict:
    manifest = json.loads((user_root / "manifest.json").read_bytes())
    if not isinstance(manifest, dict) or not isinstance(manifest.get("days", {}), dict):
        raise ValueError("manifest.json has an unexpected shape")
    return manifest


def final_archives(manifest: dict) -> dict[str, str]:
    """{day: archive sha256} of the final days: the only days that are backed up."""
    return {day: e["archive"]["sha256"] for day, e in sorted(manifest.get("days", {}).items())
            if DAY_RE.fullmatch(day) and isinstance(e, dict) and e.get("status") == "final"
            and isinstance(e.get("archive"), dict) and SHA_RE.fullmatch(str(e["archive"].get("sha256")))}


MAX_DOWNLOAD = 512 << 20  # an encrypted archive or extra file is far smaller; never read more into memory


def read_downloaded(path: Path) -> bytes:
    """A file fetched from a target, which anyone with write access could have put there."""
    if path.stat().st_size > MAX_DOWNLOAD:
        raise ValueError(f"{path.name} is larger than {MAX_DOWNLOAD >> 20} MiB")
    return path.read_bytes()


def _verified(path: Path, sha: str, what: str) -> bytes:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise ValueError(f"{what}: missing locally and not yet on this target") from None
    if sha256_hex(data) != sha:
        raise ValueError(f"{what}: changed or damaged since it was hashed; retried next run")
    return data


def wanted(user_root: Path, checked: Checked, cache: dict) -> tuple[list[Item], dict]:
    """The final days' archives and the extra files of one user, and the user's manifest. Nothing is
    read but the manifest and the extra files that changed (``cache``: path -> [size, mtime_ns,
    ctime_ns, inode, sha])."""
    manifest = read_manifest(user_root)
    items = []
    for day, sha in final_archives(manifest).items():
        path = user_root / "archives" / day[:4] / f"{day}.tar.gz"
        items.append(Item(f"day:{day}", checked.remote_name(user_root.name, f"day:{day}", sha), sha,
                          lambda path=path, sha=sha, day=day: _verified(path, sha, f"{day}: archive")))
    paths = extras_paths(user_root)
    for rel in [p for p in cache if p not in paths]:
        cache.pop(rel)
    for rel in paths:
        st = (user_root / rel).stat()
        # ctime cannot be set back (utime moves it too) and build's atomic writes change the inode, so a
        # rewrite within one coarse mtime tick is still noticed.
        stamp = [st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino]
        hit = cache.get(rel)
        if not (isinstance(hit, list) and len(hit) == 5 and hit[:4] == stamp):
            hit = cache[rel] = [*stamp, sha256_hex((user_root / rel).read_bytes())]
        sha = hit[4]
        def load(p=user_root / rel, sha=sha, rel=rel):
            try:
                return _verified(p, sha, rel)
            except ValueError:
                cache.pop(rel, None)  # e.g. rewritten within one mtime tick: hash it again next run
                raise
        items.append(Item(f"extra:{rel}", checked.remote_name(user_root.name, f"extra:{rel}", sha), sha, load))
    return items, manifest


def select(manifest: dict, items: list[Item], files: dict, rkey: str = "") -> dict[str, str]:
    """{what: remote file} the index names: for each day of the manifest and each current extra file,
    the current version if the target holds it, else the newest content it holds (``since``: when that
    content was first uploaded; a re-encrypted copy keeps it), and of copies of that content the one
    for the current recipients (``rkey``)."""
    current = {i.what: i for i in items}
    wanted_whats = {f"day:{d}" for d in manifest.get("days", {}) if DAY_RE.fullmatch(d)} | \
        {w for w in current if w.startswith("extra:")}
    best: dict[str, tuple] = {}
    for rel, f in files.items():
        what = f["what"]
        if what not in wanted_whats:
            continue
        rank = (what in current and current[what].rel == rel, f.get("since") or f.get("uploaded_at") or "",
                name_rkey(rel) == rkey, f.get("uploaded_at") or "", rel)
        if what not in best or rank > best[what][0]:
            best[what] = (rank, rel)
    return {what: rel for what, (_, rel) in best.items()}


def target_index(user: str, manifest: dict, items: list[Item], files: dict, rkey: str = "") -> bytes:
    """manifest.json.age of one target: the user's manifest and, for each day of the manifest and each
    current extra file, the newest version this target holds. A day reopened since keeps its last
    final archive."""
    best = {what: {"file": rel, "sha256": files[rel]["sha256"],
                   "since": files[rel].get("since") or files[rel].get("uploaded_at") or ""}
            for what, rel in select(manifest, items, files, rkey).items()}
    archives = {w[4:]: v for w, v in sorted(best.items()) if w.startswith("day:")}
    extras = {w[6:]: v for w, v in sorted(best.items()) if w.startswith("extra:")}
    return (json.dumps({"format": INDEX_FORMAT, "user_id": user, "archives": archives, "extras": extras,
                        "manifest": manifest}, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode()


def index_entries(index: object, uid: str) -> tuple[dict[str, tuple[str, str]], dict]:
    """{remote file: (what, sha256, since)} named by a target's index, every name checked against the exact
    pattern it must have (age does not authenticate the writer), and the manifest it carries.
    Raises ValueError for an index that is not one; bad entries are left out and reported."""
    if not isinstance(index, dict) or index.get("format") != INDEX_FORMAT or index.get("user_id") != uid \
            or not isinstance(index.get("archives"), dict) or not isinstance(index.get("extras"), dict):
        raise ValueError("not a backup index for this user")
    out, bad = {}, []
    for kind, entries in (("day", index["archives"]), ("extra", index["extras"])):
        for key, entry in sorted(entries.items()):
            ok = DAY_RE.fullmatch(key) if kind == "day" else extra_path_ok(key)
            pattern = archive_pattern(key) if kind == "day" else extra_pattern(key) if ok else None
            if not ok or not isinstance(entry, dict) or not isinstance(entry.get("file"), str) \
                    or not re.fullmatch(pattern, entry["file"]) or not SHA_RE.fullmatch(str(entry.get("sha256"))):
                bad.append(f"{kind} {str(key)[:60]!r}: index entry names an unexpected file")
                continue
            since = entry.get("since") if isinstance(entry.get("since"), str) else ""
            out[entry["file"]] = (f"{kind}:{key}", entry["sha256"], since)
    manifest = index.get("manifest") if isinstance(index.get("manifest"), dict) else {}
    return out, {"manifest": manifest, "bad": bad}


# ---------------------------------------------------------------------------- backup

@dataclass
class BackupReport:
    lines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def state_path(store: Store) -> Path:
    return store.root / "backup" / "state.json"


def _valid_file_entry(f: object) -> bool:
    return isinstance(f, dict) and isinstance(f.get("what"), str) and bool(re.fullmatch(r"(day|extra):.+", f["what"])) \
        and isinstance(f.get("sha256"), str) and bool(SHA_RE.fullmatch(f["sha256"])) \
        and (f.get("uploaded_at") is None or isinstance(f.get("uploaded_at"), str)) \
        and (f.get("since") is None or isinstance(f.get("since"), str))


def load_state(store: Store, repair: bool = False) -> dict:
    """backup/state.json, a cache of what the targets hold (each run lists them and reads a target's
    previous index when it needs to). Damaged entries are dropped; a damaged file is treated as empty,
    and with ``repair`` (a backup, under the lock) moved aside. A file that cannot be read is an error."""
    path = state_path(store)
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except ValueError:
        state = None
    if not (isinstance(state, dict) and isinstance(state.get("users", {}), dict)):
        if repair:
            path.replace(path.with_name(f"state.json.unreadable-{os.getpid()}-{int(time.time())}"))
        return {}
    for user, u in list(state.get("users", {}).items()):
        if not isinstance(u, dict) or not isinstance(u.get("targets", {}), dict):
            state["users"].pop(user)
            continue
        if not isinstance(u.get("extras_cache", {}), dict):
            u["extras_cache"] = {}
        for name, t in list(u.get("targets", {}).items()):
            if not isinstance(t, dict) or not isinstance(t.get("files", {}), dict):
                u["targets"].pop(name)
                continue
            t["files"] = {rel: f for rel, f in t.get("files", {}).items() if _valid_file_entry(f)}
            ignored = t.get("ignored", [])
            t["ignored"] = [r for r in ignored if isinstance(r, str)] if isinstance(ignored, list) else []
            if not isinstance(t.get("index_size"), int) or not isinstance(t.get("index_mtime"), (str, type(None))):
                t["index_size"], t["index_mtime"] = None, None
            failures = t.get("failures", {})
            t["failures"] = {r: n for r, n in failures.items() if isinstance(n, int) and n > 0} \
                if isinstance(failures, dict) else {}
    return state


def users(store: Store) -> list[str]:
    d = store.root / "users"
    return sorted(p.name for p in d.iterdir() if (p / "manifest.json").is_file()) if d.is_dir() else []


def backup(store: Store, targets: dict[str, Target], now: datetime, checked: Checked) -> BackupReport:
    """Upload to ``targets``; ``checked`` comes from preflight(), run before pulling."""
    report = BackupReport()
    if not targets:
        report.lines.append("no backup target configured (see `hka backup --help`)")
        return report
    try:
        state = load_state(store, repair=True)
    except OSError as exc:
        report.errors.append(f"cannot read {state_path(store)}: {exc}")
        return report
    staging_root = store.root / "backup" / "staging"
    try:
        for user in users(store):
            try:
                backup_user(store, user, targets, now, checked, state, staging_root / user, report)
            except Exception as exc:  # one user's broken manifest must not stop the others
                report.errors.append(f"{user}: {exc}")
    finally:
        write_json(state_path(store), state)
        shutil.rmtree(staging_root, ignore_errors=True)
    return report


def name_what(rel: str) -> str | None:
    """What a content-addressed name on a target holds ("day:…" or "extra:…"), or None."""
    m = ARCHIVE_NAME_RE.fullmatch(rel)
    if m and m[1] == m[2][:4]:
        return f"day:{m[2]}"
    m = EXTRA_NAME_RE.fullmatch(rel)
    return f"extra:{m[1]}" if m and extra_path_ok(m[1]) else None


INSTANT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", re.ASCII)


def checked_since(since: object, listed: "Listed") -> str:
    """``since`` as read from an index (which anyone with write access could have written): an hka
    instant, never later than the file's own modification time on the target."""
    if not (isinstance(since, str) and INSTANT_RE.fullmatch(since)):
        return listed.mtime
    return min(since, listed.mtime) if listed.mtime else since


def is_cold(target: Target, rel: str, there: dict) -> bool:
    """The object cannot be downloaded without a restore request: its listed S3 tier says Glacier, or,
    when the backend lists no tier, the target uploads its archives to Glacier."""
    listed = there.get(rel)
    if listed is not None and listed.tier:
        return listed.cold
    return target.cold and rel.startswith("archives/")


def name_key(rel: str) -> tuple[str, str] | None:
    """(hmac, recipients key) of a content-addressed name."""
    m = re.search(r"\.([0-9a-f]{16})\.([0-9a-f]{8})\.(?:tar\.gz\.)?age$", rel)
    return (m[1], m[2]) if m else None


def name_rkey(rel: str) -> str | None:
    key = name_key(rel)
    return key[1] if key else None


def keyed(names_key: bytes, user: str, rel: str, sha: str) -> bool:
    """The name carries the HMAC of this user, day or file, content and recipients key under this
    processor's secret: only this processor can have made it, for any recipients. A planted file, or a
    copy of a real one under another user's or day's name, cannot."""
    key, what = name_key(rel), name_what(rel)
    return key is not None and what is not None \
        and hmac.compare_digest(f"{key[0]}.{key[1]}", content_key(names_key, user, what, sha, key[1]))


def recover_unknown(checked: Checked, target: Target, user: str, there: dict, files: dict, tstate: dict,
                    staging: Path) -> str | None:
    """Names on the target that state.json does not know (it was lost, or a run stopped before saving
    it): learn what they hold, so that the index still names the newest version of every day (a
    reopened day's last final archive). First from the target's previous index; the rest by
    downloading and decrypting them. Files that are not this processor's (planted, copied from another
    name, damaged) are remembered in ``ignored`` and never named. Archives in cold storage are not
    downloaded (skipped this run, not remembered). A file that fails to download is retried and
    reported on every run until it downloads: it may be a day's only final version."""
    ignored = set(tstate.get("ignored", [])) & set(there)
    failures = {r: n for r, n in tstate.get("failures", {}).items() if r in there}
    unknown = [r for r in there if r not in files and r not in ignored and name_what(r)]
    if unknown and MANIFEST in there:
        local = staging / target.name / "previous" / MANIFEST
        local.parent.mkdir(parents=True, exist_ok=True)
        r = checked.rclone.run(target, "copyto", rjoin(target.remote, f"users/{user}/{MANIFEST}"), str(local),
                               timeout=600)
        try:
            entries, _ = index_entries(json.loads(pyrage.decrypt(read_downloaded(local), [checked.identity])), user) \
                if r.returncode == 0 else ({}, None)
        except (OSError, ValueError, pyrage.DecryptError):
            entries = {}
        for rel in unknown:
            if rel in entries and keyed(checked.names_key, user, rel, entries[rel][1]):
                files[rel] = {"what": entries[rel][0], "sha256": entries[rel][1], "uploaded_at": there[rel].mtime,
                              "since": checked_since(entries[rel][2], there[rel])}
        unknown = [r for r in unknown if r not in files]
    # Objects in Glacier cannot be read back without a restore request: skipped (and counted), not
    # remembered, so they are tried again if they come out of Glacier.
    cold = [r for r in unknown if is_cold(target, r, there)]
    unknown = [r for r in unknown if r not in cold]
    tstate["unchecked_cold"] = len(cold)
    err = None
    if unknown:
        folder = staging / target.name / "unknown"
        err = checked.rclone.copy_from(target, f"users/{user}", folder, unknown)
        for rel in unknown:
            local = folder / rel
            if not local.is_file() or local.stat().st_size != there[rel].size:
                failures[rel] = failures.get(rel, 0) + 1  # not (completely) downloaded: tried again next run
                continue
            try:
                sha = sha256_hex(pyrage.decrypt(read_downloaded(local), [checked.identity]))
                if not keyed(checked.names_key, user, rel, sha):
                    raise ValueError("name not made by this processor for this user and day")
                files[rel] = {"what": name_what(rel), "uploaded_at": there[rel].mtime, "sha256": sha,
                              "since": there[rel].mtime}
            except (OSError, ValueError, pyrage.DecryptError):
                ignored.add(rel)  # complete, but not this processor's (planted, copied, damaged): never named
    failures = {r: n for r, n in failures.items() if r in unknown and r not in files and r not in ignored}
    tstate["ignored"], tstate["failures"] = sorted(ignored), failures
    if failures:
        return f"cannot read {len(failures)} unknown file(s) on the target{f': {err}' if err else ''}; retried next run"
    return None


def rekey(checked: Checked, target: Target, user: str, manifest: dict, items: list[Item], files: dict,
          there: dict, tstate: dict, folder: Path, now: datetime, report: BackupReport) -> list[str]:
    """Versions the index would name that were encrypted for earlier recipients (a reopened day's last
    final archive, after HKA_BACKUP_RECIPIENTS changed): download, decrypt, check, and upload them
    again for the current recipients, so every named file opens with every current key. Archives in
    cold storage cannot be read back: they are reported. Returns errors (retried next run)."""
    rkey = checked.recipients_key[:8]
    stale = {}
    for what, rel in select(manifest, items, files, rkey).items():
        if name_rkey(rel) == rkey:
            continue
        new = checked.remote_name(user, what, files[rel]["sha256"])
        if new in there and new not in tstate.get("ignored", []):
            # Already on the target (e.g. in Glacier, unknown to the state): never overwrite. The keyed
            # name proves it is this processor's copy of this content: adopt it.
            files[new] = {"what": what, "sha256": files[rel]["sha256"], "uploaded_at": there[new].mtime,
                          "since": files[rel].get("since") or files[rel].get("uploaded_at")}
        elif new not in files:  # absent, or found unreadable there (``ignored``): written (again)
            stale[what] = rel
    if not stale:
        return []
    cold = [rel for rel in stale.values() if is_cold(target, rel, there)]
    if cold:
        report.lines.append(f"{user} -> {target.name}: {len(cold)} older archive(s) in cold storage are readable "
                            "only with the recipients of their time")
    stale = {w: r for w, r in stale.items() if r not in cold}
    if not stale:
        return []
    err = checked.rclone.copy_from(target, f"users/{user}", folder / "in", sorted(stale.values()))
    errors = [err] if err else []
    out, sent = folder / "out", []
    for what, rel in stale.items():
        try:
            plain = pyrage.decrypt(read_downloaded(folder / "in" / rel), [checked.identity])
            if sha256_hex(plain) != files[rel]["sha256"]:
                raise ValueError(f"{rel}: content differs from what was uploaded")
            new = checked.remote_name(user, what, files[rel]["sha256"])
            atomic_write(out / new, pyrage.encrypt(plain, checked.to))
            sent.append((what, new, files[rel]["sha256"], files[rel].get("since") or files[rel].get("uploaded_at")))
        except (OSError, ValueError, pyrage.DecryptError) as exc:
            errors.append(f"cannot re-encrypt {rel}: {exc}")
    for group, flags in (([s for s in sent if s[0].startswith("day:")], target.archive_flags),
                         ([s for s in sent if s[0].startswith("extra:")], [])):
        if group:
            err = checked.rclone.copy(target, out, f"users/{user}", [s[1] for s in group], flags)
            if err:
                errors.append(err)
                continue
            for what, new, sha, since in group:  # the same content: it keeps its age
                files[new] = {"what": what, "sha256": sha, "uploaded_at": instant(now), "since": since}
            tstate["ignored"] = [r for r in tstate.get("ignored", []) if r not in {s[1] for s in group}]
    return errors


def index_replaced(tstate: dict, listed: "Listed | None") -> bool:
    """The manifest.json.age listed on the target is not the one this processor last uploaded (its
    size, or its modification time as first seen after the upload, differs): someone else wrote it."""
    if listed is None or tstate.get("index_size") is None:
        return False
    if listed.size != tstate["index_size"]:
        return True
    if tstate.get("index_mtime") is None:
        tstate["index_mtime"] = listed.mtime
        return False
    return listed.mtime != tstate["index_mtime"]


def mark_current_newest(items: list[Item], files: dict, now: datetime) -> None:
    """A day whose content went back to an earlier version (final as A, then B, then A again) already
    has A on the target with an older ``since`` than B: A is the newest content again from now."""
    newest: dict[str, list[tuple[str, str]]] = {}
    for f in files.values():
        newest.setdefault(f["what"], []).append((f.get("since") or f.get("uploaded_at") or "", f["sha256"]))
    for i in items:
        held = files.get(i.rel)
        if held is None:
            continue
        other = max((since for since, sha in newest.get(i.what, []) if sha != i.sha256), default=None)
        if other is not None and other >= (held.get("since") or held.get("uploaded_at") or ""):
            held["since"] = instant(now)


def backup_user(store: Store, user: str, targets: dict[str, Target], now: datetime, checked: Checked,
                state: dict, staging: Path, report: BackupReport) -> None:
    rclone, to = checked.rclone, checked.to
    ustate = state.setdefault("users", {}).setdefault(user, {})
    items, manifest = wanted(store.user_dir(user), checked, ustate.setdefault("extras_cache", {}))
    tstates = ustate.setdefault("targets", {})
    shutil.rmtree(staging, ignore_errors=True)
    plan: dict[str, list[Item]] = {}
    listings: dict[str, dict] = {}
    recover_errors: dict[str, str | None] = {}  # reported with the target's other errors; retried
    for name, target in targets.items():
        tstate = tstates.setdefault(name, {})
        tstate["checked_at"] = instant(now)
        files = tstate.setdefault("files", {})
        try:
            there = rclone.listing(target, f"users/{user}")
        except Exception as exc:
            tstate["error"] = f"cannot read the target: {exc}"
            report.errors.append(f"{user} -> {name}: {tstate['error']}")
            continue
        for rel in [r for r in files if r not in there]:
            files.pop(rel)  # deleted on the target: no longer counts
        rejected = set(tstate.get("ignored", []))
        for i in items:  # the keyed, content-addressed name is the proof that this processor made it
            if i.rel in there and i.rel not in files and i.rel not in rejected:
                files[i.rel] = {"what": i.what, "sha256": i.sha256, "uploaded_at": there[i.rel].mtime,
                                "since": there[i.rel].mtime}
        recover_errors[name] = recover_unknown(checked, target, user, there, files, tstate, staging)
        mark_current_newest(items, files, now)
        if tstate.get("unchecked_cold"):
            report.lines.append(f"{user} -> {name}: {tstate['unchecked_cold']} file(s) in Glacier are not known to "
                                "this processor's state and cannot be checked without an S3 restore request; the "
                                "index names only what it can check")
        tstate["index_present"] = MANIFEST in there
        if index_replaced(tstate, there.get(MANIFEST)):
            tstate["index_present"] = False  # uploaded again below
            report.lines.append(f"{user} -> {name}: {MANIFEST} on the target is not the one this processor "
                                "uploaded; uploading it again")
        plan[name] = [i for i in items if i.rel not in files]
        listings[name] = there

    encrypted: dict[str, Item] = {}
    failed: dict[str, str] = {}
    for i in {i.rel: i for items_ in plan.values() for i in items_}.values():
        try:
            atomic_write(staging / "data" / i.rel, pyrage.encrypt(i.load(), to))
            encrypted[i.rel] = i
        except Exception as exc:
            failed[i.rel] = str(exc)
            report.errors.append(f"{user}: {exc}")

    for name, pending in plan.items():
        target, tstate = targets[name], tstates[name]
        files = tstate["files"]
        send = [i for i in pending if i.rel in encrypted]
        errors = [recover_errors[name]] if recover_errors.get(name) else []
        archives = [i.rel for i in send if i.what.startswith("day:")]
        extras = [i.rel for i in send if i.what.startswith("extra:")]
        copy_errors = []
        for group, flags in ((archives, target.archive_flags), (extras, [])):
            if group:  # the storage class applies to day archives only; restore needs the rest at once
                err = rclone.copy(target, staging / "data", f"users/{user}", group, flags)
                copy_errors += [err] if err else []
        if copy_errors:  # rclone stopped part way: a name that is listed now is complete
            try:
                arrived = set(rclone.listing(target, f"users/{user}"))
            except Exception as exc:
                arrived, copy_errors = set(), copy_errors + [f"cannot read the target: {exc}"]
        else:
            arrived = {i.rel for i in send}
        errors += copy_errors
        done = [i for i in send if i.rel in arrived]
        for i in done:
            files[i.rel] = {"what": i.what, "sha256": i.sha256, "uploaded_at": instant(now), "since": instant(now)}
        if done:  # a name found unreadable on the target has now been written again with the right content
            tstate["ignored"] = [r for r in tstate.get("ignored", []) if r not in {i.rel for i in done}]

        errors += rekey(checked, target, user, manifest, items, files, listings[name], tstate,
                        staging / name / "rekey", now, report)
        sent_index = False
        index = target_index(user, manifest, items, files, checked.recipients_key[:8])  # last: it names only what this target holds
        index_id = f"{sha256_hex(index)}.{checked.recipients_key[:8]}"  # new recipients: send it again
        if tstate.get("index_sha256") != index_id or not tstate.get("index_present"):
            ciphertext = pyrage.encrypt(index, to)
            atomic_write(staging / name / MANIFEST, ciphertext)
            err = rclone.copy(target, staging / name, f"users/{user}", [MANIFEST])
            if err:
                errors.append(err)
            else:
                tstate["index_sha256"], tstate["index_present"], sent_index = index_id, True, True
                tstate["index_size"], tstate["index_mtime"] = len(ciphertext), None  # mtime: at the next listing
        missing = [i for i in pending if i.rel not in files]
        tstate["pending"] = len(missing)
        if done or sent_index:
            tstate["uploaded_at"] = instant(now)
            n = sum(1 for i in done if i.what.startswith("day:"))
            x = len(done) - n
            report.lines.append(f"{user} -> {name}: uploaded {n} day archive(s)"
                                f"{f', {x} extra file(s)' if x else ''}{', manifest' if sent_index else ''}")
        if errors or missing:
            detail = errors[0] if errors else failed.get(missing[0].rel, "")
            tstate["error"] = (f"{len(missing)} file(s) not uploaded; " if missing else "") + "retried next run" \
                + (f" ({detail})" if detail else "")
            report.errors.append(f"{user} -> {name}: {tstate['error']}")
        else:
            tstate.pop("error", None)
    shutil.rmtree(staging, ignore_errors=True)


def status(store: Store) -> dict:
    """Per user and target, from backup/state.json and the manifests. No network, no key, reads no
    health data and changes nothing."""
    try:
        state = load_state(store)
    except OSError as exc:
        return {"error": f"cannot read {state_path(store)}: {exc}"}
    out = {}
    for user in users(store):
        try:
            finals = final_archives(read_manifest(store.user_dir(user)))
        except Exception as exc:
            out[user] = {"error": f"cannot read manifest.json: {exc}"}
            continue
        per = {}
        for name, t in state.get("users", {}).get(user, {}).get("targets", {}).items():
            held = {(f["what"], f["sha256"]) for f in t.get("files", {}).values()}
            uploaded = sum(1 for day, sha in finals.items() if (f"day:{day}", sha) in held)
            per[name] = {
                "archives_uploaded": uploaded, "archives_final": len(finals),
                "up_to_date": uploaded == len(finals) and t.get("pending") == 0 and not t.get("error")
                and bool(t.get("index_present")),
                "checked_at": t.get("checked_at"), "uploaded_at": t.get("uploaded_at"), "error": t.get("error"),
                "unchecked_cold": t.get("unchecked_cold", 0),
            }
        out[user] = per
    return out


# ---------------------------------------------------------------------------- restore


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _day_members(data: bytes, day: str) -> list[tuple[str, bytes]]:
    """The files of a day archive: regular files directly under ``day/``, unique, bounded in size."""
    try:  # inflate with a bound first: tarfile itself would read any header (PAX) into memory
        plain = gzip.GzipFile(fileobj=io.BytesIO(data)).read(MAX_ARCHIVE_PLAINTEXT + 1)
    except (OSError, EOFError) as exc:
        raise ValueError(f"not a gzip stream: {exc}") from None
    if len(plain) > MAX_ARCHIVE_PLAINTEXT:
        raise ValueError("archive larger than 256 MiB")
    out, seen = [], set()
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:") as tar:
        for m in tar:
            folder, _, fname = m.name.partition("/")
            if not m.isfile() or folder != day or not file_name_ok(fname) or fname in seen:
                raise ValueError(f"unexpected tar member {m.name!r}")
            seen.add(fname)
            out.append((fname, tar.extractfile(m).read()))
    return out


def restore(store: Store, target: Target, out: Path, identity_file: Path | None, user: str | None,
            rclone: Rclone | None = None) -> BackupReport:
    """Download and decrypt a target into ``out``/<user_id>/: days/, archives/, the extra files and
    restored.json (what was restored, and the manifest as it was at the last backup). Needs no lock:
    it writes only to ``out`` and a staging folder of its own."""
    report = BackupReport()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ConfigError(f"{out} is not an empty folder; restore into a new folder")
    identity = read_age_identity(identity_file) if identity_file else store.identity().age
    # With the processor's own key every name in the index can be checked as made by this processor.
    # Another key (the offline recovery identity) cannot derive that secret: the index is then trusted
    # as it is, and age does not prove who wrote it.
    try:
        own = str(store.identity().age.to_public()) == str(identity.to_public())
    except ConfigError:
        own = False
    names_key = names_key_of(identity) if own else None
    if names_key is None:
        report.lines.append("note: restoring with a key other than this processor's: file names cannot be "
                            "verified, only each file's sha256 against the backup's own manifest")
    rclone = rclone or Rclone(store)
    if user:
        user_ids = [user]
    else:
        r = rclone.run(target, "lsf", "--dirs-only", rjoin(target.remote, "users"), timeout=600)
        if r.returncode != 0:
            raise BackupError(f"cannot list users on {target.name}: {Rclone.error(r)}")
        user_ids = [ln.strip().rstrip("/") for ln in r.stdout.splitlines() if ln.strip()]
    (store.root / "backup").mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix="restore-", dir=store.root / "backup"))
    try:
        for uid in user_ids:
            if not SAFE_NAME.fullmatch(uid):
                report.errors.append(f"{uid!r}: not a user folder name; skipped")
                continue
            try:
                restore_user(rclone, target, identity, names_key, uid, staging_root / uid, out / uid, report)
            except Exception as exc:  # one user must not stop the others
                report.errors.append(f"{uid}: restore failed: {exc}")
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return report


def restore_user(rclone: Rclone, target: Target, identity, names_key: bytes | None, uid: str, staging: Path,
                 dest: Path, report: BackupReport) -> None:
    staging.mkdir(parents=True)
    remote = rjoin(target.remote, f"users/{uid}")
    r = rclone.run(target, "copyto", f"{remote}/{MANIFEST}", str(staging / MANIFEST), timeout=600)
    try:
        if r.returncode != 0:
            raise BackupError(Rclone.error(r))
        entries, extra = index_entries(json.loads(pyrage.decrypt(read_downloaded(staging / MANIFEST), [identity])), uid)
    except (OSError, ValueError, BackupError, pyrage.DecryptError) as exc:
        report.errors.append(f"{uid}: cannot read {MANIFEST}: {exc}")
        return
    report.errors += [f"{uid} {b}" for b in extra["bad"]]
    if names_key is not None:
        for rel in [r for r, (_, sha, _) in entries.items() if not keyed(names_key, uid, r, sha)]:
            entries.pop(rel)
            report.errors.append(f"{uid} {rel[:80]!r}: not made by this processor for this user; not restored")
    if entries:  # only what the index names: never the older versions, which may be in cold storage
        err = rclone.copy_from(target, f"users/{uid}", staging / "files", sorted(entries))
        if err:
            report.errors.append(f"{uid}: download incomplete: {err}")
    manifest = extra["manifest"]
    restored: dict = {"user_id": uid, "restored_from": target.name, "days": {}, "extras": {},
                      "manifest_at_backup": manifest}
    for rel, (what, sha, _) in sorted(entries.items()):
        kind, _, key = what.partition(":")
        partial = None
        try:
            data = pyrage.decrypt(read_downloaded(staging / "files" / rel), [identity])
            if sha256_hex(data) != sha:
                raise ValueError("sha256 differs from the backup's manifest")
            if kind == "extra":
                _write_new(dest / key, data)
                restored["extras"][key] = sha
                continue
            members = _day_members(data, key)
            final = dest / "days" / key[:4] / key
            if final.exists():
                raise ValueError(f"{final} exists")
            partial = final.with_name(f".{key}.partial")
            partial.mkdir(parents=True)
            for fname, content in members:  # the day appears whole, or not at all
                _write_new(partial / fname, content)
            _write_new(dest / "archives" / key[:4] / f"{key}.tar.gz", data)
            os.rename(partial, final)
            partial = None
            restored["days"][key] = sha
        except (OSError, ValueError, tarfile.TarError, pyrage.DecryptError) as exc:
            report.errors.append(f"{uid} {key[:60]!r}: {exc}")
            if partial is not None:
                shutil.rmtree(partial, ignore_errors=True)
                (dest / "archives" / key[:4] / f"{key}.tar.gz").unlink(missing_ok=True)
    _write_new(dest / "restored.json",
               (json.dumps(restored, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode())
    days = manifest.get("days", {}) if isinstance(manifest.get("days"), dict) else {}
    missing = sum(1 for d, e in days.items()
                  if isinstance(e, dict) and e.get("status") == "final" and d not in restored["days"])
    report.lines.append(f"{uid}: restored {len(restored['days'])} final day(s) and {len(restored['extras'])} "
                        f"extra file(s) into {dest}"
                        + (f"; {missing} final day(s) of its manifest are not in the backup" if missing else ""))
