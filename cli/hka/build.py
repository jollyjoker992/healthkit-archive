"""`hka build`: decrypt verified batches and merge them into per-user day folders.

Layout per user (users/<user_id>/):
  manifest.json                 day status (provisional/final), hashes, archives
  index.sqlite                  uuid -> day/file, tombstones, census, progress (rebuildable from inbox/)
  days/YYYY/YYYY-MM-DD/<HealthKit type>.ndjson, summary.json, _day.json
  undated/<type>.ndjson         records without a usable start instant
  archives/YYYY/YYYY-MM-DD.tar.gz   reproducible archive of a final day
  profile.ndjson, other/<device>-<seq>.ndjson

Records are written with the exact bytes the device sent. A batch is applied in this order:
day files are rewritten (merging by (uuid, part), so a replay never duplicates), then one
SQLite transaction records the index rows, the batch as built, and the touched days as dirty;
dirty days are refreshed (summary, _day.json, manifest) and cleared. A crash anywhere leaves
either a replayable batch or dirty days that the next build refreshes first.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import sqlite3
import tarfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pyrage

from hkacommon.encoding import FormatError, is_int, parse_instant, parse_json_object, sha256_hex
from . import days as D
from .pull import batch_paths, load_manifest_file
from .store import ConfigError, Identity, Store, atomic_write, instant, read_json, write_json

MAX_PLAINTEXT = 256 * 1024 * 1024
SETTLE = timedelta(days=7)
# The app re-sends the last 48 hours every day: a shorter period would finalize days that the next
# re-send reopens (and re-archives, re-uploads).
SETTLE_DAYS_RANGE = (3, 365)


def settle_period(env: dict | None = None) -> timedelta:
    """How long a day stays untouched before it becomes final: HKA_SETTLE_DAYS, default 7."""
    value = (os.environ if env is None else env).get("HKA_SETTLE_DAYS", "").strip()
    if not value:
        return SETTLE
    lo, hi = SETTLE_DAYS_RANGE
    if not (value.isascii() and value.isdigit()) or not lo <= int(value) <= hi:
        raise ConfigError(f"HKA_SETTLE_DAYS must be a whole number of days from {lo} to {hi}, not {value!r}")
    return timedelta(days=int(value))
MANIFEST_SCHEMA = 1
NO_IDENTITY_KINDS = {"header", "deleted", "profile", "census"}
UNDATED = "undated"

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (uuid TEXT, part INTEGER, kind TEXT, type TEXT, start TEXT, day TEXT, file TEXT,
                                    parent TEXT, device_id TEXT, seq INTEGER, PRIMARY KEY (uuid, part));
CREATE INDEX IF NOT EXISTS records_by_type_day ON records (type, day);
CREATE INDEX IF NOT EXISTS records_by_parent ON records (parent);
CREATE TABLE IF NOT EXISTS tombstones (uuid TEXT PRIMARY KEY, type TEXT, observed_at TEXT, device_id TEXT, seq INTEGER);
CREATE TABLE IF NOT EXISTS census (device_id TEXT, type TEXT, day TEXT, tz TEXT, count INTEGER, uuid_sha256 TEXT,
                                   seq INTEGER, PRIMARY KEY (device_id, type, day));
CREATE TABLE IF NOT EXISTS built (device_id TEXT PRIMARY KEY, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS dirty_days (day TEXT PRIMARY KEY);
"""


class BatchError(Exception):
    pass


@dataclass
class Line:
    rec: dict
    raw: bytes


def decrypt_batch(ciphertext: bytes, identity: Identity) -> list[Line]:
    try:
        plain = pyrage.decrypt(ciphertext, [identity.age])
    except pyrage.DecryptError as exc:
        raise BatchError(f"cannot decrypt: {exc}") from exc
    try:
        data = gzip.GzipFile(fileobj=io.BytesIO(plain)).read(MAX_PLAINTEXT + 1)
    except (OSError, EOFError) as exc:
        raise BatchError(f"not a gzip stream: {exc}") from exc
    if len(data) > MAX_PLAINTEXT:
        raise BatchError("decompressed batch larger than 256 MiB")
    if data and not data.endswith(b"\n"):
        raise BatchError("NDJSON does not end with a newline")
    out = []
    for raw in data.split(b"\n")[:-1]:
        try:
            out.append(Line(parse_json_object(raw), raw))
        except FormatError as exc:
            raise BatchError(f"bad record: {exc}") from exc
    return out


