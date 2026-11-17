"""hka backup / restore: target configuration from the environment, and uploads through the real rclone
to a local folder (the vendor targets differ only in the rclone remote they define)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tarfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pyrage

from hka import backup as K
from hka import build as B
from hka import cli
from hka.store import ConfigError, Store, create_identity

from fakedevice import sample
from test_pull_build import HR, NOW, STEPS, Harness

LATER = NOW + timedelta(days=8)
R2_ACCOUNT = "0123456789abcdef0123456789abcdef"


class TargetsFromEnv(unittest.TestCase):
    def test_nothing_set_means_no_target(self):
        self.assertEqual(K.targets_from_env({}), {})

    def test_each_vendor_is_enabled_by_its_credentials(self):
        env = {"HKA_AWS_ACCESS_KEY_ID": "AKIAEXAMPLE", "HKA_AWS_SECRET_ACCESS_KEY": "s3cret",
               "HKA_AWS_BUCKET": "family-health/hka/", "HKA_AWS_STORAGE_CLASS": "DEEP_ARCHIVE",
               "HKA_R2_ACCOUNT_ID": R2_ACCOUNT, "HKA_R2_ACCESS_KEY_ID": "r2key",
               "HKA_R2_SECRET_ACCESS_KEY": "r2secret", "HKA_R2_BUCKET": "hka-backup",
               "HKA_GDRIVE_TOKEN": json.dumps({"access_token": "a", "refresh_token": "r"}),
               "HKA_GDRIVE_CLIENT_ID": "id.apps.googleusercontent.com", "HKA_GDRIVE_CLIENT_SECRET": "cs",
               "HKA_BACKUP_RCLONE_REMOTE": "nas:backups/hka/"}
        t = K.targets_from_env(env)
        self.assertEqual(sorted(t), ["aws", "gdrive", "r2", "rclone"])
        self.assertEqual(t["aws"].remote, "hkaaws:family-health/hka")
        self.assertEqual(t["aws"].env["REGION"], "us-east-1")
        self.assertEqual(t["aws"].archive_flags, ["--s3-storage-class", "DEEP_ARCHIVE"])
        self.assertTrue(t["aws"].cold)
        self.assertFalse(K.targets_from_env({**env, "HKA_AWS_STORAGE_CLASS": "STANDARD_IA"})["aws"].cold)
        self.assertEqual(t["r2"].remote, "hkar2:hka-backup")
        self.assertEqual(t["r2"].env["ENDPOINT"], f"https://{R2_ACCOUNT}.r2.cloudflarestorage.com")
        self.assertEqual(t["r2"].env["PROVIDER"], "Cloudflare")
        self.assertEqual(t["gdrive"].remote, "hkagdrive:healthkit-archive")
        self.assertEqual(t["rclone"].remote, "nas:backups/hka")
        self.assertFalse(t["rclone"].private_config)

    def test_a_half_configured_target_is_a_configuration_error(self):
        for env, needle in [({"HKA_AWS_ACCESS_KEY_ID": "k"}, "HKA_AWS_SECRET_ACCESS_KEY"),
                            ({"HKA_R2_BUCKET": "b"}, "HKA_R2_ACCOUNT_ID"),
                            ({"HKA_GDRIVE_TOKEN": "{}"}, "HKA_GDRIVE_CLIENT_ID")]:
            with self.assertRaises(ConfigError) as ctx:
                K.targets_from_env(env)
            self.assertIn(needle, str(ctx.exception))
        base = {"HKA_R2_ACCESS_KEY_ID": "k", "HKA_R2_SECRET_ACCESS_KEY": "s", "HKA_R2_BUCKET": "b"}
        with self.assertRaisesRegex(ConfigError, "account id"):
            K.targets_from_env({**base, "HKA_R2_ACCOUNT_ID": "my-account"})
        with self.assertRaisesRegex(ConfigError, "refresh_token"):
            K.targets_from_env({"HKA_GDRIVE_TOKEN": '{"access_token": "a"}', "HKA_GDRIVE_CLIENT_ID": "i",
                                "HKA_GDRIVE_CLIENT_SECRET": "s"})

    def test_vendor_remotes_live_in_the_environment_and_a_private_config(self):
        t = K.targets_from_env({"HKA_AWS_ACCESS_KEY_ID": "AKIAEXAMPLE", "HKA_AWS_SECRET_ACCESS_KEY": "s3cret",
                                "HKA_AWS_BUCKET": "b"})["aws"]
        with _tmpdir() as tmp, mock.patch("shutil.which", return_value="/usr/bin/rclone"), \
                mock.patch.dict(os.environ, {"RCLONE_CONFIG": "/home/me/.config/rclone/rclone.conf"}):
            store = Store(Path(tmp))
            env = K.Rclone(store).env(t)
            self.assertEqual(env["RCLONE_CONFIG_HKAAWS_TYPE"], "s3")
            self.assertEqual(env["RCLONE_CONFIG_HKAAWS_SECRET_ACCESS_KEY"], "s3cret")
            self.assertEqual(env["RCLONE_CONFIG"], str(store.root / "backup" / "rclone.conf"))
            own = K.Rclone(store).env(K.Target("rclone", "nas:x", private_config=False))
            self.assertEqual(own["RCLONE_CONFIG"], "/home/me/.config/rclone/rclone.conf")

    def test_settle_days_setting(self):
        self.assertEqual(B.settle_period({}), timedelta(days=7))
        self.assertEqual(B.settle_period({"HKA_SETTLE_DAYS": "14"}), timedelta(days=14))
        for bad in ("2", "0", "-5", "7.5", "a week", "366", "²5", "٧"):
            with self.assertRaisesRegex(ConfigError, "HKA_SETTLE_DAYS"):
                B.settle_period({"HKA_SETTLE_DAYS": bad})

    def test_backup_recipients_must_be_age_keys(self):
        with _tmpdir() as tmp:
            store = Store(Path(tmp))
            create_identity(store.keys_dir, "t", NOW)
            extra = pyrage.x25519.Identity.generate()
            self.assertEqual(len(K.recipients(store, {"HKA_BACKUP_RECIPIENTS": f" {extra.to_public()} "})), 2)
            with self.assertRaises(ConfigError):
                K.recipients(store, {"HKA_BACKUP_RECIPIENTS": "ssh-ed25519 AAAA"})


def _tmpdir():
    import tempfile
    return tempfile.TemporaryDirectory()


@unittest.skipUnless(shutil.which("rclone"), "rclone is not installed")
class BackupWithRclone(Harness):
    def setUp(self):
        super().setUp()
        self.remote = Path(self.tmp.name) / "cloud"
        self.target = K.Target("rclone", str(self.remote), private_config=False)
        self.pull()
        self.confirm()
        self.upload([sample(HR, "2026-11-10T01:00:00.000Z", value=61, unit="count/min"),
                     sample(STEPS, "2026-11-11T02:00:00.000Z", value=1234),
                     {"k": "profile", "date_of_birth": "1950-01-01"}])
        self.pull()
        self.build()
        self.build(LATER)  # both days final and archived
        self.udir = self.store.user_dir(self.user)

    def backup(self, now=LATER, env=None, target=None, rclone=None) -> K.BackupReport:
        return K.backup(self.store, {"rclone": target or self.target}, now,
                        K.preflight(self.store, env or {}, rclone=rclone))

    def remote_files(self) -> dict[str, bytes]:
        base = self.remote / "users" / self.user
        return {str(p.relative_to(base)): p.read_bytes() for p in base.rglob("*") if p.is_file()}

    def remote_days(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for rel in self.remote_files():
            if rel.startswith("archives/"):
                out.setdefault(Path(rel).name[:10], []).append(rel)
        return out

    def index(self) -> dict:
        return json.loads(pyrage.decrypt(self.remote_files()[K.MANIFEST], [self.me.age]))

    def update_hr(self, day: str, now=LATER):
        self.upload([sample(HR, f"{day}T05:00:00.000Z", value=90, unit="count/min")])
        self.pull(now)
        self.build(now)

    def test_uploads_only_ciphertext_and_nothing_twice(self):
        r = self.backup()
        self.assertEqual(r.errors, [])
        self.assertIn("uploaded 2 day archive(s), 1 extra file(s), manifest", r.lines[0])
        files = self.remote_files()
        self.assertEqual(sorted(self.remote_days()), ["2026-11-10", "2026-11-11"])
        self.assertTrue(any(f.startswith("extras/profile.ndjson.") for f in files))
        for data in files.values():
            self.assertTrue(data.startswith(b"age-encryption.org/v1\n"))
            self.assertNotIn(b"HKQuantityType", data)
        rel = self.index()["archives"]["2026-11-10"]["file"]
        self.assertEqual(pyrage.decrypt(files[rel], [self.me.age]),
                         (self.udir / "archives/2026/2026-11-10.tar.gz").read_bytes())
        self.assertEqual(self.backup(LATER + timedelta(hours=1)).lines, [])  # nothing changed, nothing sent
        st = K.status(self.store)[self.user]["rclone"]
        self.assertEqual((st["archives_uploaded"], st["archives_final"], st["up_to_date"]), (2, 2, True))

    def test_settle_period_decides_when_a_day_is_final_and_backed_up(self):
        self.update_hr("2026-11-20")
        with mock.patch.dict(os.environ, {"HKA_SETTLE_DAYS": "3"}):
            B.build(self.store, self.me, LATER + timedelta(days=2))
            self.backup(LATER + timedelta(days=2))
            self.assertNotIn("2026-11-20", self.remote_days())          # 2 days untouched: still provisional
            self.assertNotIn("2026-11-20", self.index()["archives"])
            B.build(self.store, self.me, LATER + timedelta(days=3))
            self.backup(LATER + timedelta(days=3))
        self.assertIn("2026-11-20", self.remote_days())

    def test_a_file_deleted_on_the_target_is_uploaded_again(self):
        self.backup()
        (self.remote / "users" / self.user / self.remote_days()["2026-11-10"][0]).unlink()
        r = self.backup(LATER + timedelta(hours=1))
        self.assertIn("uploaded 1 day archive(s)", r.lines[0])
        self.assertIn("2026-11-10", self.remote_days())

    def test_a_reopened_day_keeps_its_last_final_archive_until_it_is_final_again(self):
        self.backup()
        first = (self.udir / "archives/2026/2026-11-10.tar.gz").read_bytes()
        self.update_hr("2026-11-10")  # provisional again; its local archive is gone
        r = self.backup(LATER + timedelta(hours=1))
        self.assertEqual(r.errors, [])
        self.assertIn("uploaded 0 day archive(s), manifest", r.lines[0])  # only the index changed
        out = Path(self.tmp.name) / "restored"
        r = K.restore(self.store, self.target, out, None, self.user)
        self.assertEqual(r.errors, [])
        self.assertIn("restored 2 final day(s)", r.lines[-1])
        self.assertEqual((out / self.user / "archives/2026/2026-11-10.tar.gz").read_bytes(), first)
        # Final again: a new version under a new name; the old one stays, the index moves on.
        self.build(LATER + timedelta(days=8))
        self.assertIn("uploaded 1 day archive(s)", self.backup(LATER + timedelta(days=8)).lines[0])
        self.assertEqual(len(self.remote_days()["2026-11-10"]), 2)
        second = (self.udir / "archives/2026/2026-11-10.tar.gz").read_bytes()
        self.assertEqual(self.index()["archives"]["2026-11-10"]["sha256"], K.sha256_hex(second))

    def test_a_failed_copy_never_leaves_the_index_naming_content_the_target_lacks(self):
        self.backup()
        self.update_hr("2026-11-10")
        self.build(LATER + timedelta(days=8))  # a new final version of 2026-11-10

        class Failing(K.Rclone):
            def copy(self, target, src, dest, rels, flags=()):
                return "rclone exited 1: simulated" if rels != [K.MANIFEST] else super().copy(target, src, dest, rels, flags)
        r = self.backup(LATER + timedelta(days=8), rclone=Failing(self.store))
        self.assertTrue(any("1 file(s) not uploaded" in e for e in r.errors))
        entry = self.index()["archives"]["2026-11-10"]
        files = self.remote_files()
        self.assertIn(entry["file"], files)  # still the old version, which is there
        self.assertEqual(K.sha256_hex(pyrage.decrypt(files[entry["file"]], [self.me.age])), entry["sha256"])
        self.assertFalse(K.status(self.store)[self.user]["rclone"]["up_to_date"])
        self.assertEqual(self.backup(LATER + timedelta(days=8, hours=1)).errors, [])  # retried
        self.assertTrue(K.status(self.store)[self.user]["rclone"]["up_to_date"])

    def test_a_lost_or_corrupt_state_is_rebuilt_from_the_target(self):
        self.backup()
        for broken in ("{trunc", "[]", '{"users": []}', '{"users": {"u": {"targets": {"x": 1}}}}'):
            K.state_path(self.store).write_text(broken)
            self.assertEqual(K.status(self.store)[self.user], {})
            r = self.backup(LATER + timedelta(hours=1))
            self.assertEqual(r.errors, [])
            self.assertTrue(all("uploaded 0 day archive(s), manifest" in ln for ln in r.lines))  # adopted
            self.assertTrue(K.status(self.store)[self.user]["rclone"]["up_to_date"])

    def test_extra_files_that_are_gone_drop_out_of_the_index(self):
        (self.udir / "undated").mkdir()
        (self.udir / "undated" / "HKCategoryTypeIdentifierX.ndjson").write_text('{"k":"sample"}\n')
        self.backup()
        self.assertIn("undated/HKCategoryTypeIdentifierX.ndjson", self.index()["extras"])
        (self.udir / "undated" / "HKCategoryTypeIdentifierX.ndjson").unlink()
        self.backup(LATER + timedelta(hours=1))
        self.assertNotIn("undated/HKCategoryTypeIdentifierX.ndjson", self.index()["extras"])
        out = Path(self.tmp.name) / "restored"
        K.restore(self.store, self.target, out, None, self.user)
        self.assertFalse((out / self.user / "undated").exists())

    def test_a_missing_local_archive_is_an_error_only_while_no_target_holds_it(self):
        (self.udir / "archives/2026/2026-11-10.tar.gz").unlink()
        r = self.backup()
        self.assertTrue(any("2026-11-10: archive: missing locally and not yet on this target" in e for e in r.errors))
        self.build(LATER)
        self.assertNotIn("2026-11-10", self.remote_days())
        (self.udir / "archives/2026/2026-11-11.tar.gz").unlink()  # already on the target from the run above
        self.assertFalse(any("2026-11-11" in e for e in self.backup(LATER + timedelta(hours=1)).errors))

    def test_a_failing_target_records_nothing(self):
        broken = Path(self.tmp.name) / "rclone"
        broken.write_text("#!/bin/sh\necho 'ERROR : simulated' >&2\nexit 1\n")
        broken.chmod(0o755)
        r = self.backup(rclone=K.Rclone(self.store, str(broken)))
        self.assertTrue(any("cannot read the target" in e and "simulated" in e for e in r.errors))
        self.assertEqual(K.status(self.store)[self.user]["rclone"]["archives_uploaded"], 0)
        self.assertEqual(self.backup().errors, [])

    def test_new_recipients_get_a_new_copy(self):
        self.backup()
        recovery = pyrage.x25519.Identity.generate()
        r = self.backup(LATER + timedelta(hours=1), env={"HKA_BACKUP_RECIPIENTS": str(recovery.to_public())})
        self.assertIn("uploaded 2 day archive(s)", r.lines[0])
        key = Path(self.tmp.name) / "recovery.txt"
        key.write_text(f"{recovery}\n")
        r = K.restore(self.store, self.target, Path(self.tmp.name) / "restored", key, self.user)
        self.assertEqual(r.errors, [])
        self.assertIn("note: restoring with a key other than", r.lines[0])
        self.assertIn("restored 2 final day(s) and 1 extra file(s)", r.lines[-1])

    def test_restore_gives_back_the_day_folders(self):
        self.backup()
        out = Path(self.tmp.name) / "restored"
        r = K.restore(self.store, self.target, out, None, None)
        self.assertEqual(r.errors, [])
        self.assertIn("restored 2 final day(s)", r.lines[-1])
        for day in ("2026-11-10", "2026-11-11"):
            for f in (self.udir / "days" / day[:4] / day).iterdir():
                self.assertEqual((out / self.user / "days" / day[:4] / day / f.name).read_bytes(), f.read_bytes())
        self.assertEqual((out / self.user / "profile.ndjson").read_bytes(), (self.udir / "profile.ndjson").read_bytes())
        restored = json.loads((out / self.user / "restored.json").read_text())
        self.assertEqual(sorted(restored["days"]), ["2026-11-10", "2026-11-11"])
        self.assertFalse((out / self.user / "manifest.json").exists())
        with self.assertRaisesRegex(ConfigError, "not an empty folder"):
            K.restore(self.store, self.target, out, None, None)
        afile = Path(self.tmp.name) / "afile"
        afile.write_text("x")
        with self.assertRaisesRegex(ConfigError, "not an empty folder"):
            K.restore(self.store, self.target, afile, None, None)

    def restore_unverified(self, out: Path, user=None) -> K.BackupReport:
        """A restore that cannot check names (as with the recovery identity): the later checks must hold."""
        with mock.patch.object(Store, "identity", side_effect=ConfigError("no processor here")):
            return K.restore(self.store, self.target, out, Path(self.store.keys_dir / "age.txt"), user)

    def write_index(self, archives: dict, extras: dict | None = None):
        """Replace the target's manifest.json.age, as anyone who knows the public key could."""
        index = {"format": K.INDEX_FORMAT, "user_id": self.user, "archives": archives, "extras": extras or {},
                 "manifest": json.loads((self.udir / "manifest.json").read_text())}
        (self.remote / "users" / self.user / K.MANIFEST).write_bytes(
            pyrage.encrypt(json.dumps(index).encode(), [self.me.age.to_public()]))

    def test_restore_rejects_an_archive_that_escapes_its_day(self):
        self.backup()
        evil = io.BytesIO()
        with tarfile.open(fileobj=evil, mode="w:gz") as tar:
            info = tarfile.TarInfo("../../evil.txt")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        index = self.index()
        rel = index["archives"]["2026-11-10"]["file"]
        (self.remote / "users" / self.user / rel).write_bytes(pyrage.encrypt(evil.getvalue(), [self.me.age.to_public()]))
        index["archives"]["2026-11-10"]["sha256"] = K.sha256_hex(evil.getvalue())
        self.write_index(index["archives"])
        r = K.restore(self.store, self.target, Path(self.tmp.name) / "verified", None, self.user)
        self.assertTrue(any("not made by this processor" in e for e in r.errors))
        out = Path(self.tmp.name) / "restored"
        r = self.restore_unverified(out, self.user)
        self.assertTrue(any("2026-11-10" in e and "unexpected tar member" in e for e in r.errors))
        self.assertFalse((Path(self.tmp.name) / "evil.txt").exists())
        self.assertTrue((out / self.user / "days/2026/2026-11-11").is_dir())

    def test_restore_takes_no_path_from_the_index(self):
        self.backup()
        z = "0" * 64
        self.write_index({"../../../../outside": {"file": "x", "sha256": z},
                          "2026-11-11": {"file": "../../../../outside.age", "sha256": z},
                          "2026-11-10": {"file": "archives/2026/2026-11-11.0000000000000000.tar.gz.age", "sha256": z}},
                         {"../../outside": {"file": "extras/../../outside.0000000000000000.age", "sha256": z},
                          "profile.ndjson": {"file": "extras/../../../outside.0000000000000000.age", "sha256": z}})
        out = Path(self.tmp.name) / "restored"
        r = K.restore(self.store, self.target, out, None, self.user)
        self.assertEqual(len(r.errors), 5)
        self.assertFalse((Path(self.tmp.name) / "outside").exists())
        self.assertEqual(sorted(p.name for p in (out / self.user).iterdir()), ["restored.json"])

    def test_storage_class_applies_to_day_archives_only(self):
        log = Path(self.tmp.name) / "rclone.log"
        wrapper = Path(self.tmp.name) / "rclone-wrapper"
        wrapper.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexec {shutil.which("rclone")} "$@"\n')
        wrapper.chmod(0o755)
        target = K.Target("rclone", str(self.remote), private_config=False,
                          archive_flags=["--s3-storage-class", "DEEP_ARCHIVE"])
        r = self.backup(target=target, rclone=K.Rclone(self.store, str(wrapper)))
        self.assertEqual(r.errors, [])
        copies = [ln for ln in log.read_text().splitlines() if ln.startswith("copy ")]
        self.assertEqual(len(copies), 3)  # archives, extras, manifest
        self.assertEqual(["DEEP_ARCHIVE" in c for c in copies], [True, False, False])
        lists = [ln for ln in log.read_text().splitlines() if ln.startswith("lsjson ")]
        self.assertTrue(lists and all("--use-server-modtime" in ln for ln in lists))  # no HEAD per object

    def test_one_broken_user_does_not_stop_the_others(self):
        broken = self.store.root / "users" / "u_broken"
        broken.mkdir(parents=True)
        (broken / "manifest.json").write_text("{trunc")
        r = self.backup()
        self.assertTrue(any(e.startswith("u_broken:") for e in r.errors))
        self.assertIn("2026-11-10", self.remote_days())
        self.assertIn("error", K.status(self.store)["u_broken"])
        self.assertEqual(cli.main(["--data-dir", str(self.store.root), "status"]), 0)

    def test_a_lost_state_keeps_a_reopened_days_last_final_archive_in_the_index(self):
        self.backup()
        self.update_hr("2026-11-10")  # reopened: not among the items any more
        K.state_path(self.store).unlink()
        r = self.backup(LATER + timedelta(hours=1))
        self.assertEqual(r.errors, [])
        self.assertIn("2026-11-10", self.index()["archives"])
        self.assertEqual(self.backup(LATER + timedelta(hours=2)).lines, [])  # examined once, then quiet

    def test_damaged_state_entries_are_dropped_not_fatal(self):
        self.backup()
        state = json.loads(K.state_path(self.store).read_text())
        files = state["users"][self.user]["targets"]["rclone"]["files"]
        for i, rel in enumerate(list(files)):
            files[rel] = [None, {"what": "day:x"}, {"sha256": "z"}][i % 3]
        state["users"][self.user]["extras_cache"] = "nonsense"
        K.state_path(self.store).write_text(json.dumps(state))
        r = self.backup(LATER + timedelta(hours=1))
        self.assertEqual(r.errors, [])
        self.assertTrue(K.status(self.store)[self.user]["rclone"]["up_to_date"])

    def test_status_reads_state_without_changing_it(self):
        K.state_path(self.store).parent.mkdir(parents=True, exist_ok=True)
        K.state_path(self.store).write_text("{trunc")
        self.assertEqual(K.status(self.store)[self.user], {})
        self.assertEqual(K.state_path(self.store).read_text(), "{trunc")

    def test_remote_names_are_keyed_by_the_processors_secret(self):
        self.backup()
        sha = K.sha256_hex((self.udir / "profile.ndjson").read_bytes())
        checked = K.preflight(self.store, {})
        rel = next(f for f in self.remote_files() if f.startswith("extras/profile.ndjson."))
        self.assertEqual(rel, checked.remote_name(self.user, "extra:profile.ndjson", sha))
        unkeyed = K.sha256_hex(f"{sha}|{checked.recipients_key[:8]}".encode())[:16]
        self.assertNotIn(unkeyed, rel)
        other = create_identity(Path(self.tmp.name) / "other" / "keys", "other", NOW)
        self.assertNotEqual(checked.content_key(self.user, "extra:profile.ndjson", sha),
                            K.Checked(checked.rclone, checked.to, other.age, b"other")
                            .content_key(self.user, "extra:profile.ndjson", sha))

    def test_unchanged_extra_files_are_not_read_again(self):
        self.backup()
        reads = []
        real = Path.read_bytes

        def spy(path):
            reads.append(path.name)
            return real(path)
        with mock.patch.object(Path, "read_bytes", spy):
            self.backup(LATER + timedelta(hours=1))
        self.assertNotIn("profile.ndjson", reads)

    def test_restore_downloads_only_what_the_index_names(self):
        self.backup()
        self.update_hr("2026-11-10")
        self.build(LATER + timedelta(days=8))
        self.backup(LATER + timedelta(days=8))  # 2026-11-10 now has two versions on the target
        log = Path(self.tmp.name) / "rclone.log"
        wrapper = Path(self.tmp.name) / "rclone-wrapper"
        wrapper.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexec {shutil.which("rclone")} "$@"\n')
        wrapper.chmod(0o755)
        r = K.restore(self.store, self.target, Path(self.tmp.name) / "restored", None, self.user,
                      rclone=K.Rclone(self.store, str(wrapper)))
        self.assertEqual(r.errors, [])
        copies = [ln for ln in log.read_text().splitlines() if ln.startswith("copy ")]
        self.assertEqual(len(copies), 1)
        self.assertIn("--files-from", copies[0])
        self.assertEqual(len(self.remote_days()["2026-11-10"]), 2)
        self.assertEqual((Path(self.tmp.name) / "restored" / self.user / "archives/2026/2026-11-10.tar.gz").read_bytes(),
                         (self.udir / "archives/2026/2026-11-10.tar.gz").read_bytes())  # the newest version

    def test_a_day_that_fails_to_restore_leaves_nothing_behind(self):
        self.backup()
        dup = io.BytesIO()
        with tarfile.open(fileobj=dup, mode="w:gz") as tar:
            for _ in range(2):
                info = tarfile.TarInfo("2026-11-10/x.ndjson")
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))
        index = self.index()
        rel = index["archives"]["2026-11-10"]["file"]
        (self.remote / "users" / self.user / rel).write_bytes(pyrage.encrypt(dup.getvalue(), [self.me.age.to_public()]))
        index["archives"]["2026-11-10"]["sha256"] = K.sha256_hex(dup.getvalue())
        self.write_index(index["archives"], index["extras"])
        out = Path(self.tmp.name) / "restored"
        r = self.restore_unverified(out, self.user)
        self.assertTrue(any("unexpected tar member" in e for e in r.errors))
        self.assertEqual(sorted(p.name for p in (out / self.user / "days/2026").iterdir()), ["2026-11-11"])
        self.assertFalse((out / self.user / "archives/2026/2026-11-10.tar.gz").exists())

    def test_cli_restore_needs_no_lock_and_ignores_other_targets(self):
        self.backup()
        env = {"HKA_BACKUP_RCLONE_REMOTE": str(self.remote), "HKA_AWS_BUCKET": "leftover"}
        with mock.patch.dict(os.environ, env), self.store.lock():
            code = cli.main(["--data-dir", str(self.store.root), "restore", "--from", "rclone",
                             "--out", str(Path(self.tmp.name) / "restored")])
        self.assertEqual(code, 0)

    def test_an_extra_rewritten_within_one_mtime_tick_is_hashed_again(self):
        f = self.udir / "profile.ndjson"
        self.backup()
        st = f.stat()
        f.write_bytes(bytes(reversed(f.read_bytes())))  # same size
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))  # same mtime: the cache cannot tell
        self.assertEqual(self.backup(LATER + timedelta(hours=1)).errors, [])
        self.assertEqual(self.index()["extras"]["profile.ndjson"]["sha256"], K.sha256_hex(f.read_bytes()))
        # And if the file changes between hashing and upload, the stale hash is dropped and redone.
        state = json.loads(K.state_path(self.store).read_text())
        state["users"][self.user]["extras_cache"]["profile.ndjson"][4] = "0" * 64
        K.state_path(self.store).write_text(json.dumps(state))
        self.assertTrue(any("changed or damaged" in e for e in self.backup(LATER + timedelta(hours=2)).errors))
        self.assertEqual(self.backup(LATER + timedelta(hours=3)).errors, [])

    def test_file_names_build_can_write_are_backed_up_and_restored(self):
        (self.udir / "undated").mkdir()
        (self.udir / "undated" / "Café-x.ndjson").write_text('{"k":"sample"}\n')
        folder = Path(self.tmp.name) / "2026-11-10"
        folder.mkdir()
        (folder / "Café.ndjson").write_text("{}\n")
        self.assertEqual([n for n, _ in K._day_members(B.build_archive(folder, "2026-11-10"), "2026-11-10")],
                         ["Café.ndjson"])
        self.backup()
        out = Path(self.tmp.name) / "restored"
        self.assertEqual(K.restore(self.store, self.target, out, None, self.user).errors, [])
        self.assertTrue((out / self.user / "undated" / "Café-x.ndjson").is_file())
        for bad in ("../x", ".hidden", "a/b", ""):
            self.assertFalse(K.extra_path_ok(f"undated/{bad}"))

    def test_an_archive_uploaded_by_a_run_that_died_is_still_found(self):
        self.backup()
        self.update_hr("2026-11-10")
        self.build(LATER + timedelta(days=8))
        v2 = K.sha256_hex((self.udir / "archives/2026/2026-11-10.tar.gz").read_bytes())

        class IndexFails(K.Rclone):
            def copy(self, target, src, dest, rels, flags=()):
                return "killed" if rels == [K.MANIFEST] else super().copy(target, src, dest, rels, flags)
        self.backup(LATER + timedelta(days=8), rclone=IndexFails(self.store))
        K.state_path(self.store).unlink()  # the run died before saving its state
        self.update_hr("2026-11-10", LATER + timedelta(days=8))  # reopened again
        self.assertEqual(self.backup(LATER + timedelta(days=8, hours=1)).errors, [])
        self.assertEqual(self.index()["archives"]["2026-11-10"]["sha256"], v2)

    def test_an_unreadable_previous_index_does_not_block_the_backup(self):
        self.backup()
        (self.remote / "users" / self.user / K.MANIFEST).write_bytes(b"planted")
        K.state_path(self.store).unlink()
        self.assertEqual(self.backup(LATER + timedelta(hours=1)).errors, [])
        self.assertEqual(sorted(self.index()["archives"]), ["2026-11-10", "2026-11-11"])

    def test_an_unreadable_state_file_is_reported(self):
        K.state_path(self.store).mkdir(parents=True)
        r = self.backup()
        self.assertTrue(any("cannot read" in e for e in r.errors))

    def test_restore_bounds_what_it_inflates(self):
        big = io.BytesIO()
        with tarfile.open(fileobj=big, mode="w:gz") as tar:
            info = tarfile.TarInfo("2026-11-10/x.ndjson")
            info.size = 5000
            tar.addfile(info, io.BytesIO(b"0" * 5000))
        with mock.patch.object(K, "MAX_ARCHIVE_PLAINTEXT", 4096), self.assertRaisesRegex(ValueError, "larger"):
            K._day_members(big.getvalue(), "2026-11-10")

    def test_restore_does_not_count_provisional_days_as_missing(self):
        self.update_hr("2026-11-20")
        self.backup()
        r = K.restore(self.store, self.target, Path(self.tmp.name) / "restored", None, self.user)
        self.assertEqual(r.errors, [])
        self.assertNotIn("not in the backup", r.lines[0])

    def reopened_with_lost_state(self):
        """2026-11-10 backed up, then reopened, and state.json lost: only the target knows its archive."""
        self.backup()
        self.update_hr("2026-11-10")
        K.state_path(self.store).unlink()
        (self.remote / "users" / self.user / K.MANIFEST).unlink()  # and no previous index to read
        return self.remote_days()["2026-11-10"][0]

    def test_a_failed_download_during_recovery_is_retried_not_ignored(self):
        rel = self.reopened_with_lost_state()

        class Flaky(K.Rclone):
            def copy_from(self, *a, **kw):
                return "rclone exited 1: network"
        r = self.backup(LATER + timedelta(hours=1), rclone=Flaky(self.store))
        self.assertTrue(any("cannot read 1 unknown file(s)" in e for e in r.errors))
        self.assertNotIn("2026-11-10", self.index()["archives"])
        self.assertFalse(K.status(self.store)[self.user]["rclone"]["up_to_date"])  # status agrees with the run
        self.assertEqual(self.backup(LATER + timedelta(hours=2)).errors, [])
        self.assertEqual(self.index()["archives"]["2026-11-10"]["file"], rel)

    def test_a_planted_newer_version_is_never_named(self):
        real = self.reopened_with_lost_state()
        evil = io.BytesIO()
        with tarfile.open(fileobj=evil, mode="w:gz") as tar:
            info = tarfile.TarInfo("2026-11-10/HKQuantityTypeIdentifierHeartRate.ndjson")
            info.size = 2
            tar.addfile(info, io.BytesIO(b"{}"))
        planted = self.remote / "users" / self.user / "archives/2026/2026-11-10.ffffffffffffffff.ffffffff.tar.gz.age"
        planted.write_bytes(pyrage.encrypt(evil.getvalue(), [self.me.age.to_public()]))
        os.utime(planted, (2_000_000_000, 2_000_000_000))  # "newer" than the real one
        self.assertEqual(self.backup(LATER + timedelta(hours=1)).errors, [])
        self.assertEqual(self.index()["archives"]["2026-11-10"]["file"], real)
        state = json.loads(K.state_path(self.store).read_text())
        self.assertIn(str(planted.relative_to(self.remote / "users" / self.user)),
                      state["users"][self.user]["targets"]["rclone"]["ignored"])

    def test_a_file_that_keeps_failing_to_download_is_never_given_up(self):
        rel = self.reopened_with_lost_state()

        class Flaky(K.Rclone):
            def copy_from(self, *a, **kw):
                return "rclone exited 1: rateLimitExceeded"
        for n in range(5):
            self.assertTrue(self.backup(LATER + timedelta(hours=n + 1), rclone=Flaky(self.store)).errors)
        self.assertEqual(self.backup(LATER + timedelta(hours=9)).errors, [])
        self.assertEqual(self.index()["archives"]["2026-11-10"]["file"], rel)

    def test_a_copy_under_another_users_or_days_name_is_not_accepted(self):
        self.reopened_with_lost_state()
        base = self.remote / "users" / self.user
        src = base / self.remote_days()["2026-11-11"][0]
        copied = base / "archives/2026" / src.name.replace("2026-11-11", "2026-11-10")
        shutil.copyfile(src, copied)  # the provider's copy, a valid file of this processor
        os.utime(copied, (2_000_000_000, 2_000_000_000))
        self.assertEqual(self.backup(LATER + timedelta(hours=1)).errors, [])
        self.assertNotEqual(self.index()["archives"]["2026-11-10"]["file"], str(copied.relative_to(base)))

    def test_cold_archives_are_not_downloaded_to_recover_state(self):
        self.reopened_with_lost_state()
        cold = K.Target("rclone", str(self.remote), private_config=False,
                        archive_flags=["--s3-storage-class", "GLACIER"], cold=True)

        class NoDownload(K.Rclone):
            def copy_from(self, *a, **kw):
                raise AssertionError("downloaded from cold storage")
        self.assertEqual(self.backup(LATER + timedelta(hours=1), target=cold, rclone=NoDownload(self.store)).errors, [])

    def test_older_versions_are_re_encrypted_for_new_recipients(self):
        self.backup()
        old = self.remote_days()["2026-11-10"][0]
        self.update_hr("2026-11-10")  # reopened: its last final archive exists only under the old recipients
        recovery = pyrage.x25519.Identity.generate()
        env = {"HKA_BACKUP_RECIPIENTS": str(recovery.to_public())}
        self.backup(LATER + timedelta(hours=1), env=env)
        # Its last final archive was re-encrypted for the new recipients: the recovery key opens it.
        rel = self.index()["archives"]["2026-11-10"]["file"]
        self.assertNotEqual(rel, old)
        self.assertEqual(pyrage.decrypt(self.remote_files()[rel], [recovery]),
                         pyrage.decrypt(self.remote_files()[old], [self.me.age]))
        K.state_path(self.store).unlink()  # and a lost state still finds both, choosing the newest
        self.assertEqual(self.backup(LATER + timedelta(hours=2), env=env).errors, [])
        self.assertEqual(self.index()["archives"]["2026-11-10"]["file"], rel)

    def test_an_empty_day_archive_restores(self):
        self.backup()
        empty = io.BytesIO()
        with tarfile.open(fileobj=empty, mode="w:gz"):
            pass
        index = self.index()
        rel = index["archives"]["2026-11-10"]["file"]
        (self.remote / "users" / self.user / rel).write_bytes(pyrage.encrypt(empty.getvalue(), [self.me.age.to_public()]))
        index["archives"]["2026-11-10"]["sha256"] = K.sha256_hex(empty.getvalue())
        self.write_index(index["archives"], index["extras"])
        out = Path(self.tmp.name) / "restored"
        self.assertEqual(self.restore_unverified(out, self.user).errors, [])
        self.assertTrue((out / self.user / "days/2026/2026-11-10").is_dir())

    def test_a_truncated_download_is_retried_not_ignored(self):
        rel = self.reopened_with_lost_state()

        class Truncating(K.Rclone):
            def copy_from(self, target, src, dest, rels):
                err = super().copy_from(target, src, dest, rels)
                for r in rels:
                    p = dest / r
                    p.write_bytes(p.read_bytes()[:50])  # killed half way, no .partial name
                return err
        self.assertTrue(self.backup(LATER + timedelta(hours=1), rclone=Truncating(self.store)).errors)
        self.assertEqual(self.backup(LATER + timedelta(hours=2)).errors, [])
        self.assertEqual(self.index()["archives"]["2026-11-10"]["file"], rel)

    def test_objects_in_glacier_are_not_downloaded_whatever_the_setting(self):
        self.reopened_with_lost_state()

        class Lifecycle(K.Rclone):  # a bucket rule moved the archives to Deep Archive
            def listing(self, target, path):
                return {r: l._replace(tier="DEEP_ARCHIVE") if r.startswith("archives/") else l
                        for r, l in super().listing(target, path).items()}

            def copy_from(self, target, src, dest, rels):
                assert not any(r.startswith("archives/") for r in rels), "downloaded from Glacier"
                return super().copy_from(target, src, dest, rels)
        self.assertEqual(self.backup(LATER + timedelta(hours=1), rclone=Lifecycle(self.store)).errors, [])

    def test_switching_recipients_back_never_overwrites(self):
        self.backup()
        self.update_hr("2026-11-10")
        first = self.remote_days()["2026-11-10"][0]
        before = self.remote_files()[first]
        recovery = pyrage.x25519.Identity.generate()
        self.backup(LATER + timedelta(hours=1), env={"HKA_BACKUP_RECIPIENTS": str(recovery.to_public())})
        self.assertEqual(self.backup(LATER + timedelta(hours=2)).errors, [])  # back to the first recipients
        self.assertEqual(self.index()["archives"]["2026-11-10"]["file"], first)
        self.assertEqual(self.remote_files()[first], before)

    def test_switching_recipients_back_keeps_naming_the_newest_content(self):
        recovery = pyrage.x25519.Identity.generate()
        with_recovery = {"HKA_BACKUP_RECIPIENTS": str(recovery.to_public())}
        self.backup()                                            # A for the processor key only
        self.update_hr("2026-11-10")
        self.build(LATER + timedelta(days=8))                    # B, final
        self.backup(LATER + timedelta(days=8), env=with_recovery)
        b = K.sha256_hex((self.udir / "archives/2026/2026-11-10.tar.gz").read_bytes())
        self.update_hr("2026-11-10", LATER + timedelta(days=8))  # reopened again
        self.assertEqual(self.backup(LATER + timedelta(days=8, hours=1)).errors, [])  # recovery key removed
        self.assertEqual(self.index()["archives"]["2026-11-10"]["sha256"], b)
        K.state_path(self.store).unlink()  # the index keeps the content's age across a lost state
        self.assertEqual(self.backup(LATER + timedelta(days=8, hours=2)).errors, [])
        self.assertEqual(self.index()["archives"]["2026-11-10"]["sha256"], b)

    def test_cold_objects_left_unchecked_are_reported(self):
        self.reopened_with_lost_state()

        class Lifecycle(K.Rclone):
            def listing(self, target, path):
                return {r: l._replace(tier="GLACIER") if r.startswith("archives/") else l
                        for r, l in super().listing(target, path).items()}
        r = self.backup(LATER + timedelta(hours=1), rclone=Lifecycle(self.store))
        self.assertTrue(any("in Glacier are not known" in ln for ln in r.lines))
        self.assertEqual(K.status(self.store)[self.user]["rclone"]["unchecked_cold"], 1)

    def test_rekey_rewrites_a_name_found_unreadable_instead_of_adopting_it(self):
        self.backup()
        a = K.sha256_hex((self.udir / "archives/2026/2026-11-10.tar.gz").read_bytes())
        self.update_hr("2026-11-10")  # reopened: only its last final archive, for the first recipients
        recovery = pyrage.x25519.Identity.generate()
        env = {"HKA_BACKUP_RECIPIENTS": str(recovery.to_public())}
        new = K.preflight(self.store, env).remote_name(self.user, "day:2026-11-10", a)
        (self.remote / "users" / self.user / new).write_bytes(b"not an age file")  # someone got there first
        K.state_path(self.store).unlink()
        self.assertEqual(self.backup(LATER + timedelta(hours=1), env=env).errors, [])
        self.assertEqual(self.index()["archives"]["2026-11-10"]["file"], new)
        self.assertEqual(K.sha256_hex(pyrage.decrypt(self.remote_files()[new], [recovery])), a)
        state = json.loads(K.state_path(self.store).read_text())
        self.assertNotIn(new, state["users"][self.user]["targets"]["rclone"]["ignored"])

    def test_an_index_replaced_on_the_target_is_uploaded_again(self):
        self.backup()
        path = self.remote / "users" / self.user / K.MANIFEST
        good = self.index()
        self.write_index({})  # planted: names nothing, another size
        r = self.backup(LATER + timedelta(hours=1))
        self.assertTrue(any("uploading it again" in ln for ln in r.lines))
        self.assertEqual(self.index()["archives"], good["archives"])
        self.assertEqual(self.backup(LATER + timedelta(hours=2)).lines, [])  # its mtime is recorded here
        os.utime(path, (1_700_000_000, 1_700_000_000))  # same size, written at another time
        r = self.backup(LATER + timedelta(hours=3))
        self.assertTrue(any("uploading it again" in ln for ln in r.lines))
        self.assertEqual(self.backup(LATER + timedelta(hours=4)).lines, [])

    def test_a_forged_since_in_the_index_cannot_roll_a_day_back(self):
        self.backup()
        old = self.index()["archives"]["2026-11-10"]
        self.update_hr("2026-11-10")
        self.build(LATER + timedelta(days=8))
        self.backup(LATER + timedelta(days=8))  # the newer final version
        b = K.sha256_hex((self.udir / "archives/2026/2026-11-10.tar.gz").read_bytes())
        self.update_hr("2026-11-10", LATER + timedelta(days=8))  # reopened again
        for since in ("9999-01-01T00:00:00.000Z", "zzzz", 7):
            self.write_index({"2026-11-10": {**old, "since": since}})
            K.state_path(self.store).unlink()
            self.assertEqual(self.backup(LATER + timedelta(days=8, hours=1)).errors, [])
            self.assertEqual(self.index()["archives"]["2026-11-10"]["sha256"], b)

    def test_content_that_comes_back_is_the_newest_again(self):
        a = K.Item("day:2026-11-10", "archives/2026/a", "a" * 64, None)
        files = {"archives/2026/a": {"what": "day:2026-11-10", "sha256": "a" * 64, "since": "2026-11-01T00:00:00.000Z"},
                 "archives/2026/a2": {"what": "day:2026-11-10", "sha256": "a" * 64, "since": "2026-11-01T00:00:00.000Z"},
                 "archives/2026/b": {"what": "day:2026-11-10", "sha256": "b" * 64, "since": "2026-11-05T00:00:00.000Z"}}
        K.mark_current_newest([a], files, LATER)
        self.assertGreater(files["archives/2026/a"]["since"], files["archives/2026/b"]["since"])
        again = dict(files["archives/2026/a"])
        K.mark_current_newest([a], files, LATER + timedelta(days=1))  # a copy of the same content is no rival
        self.assertEqual(files["archives/2026/a"], again)

    def test_status_text_shows_unchecked_glacier_files(self):
        self.backup()
        state = json.loads(K.state_path(self.store).read_text())
        state["users"][self.user]["targets"]["rclone"]["unchecked_cold"] = 3
        K.state_path(self.store).write_text(json.dumps(state))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HKA_BACKUP_RCLONE_REMOTE": str(self.remote)}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--data-dir", str(self.store.root), "status"]), 0)
        self.assertIn("3 file(s) in Glacier not checked", out.getvalue())

    def test_restore_refuses_an_oversized_download(self):
        self.backup()
        with mock.patch.object(K, "MAX_DOWNLOAD", 100):
            r = K.restore(self.store, self.target, Path(self.tmp.name) / "restored", None, self.user)
        self.assertTrue(any("larger than" in e for e in r.errors))

    def test_damaged_ignored_list_and_odd_names(self):
        self.backup()
        state = json.loads(K.state_path(self.store).read_text())
        state["users"][self.user]["targets"]["rclone"]["ignored"] = [["x"], 3, "ok"]
        K.state_path(self.store).write_text(json.dumps(state))
        self.assertEqual(self.backup(LATER + timedelta(hours=1)).errors, [])
        for bad in ("a\nb.ndjson", "x\x7f", "tab\there"):
            self.assertFalse(K.file_name_ok(bad))

    def test_cli_backup_target_ignores_other_half_configured_targets(self):
        env = {"HKA_BACKUP_RCLONE_REMOTE": str(self.remote), "HKA_GDRIVE_TOKEN": "{}"}
        with mock.patch.dict(os.environ, env), mock.patch("hka.cli.now_utc", return_value=LATER):
            self.assertEqual(cli.main(["--data-dir", str(self.store.root), "backup", "--target", "rclone"]), 0)
            self.assertEqual(cli.main(["--data-dir", str(self.store.root), "backup"]), 2)

    def test_cli_backup_and_status(self):
        args = ["--data-dir", str(self.store.root)]
        with mock.patch.dict(os.environ, {"HKA_BACKUP_RCLONE_REMOTE": str(self.remote)}), \
                mock.patch("hka.cli.now_utc", return_value=LATER):
            self.assertEqual(cli.main(args + ["backup"]), 0)
            data = cli.status_data(self.store)
        self.assertEqual(data["backup_targets"], ["rclone"])
        self.assertTrue(data["users"][0]["backup"]["rclone"]["up_to_date"])
        self.assertEqual(data["settle_days"], 7)
        with mock.patch.dict(os.environ, {"HKA_AWS_ACCESS_KEY_ID": "k"}):
            self.assertEqual(cli.main(args + ["backup"]), 2)
        for bad in ("1", "²5"):
            with mock.patch.dict(os.environ, {"HKA_SETTLE_DAYS": bad}):
                self.assertEqual(cli.main(args + ["--server", "https://relay.example", "run"]), 2)
        # A bad backup setting still lets the run pull and build; only the backup is skipped (exit 2).
        bad = [{"HKA_BACKUP_RCLONE_REMOTE": str(self.remote), "HKA_BACKUP_RECIPIENTS": "age1nope"},
               {"HKA_BACKUP_RCLONE_REMOTE": str(self.remote), "HKA_RCLONE_BIN": "/nonexistent/rclone"},
               {"HKA_R2_BUCKET": "half-configured"}]
        for env in bad:
            with mock.patch.dict(os.environ, env), mock.patch("hka.cli.run_pull", return_value=0) as pulled, \
                    mock.patch("hka.cli.run_build", return_value=0) as built, \
                    mock.patch("hka.cli.run_backup", side_effect=AssertionError("backed up")):
                self.assertEqual(cli.main(args + ["--server", "https://relay.example", "run"]), 2)
            pulled.assert_called_once()
            built.assert_called_once()
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("HKA_AWS", "HKA_R2", "HKA_GDRIVE", "HKA_BACKUP"))}
        with mock.patch.dict(os.environ, clean, clear=True):
            self.assertEqual(cli.main(args + ["backup"]), 2)


if __name__ == "__main__":
    unittest.main()
