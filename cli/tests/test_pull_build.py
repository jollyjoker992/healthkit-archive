"""pull and build against an in-memory relay and a software device."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hka import build as B
from hka import pull as P
from hka.store import Store, create_identity
from hkacommon.documents import new_user_id
from hkacommon.encoding import b64u_encode, compact_json, sha256_hex
from hkacommon.keys import fingerprint

from fakedevice import FakeDevice, sample
from fakerelay import FakeRelay, Stream

NOW = datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc)
HR = "HKQuantityTypeIdentifierHeartRate"
STEPS = "HKQuantityTypeIdentifierStepCount"
SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.me = create_identity(self.store.keys_dir, "Test Mac", NOW)
        self.relay = FakeRelay()
        self.user = new_user_id()
        self.dev = FakeDevice(self.user)
        self.relay.streams[self.dev.device_id] = Stream(self.dev.key.pub_b64u, self.user)
        self.relay.add_grant(self.dev.device_id, *self.dev.make_grant([self.me.card]))

    def tearDown(self):
        self.tmp.cleanup()

    def pull(self, now=NOW) -> P.Report:
        return P.pull(self.store, self.me, self.relay, now)

    def confirm(self):
        P.confirm(self.store, self.dev.device_id, fingerprint(self.dev.key.pub), NOW)

    def upload(self, records, **kw):
        sealed = self.dev.seal(records)
        self.relay.add_batch(self.dev.device_id, sealed, **kw)
        return sealed

    def build(self, now=NOW) -> B.BuildReport:
        return B.build(self.store, self.me, now)

    def day_records(self, day: str, type_: str) -> list[dict]:
        p = self.store.user_dir(self.user) / "days" / day[:4] / day / f"{type_}.ndjson"
        return [json.loads(line) for line in B.read_lines(p)]

    def state(self) -> dict:
        return json.loads((self.store.device_dir(self.user, self.dev.device_id) / "state.json").read_text())


class Pinning(Harness):
    def test_new_device_is_pending_until_confirmed(self):
        self.upload([sample(HR, "2026-11-12T01:00:00.000Z", value=60, unit="count/min")])
        r = self.pull()
        self.assertIn("hka confirm", r.lines[0])
        self.assertFalse((self.store.root / "inbox").exists())
        with self.assertRaises(ValueError):
            P.confirm(self.store, self.dev.device_id, "0000-0000-0000-0000-0000", NOW)
        self.confirm()
        r = self.pull()
        self.assertEqual(r.alarms, [])
        self.assertEqual(self.state()["verified_seq"], 1)

    def test_relay_swapping_the_key_after_pinning_is_an_alarm(self):
        self.pull()
        self.confirm()
        self.relay.streams[self.dev.device_id].device_pub = FakeDevice(self.user).key.pub_b64u
        alarms = self.pull().alarms
        self.assertTrue(any("pairs this device id with another key" in a for a in alarms))
        self.assertTrue(any("no longer lists this device" in a for a in alarms))


class Verification(Harness):
    def setUp(self):
        super().setUp()
        self.pull()
        self.confirm()

    def test_out_of_order_arrival_waits_at_the_gap(self):
        s1 = self.dev.seal([sample(HR, "2026-11-12T01:00:00.000Z", value=60)])
        s2 = self.dev.seal([sample(HR, "2026-11-12T02:00:00.000Z", value=61)])
        self.relay.add_batch(self.dev.device_id, s2)
        self.assertEqual(self.pull().alarms, [])
        self.assertEqual(self.state()["verified_seq"], 0)
        self.assertIn("gap_since", self.state())
        self.assertTrue(any("stuck at seq 0" in a for a in self.pull(NOW + timedelta(hours=25)).alarms))
        self.relay.add_batch(self.dev.device_id, s1)
        self.assertEqual(self.pull().alarms, [])
        self.assertEqual(self.state()["verified_seq"], 2)
        self.assertNotIn("gap_since", self.state())

    def test_broken_chain_is_an_alarm(self):
        self.upload([sample(HR, "2026-11-12T01:00:00.000Z")])
        self.dev.prev = "0" * 64  # a relay cannot do this; a buggy device or a forged history can
        self.upload([sample(HR, "2026-11-12T02:00:00.000Z")])
        self.assertTrue(any("hash chain broken at seq 2" in a for a in self.pull().alarms))
        self.assertEqual(self.state()["verified_seq"], 1)

    def test_manifest_not_signed_by_the_pinned_key(self):
        seq, manifest, _sig, ct = self.dev.seal([sample(HR, "2026-11-12T01:00:00.000Z")])
        other = FakeDevice(self.user)
        self.relay.add_batch(self.dev.device_id, (seq, manifest, other.key.sign("manifest", manifest), ct))
        self.assertTrue(any("not signed by the pinned" in a for a in self.pull().alarms))

    def test_tampered_ciphertext(self):
        seq, manifest, sig, ct = self.dev.seal([sample(HR, "2026-11-12T01:00:00.000Z")])
        self.relay.add_batch(self.dev.device_id, (seq, manifest, sig, ct[:-1] + bytes([ct[-1] ^ 1])))
        self.assertTrue(any("does not match its manifest" in a for a in self.pull().alarms))

    def test_grant_listing_this_processor_with_another_age_key(self):
        from hkacommon.documents import Card
        evil = Card(self.me.name, str(__import__("pyrage").x25519.Identity.generate().to_public()), self.me.sign.pub_b64u)
        self.relay.add_grant(self.dev.device_id, *self.dev.make_grant([evil]))
        self.assertTrue(any("different key" in a for a in self.pull().alarms))

    def test_signed_removal_versus_silent_loss_of_access(self):
        self.upload([sample(HR, "2026-11-12T01:00:00.000Z")])
        self.pull()
        stream = self.relay.streams[self.dev.device_id]
        stream.granted = False
        self.assertTrue(any("without a device-signed grant" in a for a in self.pull().alarms))
        other = create_identity(Path(self.tmp.name) / "other", "Other", NOW)
        self.relay.add_grant(self.dev.device_id, *self.dev.make_grant([other.card]))
        r = self.pull()
        self.assertEqual(r.alarms, [])
        self.assertTrue(any("removed as a processor" in line for line in r.lines))

    def test_batches_sealed_before_this_processor_was_added_are_verified_but_skipped(self):
        other = create_identity(Path(self.tmp.name) / "other", "Other", NOW)
        stream = self.relay.streams[self.dev.device_id]
        stream.grants.clear()
        self.dev.grant_seq = 0
        self.relay.add_grant(self.dev.device_id, *self.dev.make_grant([other.card]))
        self.upload([sample(HR, "2026-11-12T01:00:00.000Z")])
        self.relay.add_grant(self.dev.device_id, *self.dev.make_grant([other.card, self.me.card]))
        self.upload([sample(HR, "2026-11-12T02:00:00.000Z", value=70)])
        self.assertEqual(self.pull().alarms, [])
        self.assertEqual(self.state()["verified_seq"], 2)
        r = self.build()
        self.assertEqual((r.batches, r.skipped_not_recipient), (1, 1))

    def test_silence_alarm(self):
        self.upload([sample(HR, "2026-11-12T01:00:00.000Z")])
        self.pull()
        self.assertTrue(any("no new verified batch since" in a for a in self.pull(NOW + timedelta(days=4)).alarms))


class Build(Harness):
    def setUp(self):
        super().setUp()
        self.pull()
        self.confirm()

    def sync(self, records, now=NOW):
        from hkacommon.encoding import format_instant
        self.upload(records, received_at=format_instant(now))
        self.assertEqual(self.pull(now).alarms, [])
        r = self.build(now)
        self.assertEqual(r.errors, [])
        return r

    def test_days_follow_local_time_sleep_by_end_and_record_tz(self):
        recs = [
            sample(HR, "2026-11-12T17:30:00.000Z", value=55, unit="count/min"),   # 00:30 on the 13th in UTC+7
            {**sample(SLEEP, "2026-11-12T15:00:00.000Z", "2026-11-12T23:00:00.000Z", value=4), "kind": "category",
             "value_name": "asleepDeep"},                                        # ends 06:00 on the 13th
            sample(STEPS, "2026-11-12T17:30:00.000Z", value=10,
                   metadata={"HKMetadataKeyTimeZone": "Europe/London"}),         # 17:30 on the 12th in London
        ]
        self.sync(recs)
        self.assertEqual(len(self.day_records("2026-11-13", HR)), 1)
        self.assertEqual(len(self.day_records("2026-11-13", SLEEP)), 1)
        self.assertEqual(len(self.day_records("2026-11-12", STEPS)), 1)

    def test_duplicates_across_batches_and_devices_are_dropped(self):
        rec = sample(HR, "2026-11-12T01:00:00.000Z", value=60)
        self.sync([rec, rec])
        self.sync([rec])
        self.assertEqual(len(self.day_records("2026-11-12", HR)), 1)

    def test_deletion_removes_and_suppresses(self):
        rec = sample(HR, "2026-11-12T01:00:00.000Z", value=60)
        self.sync([rec, sample(HR, "2026-11-12T02:00:00.000Z", value=61)])
        self.sync([{"k": "deleted", "uuid": rec["uuid"], "type": HR, "observed_at": "2026-11-13T07:00:00.000Z"}])
        self.assertEqual([r["value"] for r in self.day_records("2026-11-12", HR)], [61])
        self.sync([rec])  # a late copy of a deleted uuid is ignored
        self.assertEqual(len(self.day_records("2026-11-12", HR)), 1)

    def test_route_goes_to_its_workout_day(self):
        w = {"k": "workout", "type": "HKWorkoutTypeIdentifier", "uuid": "W1", "start": "2026-11-12T16:50:00.000Z",
             "end": "2026-11-12T17:20:00.000Z", "activity_type": 79, "activity_name": "pickleball", "duration_s": 1800,
             "stats": {}, "events": [], "activities": [], "source": {"bundle_id": "x", "name": "x"}, "device": None,
             "metadata": {}}
        route = {"k": "route", "uuid": "R1", "workout_uuid": "W1", "part": 0, "parts": 1,
                 "start": "2026-11-12T17:05:00.000Z", "end": "2026-11-12T17:20:00.000Z", "points": []}
        self.sync([route, w])  # route before its workout in the batch
        self.assertEqual(len(self.day_records("2026-11-12", "HKWorkoutTypeIdentifier")), 1)
        self.assertEqual(len(self.day_records("2026-11-12", "HKWorkoutRoute")), 1)
        summary = json.loads((self.store.user_dir(self.user) / "days/2026/2026-11-12/summary.json").read_text())
        self.assertEqual(summary["workouts"][0]["activity_name"], "pickleball")

    def test_summary_picks_one_source_per_total(self):
        watch = {"bundle_id": "com.apple.health.WATCH", "name": "Watch"}
        phone = {"bundle_id": "com.apple.health.PHONE", "name": "iPhone"}
        self.sync([sample(STEPS, "2026-11-12T01:00:00.000Z", value=4000, source=watch),
                   sample(STEPS, "2026-11-12T02:00:00.000Z", value=3000, source=phone),
                   sample(STEPS, "2026-11-12T03:00:00.000Z", value=1000, source=watch),
                   sample(HR, "2026-11-12T01:00:00.000Z", value=50, unit="count/min"),
                   sample(HR, "2026-11-12T02:00:00.000Z", value=90, unit="count/min")])
        s = json.loads((self.store.user_dir(self.user) / "days/2026/2026-11-12/summary.json").read_text())
        self.assertEqual(s["totals"][STEPS]["value"], 5000)
        self.assertEqual(s["totals"][STEPS]["source"], "com.apple.health.WATCH")
        self.assertEqual((s["heart_rate"]["min"], s["heart_rate"]["max"]), (50, 90))

    def test_final_after_seven_quiet_days_and_reopened_by_a_late_batch(self):
        self.sync([sample(HR, "2026-11-12T01:00:00.000Z", value=60)])
        later = NOW + timedelta(days=8)
        self.build(later)
        man = json.loads((self.store.user_dir(self.user) / "manifest.json").read_text())
        entry = man["days"]["2026-11-12"]
        self.assertEqual(entry["status"], "final")
        archive = self.store.user_dir(self.user) / entry["archive"]["file"]
        self.assertEqual(sha256_hex(archive.read_bytes()), entry["archive"]["sha256"])
        # Reproducible: rebuilding the archive gives the same bytes.
        day_dir = self.store.user_dir(self.user) / "days/2026/2026-11-12"
        self.assertEqual(B.build_archive(day_dir, "2026-11-12"), archive.read_bytes())
        self.sync([sample(HR, "2026-11-12T05:00:00.000Z", value=65)], now=later)
        man = json.loads((self.store.user_dir(self.user) / "manifest.json").read_text())
        self.assertEqual(man["days"]["2026-11-12"]["status"], "provisional")
        self.assertFalse(archive.exists())

    def test_census_mismatch_reported(self):
        recs = [sample(HR, "2026-11-12T01:00:00.000Z"), sample(HR, "2026-11-12T02:00:00.000Z")]
        self.sync(recs + [{"k": "census", "type": HR, "day": "2026-11-12", "tz": "Asia/Ho_Chi_Minh", "count": 3,
                           "uuid_sha256": "0" * 64}])
        mism = B.census_mismatches(self.store, self.user)
        self.assertEqual(mism, [{"device_id": self.dev.device_id, "type": HR, "day": "2026-11-12", "device": 3,
                                 "archive": 2}])

    def test_header_for_another_device_stops_that_device(self):
        other = FakeDevice(self.user, key=self.dev.key)
        other.user_id = new_user_id()  # header names another user
        other.seq, other.prev, other.grant_seq, other.grant_raw, other.processors = (
            self.dev.seq, self.dev.prev, self.dev.grant_seq, self.dev.grant_raw, self.dev.processors)
        seq, manifest, _sig, ct = other.seal([sample(HR, "2026-11-12T01:00:00.000Z")])
        # Re-sign a manifest for the right user around the wrong-user ciphertext.
        doc = json.loads(manifest)
        doc["user_id"] = self.user
        manifest = compact_json(doc)
        self.relay.add_batch(self.dev.device_id, (seq, manifest, self.dev.key.sign("manifest", manifest), ct))
        self.pull()
        r = self.build()
        self.assertTrue(any("header names another user" in e for e in r.errors))

    def test_build_is_idempotent(self):
        self.sync([sample(HR, "2026-11-12T01:00:00.000Z")])
        before = (self.store.user_dir(self.user) / "days/2026/2026-11-12" / f"{HR}.ndjson").read_bytes()
        db = B.UserBuild(self.store, self.user, NOW)
        db.db.execute("DELETE FROM built")  # forget progress: rebuilding must not duplicate
        db.db.commit()
        db.close()
        self.build()
        after = (self.store.user_dir(self.user) / "days/2026/2026-11-12" / f"{HR}.ndjson").read_bytes()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
