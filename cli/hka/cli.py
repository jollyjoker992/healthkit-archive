"""hka: the processor. Downloads, verifies and decrypts a family's HealthKit batches.

Data directory: $HKA_DATA_DIR (default ./data). See ../spec/README.md and docs/design.md.
Exit codes: 0 ok, 1 partial (alarms or errors), 2 configuration, 3 another run holds the lock.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from hkacommon.client import ApiError, Client
from hkacommon.documents import ORIGIN_RE, parse_grant, verify_signed
from hkacommon.encoding import FormatError, b64u_decode, is_name, sha256_hex
from hkacommon.qr import print_qr
from . import backup as K
from . import build as B
from . import pull as P
from .store import (EXIT_CONFIG, EXIT_LOCKED, EXIT_OK, EXIT_PARTIAL, ConfigError, Locked, Store, create_identity,
                    now_utc, read_json, write_json)


def store_from(args) -> Store:
    return Store(Path(args.data_dir or os.environ.get("HKA_DATA_DIR") or "data").resolve())


def client_for(store: Store, args) -> Client:
    return Client(store.server(args.server), store.identity().sign, "p")


def cmd_init(args) -> int:
    if args.name is not None and not is_name(args.name):
        raise ConfigError("--name must be 1 to 64 characters")
    if args.recovery:
        if not args.out:
            raise ConfigError("--recovery needs --out DIR (a folder you will move offline)")
        out = Path(args.out)
        ident = create_identity(out, args.name or "Recovery", now_utc())
        print(f"Created an offline recovery identity in {out}.")
        print("Move that folder to two offline places (e.g. USB drives). It is never used by `hka pull`.")
        print("Members add its card so that their data can be recovered if the main processor key is lost.\n")
        show_card(ident.card, qr=True)
        return EXIT_OK
    store = store_from(args)
    if args.server and not ORIGIN_RE.fullmatch(args.server):
        raise ConfigError("--server must be https://host[:port] without a path")
    ident = create_identity(store.keys_dir, args.name or "Processor", now_utc())
    conf = store.config()
    if args.server:
        conf["server"] = args.server
    conf["processor_id"] = ident.processor_id
    write_json(store.config_path, conf)
    print(f"Created processor identity in {store.keys_dir} (files are mode 600).")
    print(f"processor_id: {ident.processor_id}")
    print("\nIMPORTANT: copy keys/age.txt to two offline places now. Without it nothing this processor\n"
          "received can ever be decrypted. Restore needs only that file and the stock `age` tool.")
    return EXIT_OK


def show_card(card, qr: bool) -> None:
    if qr:
        print_qr(card.text())
    print(card.text())
    print(f"\nname:        {card.name}\nprocessor:   {card.processor_id}\nfingerprint: {card.fingerprint}")
    print("The member's app must show this same fingerprint before adding the processor.")


def cmd_card(args) -> int:
    show_card(store_from(args).identity().card, qr=args.qr)
    return EXIT_OK


def cmd_confirm(args) -> int:
    store = store_from(args)
    with store.lock():
        pins = store.pins()
        pin = pins.get(args.device)
        if pin is None:
            raise ConfigError(f"{args.device} is not known; run `hka pull` first")
        fp = args.fingerprint
        if fp is None:
            print(f"{args.device}: {pin['user_name']}, {pin['device_name']}\nrelay says fingerprint {pin['fingerprint']}")
            fp = input("Type the fingerprint shown in the member's app: ")
        try:
            P.confirm(store, args.device, fp, now_utc())
        except ValueError as exc:
            print(f"error: {exc}. Nothing pinned; check with the member.", file=sys.stderr)
            return EXIT_PARTIAL
    print(f"Pinned {args.device}. Run `hka pull` to download its data.")
    return EXIT_OK


def run_pull(store: Store, args) -> int:
    report = P.pull(store, store.identity(), client_for(store, args), now_utc())
    for line in report.lines:
        print(line)
    for alarm in report.alarms:
        print(f"ALARM: {alarm}", file=sys.stderr)
    return EXIT_PARTIAL if report.alarms else EXIT_OK


def run_build(store: Store) -> int:
    r = B.build(store, store.identity(), now_utc())
    print(f"built {r.batches} batches ({r.skipped_not_recipient} not addressed to this processor), "
          f"{r.days_touched} day updates, {r.days_finalized} days finalized")
    if r.records_moved:
        print(f"moved {r.records_moved} sleep records to the sleep day they belong to (day rules {B.D.DAY_RULES})")
    if r.records_unplaced:
        print(f"WARNING: {r.records_unplaced} sleep records left where they were: their time zone could not be "
              "worked out", file=sys.stderr)
    for e in r.errors:
        print(f"ERROR: {e}", file=sys.stderr)
    return EXIT_PARTIAL if r.errors else EXIT_OK


def cmd_pull(args) -> int:
    store = store_from(args)
    with store.lock():
        return run_pull(store, args)


def cmd_build(args) -> int:
    store = store_from(args)
    with store.lock():
        return run_build(store)


def run_backup(store: Store, targets: dict, checked: K.Checked) -> int:
    r = K.backup(store, targets, now_utc(), checked)
    for line in r.lines:
        print(line)
    for e in r.errors:
        print(f"ERROR: backup: {e}", file=sys.stderr)
    return EXIT_PARTIAL if r.errors else EXIT_OK


def selected_targets(names: list[str] | None) -> dict:
    """The configured targets, or only ``names`` (the others are then not even checked)."""
    if not names:
        return K.targets_from_env()
    targets = {n: t for name in names for n, t in K.targets_from_env(only=name).items()}
    for name in names:
        if name not in targets:
            raise ConfigError(f"backup target {name!r} is not configured")
    return targets


def cmd_run(args) -> int:
    store = store_from(args)
    B.settle_period()  # build needs it: a bad value stops the run before pulling
    try:  # a bad backup setting must not stop the pull and build: report it now, skip only the backup
        targets = K.targets_from_env()
        checked = K.preflight(store) if targets else None
        backup_error = None
    except ConfigError as exc:
        targets, checked, backup_error = {}, None, exc
        print(f"error: backup disabled for this run: {exc}", file=sys.stderr)
    with store.lock():
        code = max(run_pull(store, args), run_build(store))
        if backup_error is not None:
            print(f"error: no backup made: {backup_error}", file=sys.stderr)
            return max(code, EXIT_CONFIG)
        return max(code, run_backup(store, targets, checked)) if targets else code


def cmd_backup(args) -> int:
    store = store_from(args)
    targets = selected_targets(args.target)
    if not targets:
        raise ConfigError("no backup target configured: set the credentials of AWS S3 (HKA_AWS_*), "
                          "Cloudflare R2 (HKA_R2_*), Google Drive (HKA_GDRIVE_*) or HKA_BACKUP_RCLONE_REMOTE")
    checked = K.preflight(store)
    with store.lock():
        return run_backup(store, targets, checked)


def cmd_restore(args) -> int:
    store = store_from(args)
    target = K.targets_from_env(only=args.source).get(args.source)
    if target is None:
        raise ConfigError(f"backup target {args.source!r} is not configured")
    # No lock: restore writes only to --out and its own staging folder, so `hka run` keeps working.
    r = K.restore(store, target, Path(args.out), Path(args.identity) if args.identity else None, args.user)
    for line in r.lines:
        print(line)
    for e in r.errors:
        print(f"ERROR: {e}", file=sys.stderr)
    if r.lines:
        print("decrypted health data: handle with care")
    return EXIT_PARTIAL if r.errors else EXIT_OK


def status_data(store: Store) -> dict:
    pins = store.pins()
    out = {"data_dir": str(store.root), "processor_id": None, "devices": [], "users": []}
    try:
        out["processor_id"] = store.identity().processor_id
    except ConfigError:
        pass
    for did, pin in sorted(pins.items(), key=lambda kv: (kv[1]["user_id"], kv[0])):
        state = read_json(store.device_dir(pin["user_id"], did) / "state.json", {}) or {}
        out["devices"].append({
            "device_id": did, "user_id": pin["user_id"], "user_name": pin["user_name"],
            "device_name": pin["device_name"], "pin": pin["status"], "fingerprint": pin["fingerprint"],
            "server_status": state.get("server_status"), "granted": state.get("granted"),
            "verified_seq": state.get("verified_seq", 0), "server_max_seq": state.get("server_max_seq"),
            "gap_since": state.get("gap_since"), "last_advanced_at": state.get("last_advanced_at"),
            "last_created_at": state.get("last_created_at"),
            "removed_by_grant": state.get("removed_by_grant"),
        })
    users_dir = store.root / "users"
    for udir in sorted(p for p in users_dir.iterdir() if p.is_dir() and not p.name.startswith(".")) \
            if users_dir.is_dir() else []:
        try:  # one unreadable user must not hide the others
            manifest = read_json(udir / "manifest.json", {"days": {}})
            days = manifest.get("days", {})
            out["users"].append({
                "user_id": udir.name,
                "days": len(days),
                "final": sum(1 for d in days.values() if d.get("status") == "final"),
                "provisional": sum(1 for d in days.values() if d.get("status") == "provisional"),
                "range": [min(days), max(days)] if days else None,
                "census_mismatches": B.census_mismatches(store, udir.name),
            })
        except Exception as exc:
            out["users"].append({"user_id": udir.name, "error": f"cannot read: {exc!r}", "days": 0, "final": 0,
                                 "provisional": 0, "range": None, "census_mismatches": []})
    try:
        out["settle_days"] = B.settle_period().days
    except ConfigError as exc:
        out["settle_days"], out["settle_config_error"] = None, str(exc)
    try:
        out["backup_targets"] = sorted(K.targets_from_env())
    except ConfigError as exc:
        out["backup_targets"], out["backup_config_error"] = [], str(exc)
    backup = K.status(store)
    if isinstance(backup.get("error"), str):
        out["backup_state_error"], backup = backup["error"], {}
    for u in out["users"]:
        u["backup"] = backup.get(u["user_id"], {})
    return out


def cmd_status(args) -> int:
    data = status_data(store_from(args))
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return EXIT_OK
    print(f"data: {data['data_dir']}  processor: {data['processor_id']}")
    for d in data["devices"]:
        flags = []
        if d["pin"] != "confirmed":
            flags.append(f"awaiting `hka confirm` (fingerprint {d['fingerprint']})")
        if d["gap_since"]:
            flags.append(f"waiting for missing batches since {d['gap_since']}")
        if d["removed_by_grant"]:
            flags.append(f"removed by grant {d['removed_by_grant']}")
        print(f"  {d['user_name']} / {d['device_name']} {d['device_id']} [{d['server_status']}] "
              f"seq {d['verified_seq']}/{d['server_max_seq']} last batch {d['last_created_at']} "
              f"(verified {d['last_advanced_at']}) {'; '.join(flags)}")
    for u in data["users"]:
        if u.get("error"):
            print(f"  {u['user_id']}: ERROR {u['error']}")
            continue
        rng = f"{u['range'][0]}..{u['range'][1]}" if u["range"] else "-"
        print(f"  {u['user_id']}: {u['days']} days ({u['final']} final, {u['provisional']} provisional) {rng}; "
              f"{len(u['census_mismatches'])} census mismatches")
        if "error" in u["backup"]:
            print(f"    backup: {u['backup']['error']}")
            continue
        for name in data["backup_targets"]:
            b = u["backup"].get(name)
            if b is None:
                print(f"    backup {name}: never run")
                continue
            print(f"    backup {name}: {b['archives_uploaded']}/{b['archives_final']} final days"
                  f"{', up to date' if b['up_to_date'] else ''}, checked {b['checked_at']}"
                  + (f"; {b['unchecked_cold']} file(s) in Glacier not checked" if b.get("unchecked_cold") else "")
                  + (f"; ERROR {b['error']}" if b["error"] else ""))
    if data.get("backup_state_error"):
        print(f"  backup: ERROR {data['backup_state_error']}")
    if data.get("settle_config_error"):
        print(f"  {data['settle_config_error']}")
    else:
        print(f"  days become final (and are backed up) {data['settle_days']} days after their last change "
              "(HKA_SETTLE_DAYS)")
    if data.get("backup_config_error"):
        print(f"  backup: {data['backup_config_error']}")
    elif not data["backup_targets"]:
        print("  backup: no target configured")
    return EXIT_OK


def cmd_verify(args) -> int:
    """Re-check every stored grant, manifest, chain link and ciphertext hash, and test-decrypt."""
    store = store_from(args)
    me = store.identity()
    store.check_identity(me)
    pins = store.pins()
    problems: list[str] = []
    for user_id, did in store.inbox_devices():
        pin = pins.get(did)
        if not pin or pin["status"] != "confirmed" or pin["user_id"] != user_id:
            problems.append(f"{did}: inbox folder without a confirmed pin for {user_id}")
            continue
        dev_dir = store.device_dir(user_id, did)
        grants = {}
        for g_path in sorted((dev_dir / "grants").glob("*.json")):
            entry = json.loads(g_path.read_text())
            raw = b64u_decode(entry["doc"])
            g = parse_grant(raw)
            mine = [p for p in g.processors if p["id"] == me.processor_id]
            if (not verify_signed(pin["pub"], "grant", raw, entry["sig"]) or g.device_id != did
                    or g.user_id != user_id or g_path.name != f"{g.grant_seq:06d}.json"
                    or (mine and (mine[0]["age_recipient"] != me.age_recipient
                                  or mine[0]["sign_pub"] != me.sign.pub_b64u))):
                problems.append(f"{did}: {g_path.name} fails verification")
                continue
            grants[g.grant_seq] = g
        head, seq, last_mine = None, 0, None
        state = read_json(dev_dir / "state.json", {}) or {}
        while seq < state.get("verified_seq", 0):
            seq += 1
            mpath, cpath = P.batch_paths(dev_dir, seq)
            loaded = P.load_manifest_file(mpath)
            if loaded is None:
                problems.append(f"{did}: seq {seq}: manifest missing")
                break
            m, entry = loaded
            g = grants.get(m.grant_seq)
            ok = (verify_signed(pin["pub"], "manifest", m.raw, entry["sig"]) and m.prev == head and m.seq == seq
                  and m.device_id == did and m.user_id == user_id and g is not None
                  and g.sha256 == m.grant_sha256 and list(m.recipients) == g.processor_ids)
            if me.processor_id in m.recipients:
                data = cpath.read_bytes() if cpath.exists() else b""
                ok = ok and len(data) == m.size and sha256_hex(data) == m.ciphertext_sha256
                last_mine = cpath
            if not ok:
                problems.append(f"{did}: seq {seq}: verification failed")
            head = m.sha256
        if head != state.get("chain_head"):
            problems.append(f"{did}: chain head differs from state.json")
        if last_mine is not None:
            try:
                B.decrypt_batch(last_mine.read_bytes(), me)
            except B.BatchError as exc:
                problems.append(f"{did}: test decryption of {last_mine.name} failed: {exc}")
        print(f"{did}: checked {seq} batches")
    for p in problems:
        print(f"PROBLEM: {p}", file=sys.stderr)
    return EXIT_PARTIAL if problems else EXIT_OK


def cmd_export(args) -> int:
    """Zip a user's decrypted day folders (optionally a date range) for handing over."""
    store = store_from(args)
    days_dir = store.user_dir(args.user) / "days"
    if not days_dir.is_dir() and not (store.user_dir(args.user) / "undated").is_dir():
        raise ConfigError(f"no data for {args.user}")
    out = Path(args.out)
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        undated = store.user_dir(args.user) / "undated"
        if undated.is_dir() and not (args.date_from or args.date_to):
            for f in sorted(f for f in undated.iterdir() if f.is_file() and not f.name.startswith(".")):
                z.write(f, f"{args.user}/undated/{f.name}")
        years = [y for y in days_dir.iterdir() if y.is_dir()] if days_dir.is_dir() else []
        for day in sorted(p for year in years for p in year.iterdir() if p.is_dir()):
            if (args.date_from and day.name < args.date_from) or (args.date_to and day.name > args.date_to):
                continue
            files = [f for f in sorted(day.iterdir()) if f.is_file() and not f.name.startswith(".")]
            if not files:
                continue
            for f in files:
                z.write(f, f"{args.user}/{day.name}/{f.name}")
            count += 1
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(buf.getvalue())
    print(f"wrote {out} ({count} days, decrypted health data: handle with care)")
    return EXIT_OK


