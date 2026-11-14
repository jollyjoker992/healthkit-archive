"""`hka pull`: download and verify grants, manifests and ciphertext (spec section 7).

Nothing is decrypted here. Everything stored under inbox/ has been checked against the
device key pinned by the owner. The relay is never trusted for keys, ordering, identifiers
or time: alarms are driven by local state and the local clock.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from hkacommon.client import ApiError, Client
from hkacommon.documents import ID_RE, Grant, Manifest, parse_grant, parse_manifest, verify_signed
from hkacommon.encoding import FormatError, b64u_decode, is_instant, is_int, parse_instant, sha256_hex
from hkacommon.keys import fingerprint, key_id, load_public_key, public_bytes
from .store import Identity, Store, atomic_write, instant, read_json, write_json

GAP_ALARM_AFTER = timedelta(hours=24)
SILENCE_ALARM_AFTER = timedelta(days=3)
MAX_PAGES = 10_000
STATUSES = ("active", "retired", "purged")


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    alarms: list[str] = field(default_factory=list)

    def info(self, msg: str) -> None:
        self.lines.append(msg)

    def alarm(self, msg: str) -> None:
        self.alarms.append(msg)


class VerifyError(Exception):
    pass


def batch_paths(dev_dir: Path, seq: int) -> tuple[Path, Path]:
    base = dev_dir / "batches" / f"{seq:010d}"
    return base.with_suffix(".manifest.json"), base.with_suffix(".age")


def load_manifest_file(path: Path) -> tuple[Manifest, dict] | None:
    entry = read_json(path)
    if entry is None:
        return None
    return parse_manifest(b64u_decode(entry["manifest"])), entry


def verified_grants(dev_dir: Path) -> dict[int, Grant]:
    out: dict[int, Grant] = {}
    gdir = dev_dir / "grants"
    if gdir.is_dir():
        for p in sorted(gdir.glob("*.json")):
            entry = json.loads(p.read_text())
            g = parse_grant(b64u_decode(entry["doc"]))
            out[g.grant_seq] = g
    return out


def validate_stream(s: object) -> dict:
    """Check a /v1/streams entry before anything from it is used or written to disk."""
    if not isinstance(s, dict):
        raise VerifyError("stream entry is not an object")
    did, uid = s.get("device_id"), s.get("user_id")
    if not (isinstance(did, str) and ID_RE["d"].fullmatch(did) and isinstance(uid, str) and ID_RE["u"].fullmatch(uid)):
        raise VerifyError("stream entry with malformed ids")
    try:
        pub = public_bytes(load_public_key(s.get("device_pub")))
    except FormatError as exc:
        raise VerifyError(f"{did}: malformed device key from the relay") from exc
    if key_id("d", pub) != did:
        raise VerifyError(f"{did}: the relay pairs this device id with another key")
    for name in ("user_name", "device_name"):
        if not isinstance(s.get(name), str) or not 1 <= len(s[name]) <= 64:
            raise VerifyError(f"{did}: malformed {name}")
    if s.get("status") not in STATUSES or not isinstance(s.get("granted"), bool):
        raise VerifyError(f"{did}: malformed status")
    if not all(is_int(s.get(f)) for f in ("contiguous_seq", "max_seq")):
        raise VerifyError(f"{did}: malformed seq fields")
    return s


class Puller:
    def __init__(self, store: Store, identity: Identity, client: Client, now: datetime, report: Report):
        self.store, self.me, self.client, self.now, self.report = store, identity, client, now, report

    # ------------------------------------------------------------------ pins
    def pin_check(self, stream: dict, pins: dict) -> dict | None:
        """Return the pin for a confirmed device, recording new devices as pending."""
        did = stream["device_id"]
        pin = pins.get(did)
        if pin is None:
            pins[did] = {
                "pub": stream["device_pub"], "user_id": stream["user_id"], "user_name": stream["user_name"],
                "device_name": stream["device_name"], "fingerprint": fingerprint(b64u_decode(stream["device_pub"])),
                "status": "pending", "first_seen": instant(self.now),
            }
            self.report.info(f"new device {did} ({stream['user_name']}, {stream['device_name']}), fingerprint "
                             f"{pins[did]['fingerprint']}: compare it with the app, then `hka confirm {did}`")
            return None
        if pin["pub"] != stream["device_pub"] or pin["user_id"] != stream["user_id"]:
            self.report.alarm(f"{did}: the relay presents a different key or user than the one pinned; ignoring it")
            return None
        if pin["status"] != "confirmed":
            self.report.info(f"{did} ({pin['user_name']}) awaits `hka confirm {did}`")
            return None
        return pin

    # ------------------------------------------------------------------ grants
    def pull_grants(self, did: str, pin: dict, dev_dir: Path) -> dict[int, Grant]:
        known = verified_grants(dev_dir)
        data = self.client.get(f"/v1/devices/{did}/grants")
        if not isinstance(data, dict) or not isinstance(data.get("grants"), list):
            raise VerifyError(f"{did}: malformed grant list")
        for entry in data["grants"]:
            if not (isinstance(entry, dict) and isinstance(entry.get("doc"), str) and isinstance(entry.get("sig"), str)
                    and is_instant(entry.get("received_at"))):
                raise VerifyError(f"{did}: malformed grant entry")
            raw = b64u_decode(entry["doc"])
            if not verify_signed(pin["pub"], "grant", raw, entry["sig"]):
                self.report.alarm(f"{did}: a grant is not signed by the pinned device key")
                continue
            g = parse_grant(raw)
            if g.device_id != did or g.user_id != pin["user_id"]:
                self.report.alarm(f"{did}: grant {g.grant_seq} names another device or user")
                continue
            mine = [p for p in g.processors if p["id"] == self.me.processor_id]
            if mine and (mine[0]["age_recipient"] != self.me.age_recipient or mine[0]["sign_pub"] != self.me.sign.pub_b64u):
                self.report.alarm(f"{did}: grant {g.grant_seq} lists this processor with a different key; "
                                  "a card may have been tampered with")
                continue
            if g.grant_seq in known:
                if known[g.grant_seq].sha256 != g.sha256:
                    self.report.alarm(f"{did}: two different device-signed grants with grant_seq {g.grant_seq}")
                continue
            write_json(dev_dir / "grants" / f"{g.grant_seq:06d}.json",
                       {"doc": entry["doc"], "sig": entry["sig"], "received_at": entry["received_at"]})
            known[g.grant_seq] = g
        return known

    # ------------------------------------------------------------------ batches
    def pull_batches(self, did: str, pin: dict, dev_dir: Path, state: dict) -> None:
        # From the verified prefix, not the highest seq seen: uploads arrive out of order, so a
        # gap below the highest seq is filled later and must be listed again.
        after = state.get("verified_seq", 0)
        for _ in range(MAX_PAGES):
            page = self.client.get(f"/v1/devices/{did}/batches?after={after}&limit=500")
            if not (isinstance(page, dict) and isinstance(page.get("batches"), list)
                    and is_int(page.get("contiguous_seq")) and is_int(page.get("max_seq"))):
                raise VerifyError(f"{did}: malformed batch listing")
            state["server_contiguous_seq"], state["server_max_seq"] = page["contiguous_seq"], page["max_seq"]
            if not page["batches"]:
                return
            for b in page["batches"]:
                if not (isinstance(b, dict) and is_int(b.get("seq"), 1) and b["seq"] > after):
                    raise VerifyError(f"{did}: the relay's batch listing does not advance")
                after = self.fetch_batch(did, pin, dev_dir, b)
        raise VerifyError(f"{did}: batch listing did not end after {MAX_PAGES} pages")

    def fetch_batch(self, did: str, pin: dict, dev_dir: Path, b: dict) -> int:
        if not (isinstance(b.get("manifest"), str) and isinstance(b.get("sig"), str) and is_int(b.get("size"), 1)
                and is_instant(b.get("received_at"))):
            raise VerifyError(f"{did}: malformed listing entry for seq {b['seq']}")
        raw = b64u_decode(b["manifest"])
        if not verify_signed(pin["pub"], "manifest", raw, b["sig"]):
            raise VerifyError(f"{did}: manifest {b['seq']} is not signed by the pinned device key")
        m = parse_manifest(raw)
        if m.device_id != did or m.user_id != pin["user_id"] or m.seq != b["seq"] or m.size != b["size"]:
            raise VerifyError(f"{did}: manifest {b['seq']} does not match its listing")
        mpath, cpath = batch_paths(dev_dir, m.seq)
        existing = load_manifest_file(mpath)
        if existing and existing[0].sha256 != m.sha256:
            raise VerifyError(f"{did}: the relay served two different signed manifests for seq {m.seq}")
        if self.me.processor_id in m.recipients and not cpath.exists():
            data = self.client.get_bytes(f"/v1/devices/{did}/batches/{m.seq}", max_bytes=m.size)
            if len(data) != m.size or sha256_hex(data) != m.ciphertext_sha256:
                raise VerifyError(f"{did}: ciphertext {m.seq} does not match its manifest")
            atomic_write(cpath, data)
        if not existing:
            write_json(mpath, {"manifest": b["manifest"], "sig": b["sig"], "received_at": b["received_at"]})
        return m.seq

    # ------------------------------------------------------------------ chain
    def advance_chain(self, did: str, dev_dir: Path, state: dict, grants: dict[int, Grant]) -> None:
        seq = state.get("verified_seq", 0)
        head = state.get("chain_head")
        start = seq
        while True:
            mpath, cpath = batch_paths(dev_dir, seq + 1)
            loaded = load_manifest_file(mpath)
            if loaded is None:
                break
            m, entry = loaded
            if m.prev != head:
                self.report.alarm(f"{did}: hash chain broken at seq {m.seq} (prev does not match seq {seq})")
                break
            g = grants.get(m.grant_seq)
            if g is None:
                break  # grant not fetched yet; try again next pull
            if g.sha256 != m.grant_sha256 or list(m.recipients) != g.processor_ids:
                self.report.alarm(f"{did}: batch {m.seq} does not match grant {m.grant_seq}")
                break
            if self.me.processor_id in m.recipients and not cpath.exists():
                break
            seq, head = m.seq, m.sha256
            state["last_created_at"] = m.created_at  # device-signed, unlike the relay's received_at
        state["verified_seq"], state["chain_head"] = seq, head
        if seq > start:
            state["last_advanced_at"] = instant(self.now)  # local clock: the relay cannot move it
        server_max = state.get("server_max_seq", 0)
        if server_max > seq or (dev_dir / "batches" / f"{seq + 1:010d}.manifest.json").exists():
            state.setdefault("gap_since", instant(self.now))
            if self.now - parse_instant(state["gap_since"]) > GAP_ALARM_AFTER:
                self.report.alarm(f"{did}: stuck at seq {seq} (relay has up to {server_max}) since {state['gap_since']}")
        else:
            state.pop("gap_since", None)

    # ------------------------------------------------------------------ one device
    def pull_device(self, stream: dict, pin: dict) -> None:
        did = stream["device_id"]
        dev_dir = self.store.device_dir(pin["user_id"], did)
        state_path = dev_dir / "state.json"
        state = read_json(state_path, {})
        state.setdefault("watch_since", instant(self.now))
        state.update({"server_status": stream["status"], "granted": stream["granted"]})
        try:
            grants = self.pull_grants(did, pin, dev_dir)
            if stream["status"] == "purged":
                if not state.get("purge_reported"):
                    self.report.info(f"{did} ({pin['user_name']}): the relay reports this device as purged. "
                                     "Your local copy is kept; delete it yourself if that was the member's wish.")
                    state["purge_reported"] = instant(self.now)
            elif not stream["granted"]:
                self.check_removal(did, pin, grants, state, stream)
            else:
                state.pop("removed_by_grant", None)
                state.pop("removed_still_uploading_reported", None)
                self.pull_batches(did, pin, dev_dir, state)
            self.advance_chain(did, dev_dir, state, grants)
        except ApiError as exc:
            if exc.code == "not_granted":
                self.check_removal(did, pin, verified_grants(dev_dir), state, stream)
            else:
                self.report.alarm(f"{did}: relay error {exc}")
        except Exception as exc:  # the relay's data must never stop other devices
            self.report.alarm(f"{did}: {exc}")
        finally:
            self.check_liveness(did, pin, stream["status"], state)
            write_json(state_path, state)

    def check_removal(self, did: str, pin: dict, grants: dict[int, Grant], state: dict, stream: dict) -> None:
        newest = max(grants) if grants else 0
        if grants and self.me.processor_id not in grants[newest].processor_ids:
            if state.get("removed_by_grant") != newest:
                state["removed_by_grant"] = newest
                state["max_seq_at_removal"] = stream["max_seq"]
                self.report.info(f"{did} ({pin['user_name']}): removed as a processor by device-signed grant {newest}.")
            elif stream["max_seq"] > state.get("max_seq_at_removal", 0) and not state.get("removed_still_uploading_reported"):
                state["removed_still_uploading_reported"] = instant(self.now)
                self.report.info(f"{did} ({pin['user_name']}): still uploading after removing this processor; "
                                 "ask the member whether the removal was intended")
        else:
            self.report.alarm(f"{did} ({pin['user_name']}): access lost without a device-signed grant removing "
                              "this processor; the relay may be withholding data")

    def check_liveness(self, did: str, pin: dict, status: str, state: dict) -> None:
        if status != "active":
            if state.get("status_reported") != status:
                state["status_reported"] = status
                self.report.info(f"{did} ({pin['user_name']}): device is now {status}")
            return
        if state.get("removed_by_grant"):
            return
        since = state.get("last_advanced_at") or state.get("watch_since") or pin.get("confirmed_at")
        if since and is_instant(since) and self.now - parse_instant(since) > SILENCE_ALARM_AFTER:
            self.report.alarm(f"{did} ({pin['user_name']}): no new verified batch since {since}")


def pull(store: Store, identity: Identity, client: Client, now: datetime) -> Report:
    store.check_identity(identity)
    report = Report()
    puller = Puller(store, identity, client, now, report)
    pins = store.pins()
    listed: set[str] = set()
    streams_ok = True
    try:
        try:
            data = client.get("/v1/streams")
            streams = data["streams"] if isinstance(data, dict) and isinstance(data.get("streams"), list) else None
            if streams is None:
                report.alarm("malformed /v1/streams response")
                streams, streams_ok = [], False
        except ApiError as exc:
            if exc.code != "unknown_key":
                report.alarm(f"cannot list devices: {exc}")
                streams_ok = False
            streams = []
        except Exception as exc:  # network errors and anything else the relay can provoke
            report.alarm(f"cannot reach the relay: {exc!r}")
            streams, streams_ok = [], False
        for raw in streams:
            try:
                stream = validate_stream(raw)
            except VerifyError as exc:
                report.alarm(str(exc))
                continue
            listed.add(stream["device_id"])
            pin = puller.pin_check(stream, pins)
            if pin is not None:
                puller.pull_device(stream, pin)
        # Devices the relay stopped listing are checked from local state alone.
        for did, pin in sorted(pins.items()):
            if pin.get("status") != "confirmed" or did in listed:
                continue
            state_path = store.device_dir(pin["user_id"], did) / "state.json"
            state = read_json(state_path, {}) or {}
            if streams_ok:
                report.alarm(f"{did} ({pin['user_name']}): the relay no longer lists this device")
            last_status = state.get("server_status") or "active"
            if last_status == "active" and state.get("removed_by_grant") is None:
                puller.check_liveness(did, pin, "active", state)
            if state:
                write_json(state_path, state)
        if not pins and not streams:
            report.info("no device has listed this processor yet (share its card with `hka card`)")
    finally:
        store.save_pins(pins)
    return report


def confirm(store: Store, device_id: str, fingerprint_text: str | None, now: datetime) -> str:
    pins = store.pins()
    pin = pins.get(device_id)
    if pin is None:
        raise KeyError(f"{device_id} is not known; run `hka pull` first")
    if fingerprint_text is not None and fingerprint_text.strip().upper() != pin["fingerprint"]:
        raise ValueError(f"fingerprint mismatch: the relay gave {pin['fingerprint']}")
    pin["status"], pin["confirmed_at"] = "confirmed", instant(now)
    store.save_pins(pins)
    return pin["fingerprint"]
