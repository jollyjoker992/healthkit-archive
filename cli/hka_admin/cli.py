"""hka-admin: the relay owner's tool. Holds the admin key; never decrypts anything.

Commands: init, invite, users, retire, purge. The key lives in $HKA_ADMIN_DIR
(default ~/.config/hka-admin), separate from any processor's data directory.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hkacommon.client import ApiError, Client
from hkacommon.documents import ID_RE, ORIGIN_RE, make_invite, new_user_id
from hkacommon.encoding import FormatError, format_instant
from hkacommon.keys import SigningKey, fingerprint
from hkacommon.qr import print_qr

EXIT_OK, EXIT_FAIL, EXIT_CONFIG = 0, 1, 2


def admin_dir() -> Path:
    return Path(os.environ.get("HKA_ADMIN_DIR") or Path.home() / ".config" / "hka-admin")


class ConfigError(Exception):
    pass


def load(args) -> tuple[SigningKey, str]:
    d = admin_dir()
    key_path, conf_path = d / "admin.pem", d / "config.json"
    if not key_path.exists():
        raise ConfigError(f"no admin key in {d}; run `hka-admin init` first")
    conf = json.loads(conf_path.read_text()) if conf_path.exists() else {}
    server = getattr(args, "server", None) or os.environ.get("HKA_SERVER") or conf.get("server")
    if not server or not ORIGIN_RE.fullmatch(server):
        raise ConfigError("relay origin not set: pass --server https://… or run `hka-admin --server … init`")
    return SigningKey.load(key_path), server


def cmd_init(args) -> int:
    d = admin_dir()
    key_path, conf_path = d / "admin.pem", d / "config.json"
    if args.server and not ORIGIN_RE.fullmatch(args.server):
        raise ConfigError("--server must be https://host[:port] without a path")
    if key_path.exists():
        key = SigningKey.load(key_path)
        print(f"Admin key already exists at {key_path}.")
    else:
        key = SigningKey.generate()
        key.save(key_path)
        print(f"Created admin key {key_path} (mode 600).")
    if args.server:
        d.mkdir(parents=True, exist_ok=True)
        conf_path.write_text(json.dumps({"server": args.server}, indent=2) + "\n")
    print(f"\nadmin_id:    {key.id('a')}\nfingerprint: {fingerprint(key.pub)}")
    print("\nPut this in the Worker's ADMIN_KEYS variable (wrangler.jsonc):")
    print(json.dumps({key.id("a"): key.pub_b64u}))
    print("\nKeep a copy of admin.pem offline. Losing it only stops new enrolments; "
          "it cannot read anyone's data.")
    return EXIT_OK


def cmd_invite(args) -> int:
    key, server = load(args)
    if args.user and not ID_RE["u"].fullmatch(args.user):
        raise ConfigError("--user must look like u_ followed by 32 hex characters")
    if not 1 <= args.days <= 7:
        raise ConfigError("--days must be 1 to 7")
    user_id = args.user or new_user_id()
    # A margin below the relay's 7-day limit absorbs clock differences between this machine and the relay.
    exp = format_instant(datetime.now(timezone.utc) + timedelta(days=args.days) - timedelta(minutes=10))
    text = make_invite(key, server, user_id, args.name, exp)
    if not args.no_qr:
        print_qr(text)
    print(text)
    print(f"\nuser_id: {user_id}  ({'existing user, new device' if args.user else 'new user'})")
    print(f"expires: {exp}. Single use: one invite enrols one phone.")
    if not args.user:
        print(f"For this person's next phone: hka-admin invite --name {shlex.quote(args.name)} --user {user_id}")
    return EXIT_OK


def cmd_users(args) -> int:
    key, server = load(args)
    data = Client(server, key, "a").get("/v1/admin/users")
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return EXIT_OK
    for u in data["users"]:
        print(f"{u['user_id']}  {u['name']}  [{u['status']}]  since {u['created_at'][:10]}")
        for dv in u["devices"]:
            mb = dv["bytes"] / 1e6
            print(f"    {dv['device_id']}  {dv['name']}  [{dv['status']}]  seq {dv['contiguous_seq']}/{dv['max_seq']}  "
                  f"{dv['batches']} batches  {mb:.1f} MB  grant {dv['grant_seq']}")
    return EXIT_OK


def confirm(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    return input(f"{prompt} Type 'yes' to continue: ").strip() == "yes"


def cmd_retire(args) -> int:
    if not ID_RE["d"].fullmatch(args.device):
        raise ConfigError("DEVICE must look like d_ followed by 32 hex characters")
    key, server = load(args)
    if not confirm(f"Retire {args.device}? It will never upload again (its data stays readable).", args.yes):
        return EXIT_FAIL
    print(Client(server, key, "a").post(f"/v1/admin/devices/{args.device}/retire"))
    return EXIT_OK


def cmd_purge(args) -> int:
    if not ID_RE["u"].fullmatch(args.user):
        raise ConfigError("USER must look like u_ followed by 32 hex characters")
    key, server = load(args)
    if not confirm(f"Purge {args.user}? The relay stops serving all their data and deletes it; this cannot be undone.",
                   args.yes):
        return EXIT_FAIL
    print(Client(server, key, "a").delete(f"/v1/admin/users/{args.user}"))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="hka-admin", description=__doc__.split("\n")[0])
    ap.add_argument("--server", help="relay origin, e.g. https://hka-relay.example.workers.dev")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="create the admin key and print ADMIN_KEYS")
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("invite", help="print a single-use invite QR for a phone")
    p.add_argument("--name", required=True, help="person's name shown to processors (1-64 characters)")
    p.add_argument("--user", help="existing user_id, to enrol another phone for the same person")
    p.add_argument("--days", type=int, default=7, help="validity, 1-7 days (default 7)")
    p.add_argument("--no-qr", action="store_true")
    p.set_defaults(func=cmd_invite)
    p = sub.add_parser("users", help="list users, devices and storage")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_users)
    p = sub.add_parser("retire", help="stop a lost or replaced phone from uploading")
    p.add_argument("device")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_retire)
    p = sub.add_parser("purge", help="delete a user's data from the relay")
    p.add_argument("user")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_purge)
    return ap


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, FormatError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except ApiError as exc:
        print(f"relay error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except (KeyError, TypeError, ValueError) as exc:
        print(f"unexpected relay response: {exc!r}", file=sys.stderr)
        return EXIT_FAIL
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