def read_lines(path: Path) -> list[bytes]:
    """Lines of an NDJSON file we wrote: split on \n only (a record may contain a raw \r)."""
    if not path.exists():
        return []
    data = path.read_bytes()
    return data.split(b"\n")[:-1] if data.endswith(b"\n") else data.split(b"\n")


def record_key(rec: dict) -> tuple[str, int]:
    part = rec.get("part", 0)
    return rec["uuid"], part if is_int(part) else 0


def has_identity(rec: dict) -> bool:
    return isinstance(rec.get("uuid"), str) and rec.get("k") not in NO_IDENTITY_KINDS


@dataclass
class Changes:
    add: dict[tuple[str, str], list[Line]] = field(default_factory=lambda: defaultdict(list))
    remove: dict[tuple[str, str], set[str]] = field(default_factory=lambda: defaultdict(set))
    index_rows: list[tuple] = field(default_factory=list)
    moved: list[tuple[str, int, str]] = field(default_factory=list)   # (uuid, part, new day)
    tombstones: list[tuple] = field(default_factory=list)
    census: list[tuple] = field(default_factory=list)
    profiles: list[bytes] = field(default_factory=list)
    other: list[bytes] = field(default_factory=list)
    tz: str | None = None


class UserBuild:
    def __init__(self, store: Store, user_id: str, now: datetime):
        self.store, self.user_id, self.now = store, user_id, now
        self.root = store.user_dir(user_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "index.sqlite")
        self.db.executescript(SCHEMA)
        # A new archive starts on the current day rules; day folders without a manifest may predate them.
        self.manifest = read_json(self.root / "manifest.json") or {
            "schema_version": MANIFEST_SCHEMA, "days": {},
            **({} if (self.root / "days").is_dir() else {"day_rules": D.DAY_RULES})}
        self.refresh_errors: list[str] = []
        if self.manifest.get("schema_version") != MANIFEST_SCHEMA:
            raise BatchError(f"{self.root / 'manifest.json'}: unsupported schema_version")
        rules = self.manifest.get("day_rules", 1)
        if type(rules) is not int or rules < 1 or rules > D.DAY_RULES:
            raise BatchError(f"{self.root / 'manifest.json'}: day_rules {rules!r} is not a day-rules version this hka understands")

    def close(self) -> None:
        self.db.close()

    def built_seq(self, device_id: str) -> int:
        row = self.db.execute("SELECT seq FROM built WHERE device_id = ?", (device_id,)).fetchone()
        return row[0] if row else 0

    def mark_built(self, device_id: str, seq: int) -> None:
        with self.db:
            self.db.execute("INSERT INTO built VALUES (?, ?) ON CONFLICT (device_id) DO UPDATE SET seq = excluded.seq",
                            (device_id, seq))

    def day_dir(self, day: str) -> Path:
        return self.root / UNDATED if day == UNDATED else self.root / "days" / day[:4] / day

    # ------------------------------------------------------------------ plan one batch
    def plan(self, device_id: str, seq: int, lines: list[Line]) -> Changes:
        ch = Changes()
        header = lines[0].rec if lines else None
        if not header or header.get("k") != "header" or not (is_int(header.get("v")) and header["v"] == 1):
            raise BatchError("first record is not a version 1 header")
        if header.get("user_id") != self.user_id or header.get("device_id") != device_id:
            raise BatchError("header names another user or device than the manifest")
        batch_tz = header.get("tz") if isinstance(header.get("tz"), str) else None
        ch.tz = batch_tz

        seen: set[tuple[str, int]] = set()
        workout_days: dict[str, str] = {}

        def known(key: tuple[str, int]) -> bool:
            if key in seen:
                return True
            if self.db.execute("SELECT 1 FROM tombstones WHERE uuid = ?", (key[0],)).fetchone():
                return True
            return self.db.execute("SELECT 1 FROM records WHERE uuid = ? AND part = ?", key).fetchone() is not None

        def workout_day(uuid) -> str | None:
            if not isinstance(uuid, str):
                return None
            if uuid in workout_days:
                return workout_days[uuid]
            row = self.db.execute("SELECT day FROM records WHERE uuid = ? AND part = 0 AND kind = 'workout'",
                                  (uuid,)).fetchone()
            return row[0] if row and row[0] != UNDATED else None

        deleted = {ln.rec["uuid"] for ln in lines if ln.rec.get("k") == "deleted" and isinstance(ln.rec.get("uuid"), str)}
        # Routes of a workout deleted in this same batch are deleted too.
        deleted |= {ln.rec["uuid"] for ln in lines if ln.rec.get("k") == "route" and isinstance(ln.rec.get("uuid"), str)
                    and ln.rec.get("workout_uuid") in deleted}
        ordered = sorted(lines[1:], key=lambda ln: ln.rec.get("k") == "route")  # workouts before their routes
        for ln in ordered:
            rec, kind = ln.rec, ln.rec.get("k")
            if has_identity(rec):
                key = record_key(rec)
                if rec["uuid"] in deleted or known(key):
                    continue
                seen.add(key)
                day = D.assign_day(rec, batch_tz, workout_day) or UNDATED
                if kind == "workout" and day != UNDATED:
                    workout_days[rec["uuid"]] = day
                    self.plan_route_moves(rec["uuid"], day, ch, deleted)
                fname = D.file_name(rec)
                parent = rec.get("workout_uuid") if kind == "route" and isinstance(rec.get("workout_uuid"), str) else None
                ch.add[(day, fname)].append(ln)
                start = rec.get("start") if isinstance(rec.get("start"), str) else None
                ch.index_rows.append((key[0], key[1], kind, rec.get("type") if isinstance(rec.get("type"), str) else None,
                                      start, day, fname, parent, device_id, seq))
            elif kind == "deleted" and isinstance(rec.get("uuid"), str):
                # Deleting a workout deletes its routes too (HealthKit removes associated routes).
                children = [u for (u,) in self.db.execute("SELECT DISTINCT uuid FROM records WHERE parent = ?", (rec["uuid"],))]
                for uuid in [rec["uuid"], *children]:
                    for day, fname in self.db.execute("SELECT day, file FROM records WHERE uuid = ?", (uuid,)):
                        ch.remove[(day, fname)].add(uuid)
                    ch.tombstones.append((uuid, rec.get("type") if uuid == rec["uuid"] else "HKWorkoutRoute",
                                          rec.get("observed_at"), device_id, seq))
            elif kind == "profile":
                ch.profiles.append(ln.raw)
            elif kind == "census" and isinstance(rec.get("type"), str) and isinstance(rec.get("day"), str):
                ch.census.append((device_id, rec["type"], rec["day"], rec.get("tz"), rec.get("count"),
                                  rec.get("uuid_sha256"), seq))
            elif kind == "header":
                raise BatchError("more than one header record")
            else:
                ch.other.append(ln.raw)
        return ch

    def plan_route_moves(self, workout_uuid: str, day: str, ch: Changes, deleted: set[str]) -> None:
        """Routes that arrived before their workout were filed by their own start: move them."""
        rows = self.db.execute("SELECT uuid, part, day, file FROM records WHERE parent = ? AND day != ?",
                               (workout_uuid, day)).fetchall()
        for uuid, part, old_day, fname in rows:
            if uuid in deleted:
                continue  # its tombstone removes it where it is
            raw = self.read_line(old_day, fname, (uuid, part))
            if raw is not None:
                ch.remove[(old_day, fname)].add(uuid)
                ch.add[(day, fname)].append(Line(json.loads(raw), raw))
                ch.moved.append((uuid, part, day))
            elif self.read_line(day, fname, (uuid, part)) is not None:
                ch.moved.append((uuid, part, day))  # moved by an interrupted run whose index update was lost

    # ------------------------------------------------------------------ day-rule migration
    def migrate_day_rules(self, identity: Identity | None = None) -> tuple[int, int]:
        """Move sleep records filed under day rules 1 (each on the calendar date it ends, which split nights at
        midnight) to the sleep day the current rule gives, then record the version.

        Works from the day files, not the index, so it is also right after the index was rebuilt from the
        inbox. Each record's zone is the one version 1 used: the zone in the header of the batch it arrived in
        (the index names the batch), or, when that batch cannot be read, the zone that first filled its day.
        A record moves only when that zone reproduces where version 1 put it; one that does not stays where
        it is and is counted as unplaced, never moved somewhere arbitrary.

        Crash-safe like `apply`: additions are written before removals, then the index and dirty days are
        updated in one transaction, and `day_rules` is saved last. A rerun scans the files again: a record found
        in two days keeps the copy on its sleep day, and an index row that disagrees with the files is corrected.
        Returns (records moved, records left unplaced).
        """
        if self.manifest.get("day_rules", 1) >= D.DAY_RULES:
            return 0, 0
        fname = D.file_name({"type": D.SLEEP})
        found: dict[tuple[str, int], list[tuple[str, bytes]]] = defaultdict(list)
        days_dir = self.root / "days"
        for path in sorted(days_dir.glob(f"*/*/{fname}")) if days_dir.is_dir() else []:
            for raw in read_lines(path):
                found[record_key(json.loads(raw))].append((path.parent.name, raw))
        index = {(u, p): (d, dev, seq) for u, p, d, dev, seq in self.db.execute(
            "SELECT uuid, part, day, device_id, seq FROM records WHERE type = ? AND day != ?", (D.SLEEP, UNDATED))}
        zones: dict[tuple[str, int], object] = {}
        unknown = object()

        def batch_zone(device_id, seq):
            if identity is None or not isinstance(device_id, str) or not is_int(seq):
                return unknown
            if (device_id, seq) not in zones:
                try:
                    _, cpath = batch_paths(self.store.device_dir(self.user_id, device_id), seq)
                    header = decrypt_batch(cpath.read_bytes(), identity)[0].rec
                    zones[(device_id, seq)] = header.get("tz") if isinstance(header.get("tz"), str) else None
                except Exception:
                    zones[(device_id, seq)] = unknown
            return zones[(device_id, seq)]

        adds: dict[str, list[Line]] = defaultdict(list)
        removes: dict[str, set[str]] = defaultdict(set)
        index_fix: list[tuple[str, int, str]] = []
        touched: set[str] = set()
        new_day_tz: dict[str, str] = {}
        moved = unplaced = 0
        for key, copies in sorted(found.items()):
            days = sorted({day for day, _ in copies})
            raw = copies[0][1]
            rec = json.loads(raw)
            row = index.get(key)
            tz = batch_zone(row[1], row[2]) if row else unknown
            if tz is unknown:
                tz = self.manifest["days"].get(row[0] if row else days[0], {}).get("tz")
            new_day = D.assign_day(rec, tz, lambda _uuid: None)
            rtz = D.record_tz(rec, tz)
            old_day = D.local_day(rec.get("end"), rtz) or D.local_day(rec.get("start"), rtz)  # rules 1
            if new_day and (new_day in days or old_day in days):
                target = new_day
            else:
                target = row[0] if row and row[0] in days else days[0]
                unplaced += new_day is not None and target != new_day
            if target not in days:
                adds[target].append(Line(rec, raw))
                if isinstance(tz, str):
                    new_day_tz.setdefault(target, tz)
            for day in days:
                if day != target:
                    removes[day].add(key[0])
            if days != [target]:
                moved += 1
                touched |= set(days) | {target}
            if row and row[0] != target:
                index_fix.append((key[0], key[1], target))
                touched |= {row[0], target}
        for day, add in sorted(adds.items()):
            self.merge_file(day, fname, add, set())
        for day, remove in sorted(removes.items()):
            self.merge_file(day, fname, [], remove)
        for day, tz in new_day_tz.items():  # saved before the index points at a new day, so a rerun sees its zone
            self.manifest["days"].setdefault(day, {}).setdefault("tz", tz)
        self.save()
        self.commit_moves(index_fix, touched)
        self.refresh_dirty()
        self.manifest["day_rules"] = D.DAY_RULES
        self.save()
        return moved, unplaced

    def commit_moves(self, moved: list[tuple[str, int, str]], touched: set[str]) -> None:
        """Point the index at each record's new day and mark every day involved dirty, in one transaction."""
        with self.db:
            self.db.executemany("UPDATE records SET day = ? WHERE uuid = ? AND part = ?",
                                [(day, uuid, part) for uuid, part, day in moved])
            self.db.executemany("INSERT OR IGNORE INTO dirty_days VALUES (?)", [(d,) for d in sorted(touched)])

    def read_line(self, day: str, fname: str, key: tuple[str, int]) -> bytes | None:
        for raw in read_lines(self.day_dir(day) / fname):
            if record_key(json.loads(raw)) == key:
                return raw
        return None

    # ------------------------------------------------------------------ apply
    def apply(self, device_id: str, seq: int, ch: Changes) -> set[str]:
        touched = {day for day, _ in ch.add} | {day for day, _ in ch.remove}
        for day, fname in sorted(set(ch.add) | set(ch.remove)):
            self.merge_file(day, fname, ch.add.get((day, fname), []), ch.remove.get((day, fname), set()))
        if ch.profiles:
            self.append_unique(self.root / "profile.ndjson", ch.profiles)
        if ch.other:
            atomic_write(self.root / "other" / f"{device_id}-{seq:010d}.ndjson", b"".join(r + b"\n" for r in ch.other))
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO records VALUES (?,?,?,?,?,?,?,?,?,?)", ch.index_rows)
            self.db.executemany("UPDATE records SET day = ? WHERE uuid = ? AND part = ?",
                                [(day, uuid, part) for uuid, part, day in ch.moved])
            for uuid, *_ in ch.tombstones:
                self.db.execute("DELETE FROM records WHERE uuid = ?", (uuid,))
            self.db.executemany("INSERT OR IGNORE INTO tombstones VALUES (?,?,?,?,?)", ch.tombstones)
            self.db.executemany("INSERT OR REPLACE INTO census VALUES (?,?,?,?,?,?,?)", ch.census)
            self.db.executemany("INSERT OR IGNORE INTO dirty_days VALUES (?)", [(d,) for d in touched if d != UNDATED])
            self.db.execute("INSERT INTO built VALUES (?, ?) ON CONFLICT (device_id) DO UPDATE SET seq = excluded.seq",
                            (device_id, seq))
        if ch.tz:
            for day in touched - {UNDATED}:
                # The zone that first filled this day; later batches from another zone do not move it.
                self.manifest["days"].setdefault(day, {}).setdefault("tz", ch.tz)
        self.refresh_dirty()
        return touched - {UNDATED}

    def merge_file(self, day: str, fname: str, add: list[Line], remove: set[str]) -> None:
        path = self.day_dir(day) / fname
        lines: dict[tuple[str, int], tuple[tuple, bytes]] = {}
        for raw in read_lines(path):
            rec = json.loads(raw)
            lines[record_key(rec)] = (D.sort_key(rec), raw)
        for ln in add:
            lines.setdefault(record_key(ln.rec), (D.sort_key(ln.rec), ln.raw))
        for key in [k for k in lines if k[0] in remove]:
            del lines[key]
        if not lines:
            path.unlink(missing_ok=True)
            return
        atomic_write(path, b"".join(raw + b"\n" for _, raw in sorted(lines.values(), key=lambda v: v[0])))

    def append_unique(self, path: Path, raws: list[bytes]) -> None:
        existing = read_lines(path)
        have = set(existing)
        new = [r for r in raws if r not in have]
        if new:
            atomic_write(path, b"".join(r + b"\n" for r in existing + new))

    # ------------------------------------------------------------------ day state
    def load_day_files(self, day: str) -> dict[str, list[dict]]:
        d = self.day_dir(day)
        out: dict[str, list[dict]] = {}
        if d.is_dir():
            for p in sorted(d.glob("*.ndjson")):
                out[p.name] = [json.loads(line) for line in read_lines(p)]
        return out

    def refresh_dirty(self) -> list[str]:
        """Refresh every dirty day. A day that fails stays dirty and is reported; others go on."""
        errors = []
        for (day,) in self.db.execute("SELECT day FROM dirty_days ORDER BY day").fetchall():
            try:
                self.refresh_day(day)
            except Exception as exc:
                errors.append(f"{self.user_id} {day}: {exc}")
                continue
            self.save()
            with self.db:
                self.db.execute("DELETE FROM dirty_days WHERE day = ?", (day,))
        self.refresh_errors.extend(e for e in errors if e not in self.refresh_errors)
        return errors

    def refresh_day(self, day: str) -> None:
        d = self.day_dir(day)
        files = self.load_day_files(day)
        entry = self.manifest["days"].get(day, {})
        archive = entry.pop("archive", None)
        if archive:
            (self.root / archive["file"]).unlink(missing_ok=True)
        file_info = {name: {"records": len(recs), "sha256": sha256_hex((d / name).read_bytes())}
                     for name, recs in files.items()}
        entry.update({"status": "provisional", "touched_at": instant(self.now), "files": file_info,
                      "content_sha256": sha256_hex(json.dumps(file_info, sort_keys=True).encode())})
        if not files:  # every record of the day was deleted
            self.manifest["days"].pop(day, None)
            for name in ("summary.json", "_day.json"):
                (d / name).unlink(missing_ok=True)
            return
        self.manifest["days"][day] = entry
        try:
            summary = D.summarize(day, files, self.store.config().get("source_priority", []))
        except Exception as exc:  # a summary must never block the archive
            summary = {"day": day, "error": f"summary failed: {exc}"}
        write_json(d / "summary.json", summary)
        write_json(d / "_day.json", {"day": day, "user_id": self.user_id, "status": "provisional",
                                     "files": file_info, "updated_at": instant(self.now)})

    def finalize(self, settle: timedelta = SETTLE) -> list[str]:
        """Mark days untouched for ``settle`` (HKA_SETTLE_DAYS, default 7 days) final and archive them."""
        done = []
        dirty = {d for (d,) in self.db.execute("SELECT day FROM dirty_days")}
        for day, entry in sorted(self.manifest["days"].items()):
            if day in dirty:
                continue  # refresh failed; finalize only once its files and manifest agree
            if entry.get("status") != "provisional" or self.now - parse_instant(entry["touched_at"]) < settle:
                continue
            d = self.day_dir(day)
            if d.is_dir():
                day_json = read_json(d / "_day.json", {})
                day_json["status"] = "final"
                write_json(d / "_day.json", day_json)
                rel = Path("archives") / day[:4] / f"{day}.tar.gz"
                data = build_archive(d, day)
                atomic_write(self.root / rel, data)
                entry["archive"] = {"file": str(rel), "bytes": len(data), "sha256": sha256_hex(data),
                                    "created_at": instant(self.now)}
            entry["status"] = "final"
            done.append(day)
        return done

    def save(self) -> None:
        write_json(self.root / "manifest.json", self.manifest)