def cmd_fhir_map(args) -> int:
    from . import fhir as F
    store = store_from(args)
    users = [args.user] if args.user else sorted(p.name for p in (store.root / "users").iterdir() if p.is_dir()) \
        if (store.root / "users").is_dir() else []
    status = EXIT_OK
    with store.lock():
        for user in users:
            r = F.map_user(store, user, F.fhir_base(), force=args.force)
            print(f"{user}: mapped {r['mapped']} days, {r['skipped']} unchanged, {r['removed']} removed; "
                  f"{r['unmapped_samples']} samples with no correct FHIR form (kept in the day folders)")
            for e in r["errors"]:
                print(f"ERROR: {e}", file=sys.stderr)
                status = EXIT_PARTIAL
    return status


def cmd_fhir_load(args) -> int:
    from . import fhir as F
    store = store_from(args)
    url = args.url or os.environ.get("HKA_FHIR_URL")
    cid, secret = os.environ.get("HKA_FHIR_CLIENT_ID"), os.environ.get("HKA_FHIR_CLIENT_SECRET")
    if not url or not cid or not secret:
        raise ConfigError("set HKA_FHIR_URL (or --url), HKA_FHIR_CLIENT_ID and HKA_FHIR_CLIENT_SECRET")
    client = F.FhirClient(url, cid, secret)
    users_dir = store.root / "users"
    users = [args.user] if args.user else sorted(p.name for p in users_dir.iterdir() if p.is_dir()) if users_dir.is_dir() else []
    status = EXIT_OK
    with store.lock():
        for user in users:
            try:
                r = F.load_user(store, user, client, F.fhir_base())
            except F.LoadError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                status = EXIT_PARTIAL
                continue
            print(f"{user}: loaded {r['loaded']} batches, {r['skipped']} unchanged, {r['removed']} removed")
            for e in r["errors"]:
                print(f"ERROR: {e}", file=sys.stderr)
                status = EXIT_PARTIAL
    return status


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="hka", description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", help="default $HKA_DATA_DIR or ./data")
    ap.add_argument("--server", help="relay origin (stored by `hka --server … init`)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="create this processor's identity")
    p.add_argument("--name", help="name members see on the card (1-64 characters)")
    p.add_argument("--recovery", action="store_true", help="create an offline recovery identity instead")
    p.add_argument("--out", help="folder for --recovery")
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("card", help="show the processor card for members to scan")
    p.add_argument("--qr", action="store_true")
    p.set_defaults(func=cmd_card)
    p = sub.add_parser("confirm", help="pin a new device after comparing fingerprints with the member")
    p.add_argument("device")
    p.add_argument("--fingerprint", help="fingerprint read from the app (prompted when omitted)")
    p.set_defaults(func=cmd_confirm)
    sub.add_parser("pull", help="download and verify new batches").set_defaults(func=cmd_pull)
    sub.add_parser("build", help="decrypt verified batches into day folders").set_defaults(func=cmd_build)
    sub.add_parser("run", help="pull, then build, then back up (when a target is configured)").set_defaults(func=cmd_run)
    p = sub.add_parser("backup", help="upload final days, age-encrypted, to every configured cloud target",
                       description=K.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", action="append", choices=["aws", "r2", "gdrive", "rclone"],
                   help="only this target (repeatable)")
    p.set_defaults(func=cmd_backup)
    p = sub.add_parser("restore", help="download and decrypt a backup into a new folder")
    p.add_argument("--from", dest="source", required=True, choices=["aws", "r2", "gdrive", "rclone"])
    p.add_argument("--out", required=True, help="new or empty folder")
    p.add_argument("--user", help="one user (default: every user on the target)")
    p.add_argument("--identity", help="age identity file (default keys/age.txt; e.g. the recovery identity)")
    p.set_defaults(func=cmd_restore)
    p = sub.add_parser("status", help="devices, gaps, days")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)
    sub.add_parser("verify", help="re-check everything stored in the inbox").set_defaults(func=cmd_verify)
    fhir = sub.add_parser("fhir", help="FHIR R4 from the day folders").add_subparsers(dest="fhir_cmd", required=True)
    p = fhir.add_parser("map", help="write FHIR NDJSON for changed days")
    p.add_argument("--user")
    p.add_argument("--force", action="store_true", help="map every day again")
    p.set_defaults(func=cmd_fhir_map)
    p = fhir.add_parser("load", help="load mapped days into a FHIR server (Medplum, client credentials)")
    p.add_argument("--user")
    p.add_argument("--url", help="server base, e.g. http://localhost:8103 (default $HKA_FHIR_URL)")
    p.set_defaults(func=cmd_fhir_load)
    p = sub.add_parser("export", help="zip a user's decrypted day folders")
    p.add_argument("--user", required=True)
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_export)
    return ap


def load_env_file(path: Path) -> None:
    """Take HKA_* settings from a .env file (as written by medplum/setup.py). Variables already set win."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.strip().partition("=")
        key = key.strip()
        if sep and key.startswith("HKA_") and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)  # day folders and the index hold plaintext health data
    if argv is None:  # a real command line; callers that pass argv (tests) keep their own environment
        load_env_file(Path(".env"))
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Locked as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCKED
    except (ConfigError, FormatError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc!r}", file=sys.stderr)
        return EXIT_CONFIG
    except K.BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_PARTIAL
    except ApiError as exc:
        print(f"relay error: {exc}", file=sys.stderr)
        return EXIT_PARTIAL
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_PARTIAL


if __name__ == "__main__":
    sys.exit(main())