def build_archive(folder: Path, day: str) -> bytes:
    """Reproducible tar.gz: sorted members, mtime 0, no owner."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for path in sorted(folder.iterdir()):
                if not path.is_file() or path.name.startswith("."):  # e.g. Finder's .DS_Store
                    continue
                info = tarfile.TarInfo(f"{day}/{path.name}")
                data = path.read_bytes()
                info.size, info.mtime, info.mode = len(data), 0, 0o644
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@dataclass
class BuildReport:
    batches: int = 0
    skipped_not_recipient: int = 0
    days_touched: int = 0
    days_finalized: int = 0
    records_moved: int = 0   # by a day-rule migration
    records_unplaced: int = 0  # by a day-rule migration: left where they were (zone unknown)
    errors: list[str] = field(default_factory=list)


def build(store: Store, identity: Identity, now: datetime, settle: timedelta | None = None) -> BuildReport:
    settle = settle_period() if settle is None else settle
    store.check_identity(identity)
    report = BuildReport()
    pins = store.pins()
    by_user: dict[str, list[str]] = defaultdict(list)
    for user_id, device_id in store.inbox_devices():
        pin = pins.get(device_id)
        if pin and pin.get("status") == "confirmed" and pin["user_id"] == user_id:
            by_user[user_id].append(device_id)
    for user_id, devices in sorted(by_user.items()):
        try:
            ub = UserBuild(store, user_id, now)
        except Exception as exc:
            report.errors.append(f"{user_id}: {exc}")
            continue
        try:
            ub.refresh_dirty()  # finish days left dirty by an interrupted run
            moved, unplaced = ub.migrate_day_rules(identity)
            report.records_moved += moved
            report.records_unplaced += unplaced
            for device_id in devices:
                build_device(store, identity, ub, user_id, device_id, report)
            report.days_finalized += len(ub.finalize(settle))
            ub.save()
            report.errors.extend(ub.refresh_errors)
        except Exception as exc:
            report.errors.append(f"{user_id}: {exc}")
        finally:
            ub.close()
    return report


def build_device(store: Store, identity: Identity, ub: UserBuild, user_id: str, device_id: str,
                 report: BuildReport) -> None:
    dev_dir = store.device_dir(user_id, device_id)
    verified = (read_json(dev_dir / "state.json", {}) or {}).get("verified_seq", 0)
    for seq in range(ub.built_seq(device_id) + 1, verified + 1):
        mpath, cpath = batch_paths(dev_dir, seq)
        try:
            m, _ = load_manifest_file(mpath)
            if identity.processor_id not in m.recipients:
                ub.mark_built(device_id, seq)
                report.skipped_not_recipient += 1
                continue
            lines = decrypt_batch(cpath.read_bytes(), identity)
            report.days_touched += len(ub.apply(device_id, seq, ub.plan(device_id, seq, lines)))
            report.batches += 1
        except Exception as exc:
            # Keep order: later batches of this device wait until this one is resolved.
            report.errors.append(f"{device_id} seq {seq}: {exc}")
            return
        finally:
            ub.save()


def census_mismatches(store: Store, user_id: str) -> list[dict]:
    """Compare each device's latest census with the index (spec records.md, census)."""
    path = store.user_dir(user_id) / "index.sqlite"
    if not path.exists():
        return []
    db = sqlite3.connect(path)
    try:
        out = []
        for device_id, type_, day, tz, count, _uuids, _seq in db.execute("SELECT * FROM census ORDER BY day, type"):
            try:
                # Records are filed by start (sleep: by the sleep day of its end), so look a few days around.
                slack = timedelta(days=3 if type_ == D.SLEEP else 2)
                lo = (datetime.fromisoformat(day) - slack).date().isoformat()
                hi = (datetime.fromisoformat(day) + slack).date().isoformat()
            except (ValueError, OverflowError, TypeError):
                continue
            uuids = {u for u, start in db.execute(
                "SELECT uuid, start FROM records WHERE type = ? AND day BETWEEN ? AND ? AND start IS NOT NULL",
                (type_, lo, hi)) if D.local_day(start, tz) == day}
            if len(uuids) != count:
                out.append({"device_id": device_id, "type": type_, "day": day, "device": count, "archive": len(uuids)})
        return out
    finally:
        db.close()
