"""A lying relay, malformed device data, crashes and identity mix-ups."""
from __future__ import annotations

import io
import json
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from hka import build as B
from hka import pull as P
from hka.store import ConfigError, Store, create_identity
from hka_admin import cli as admin_cli
from hkacommon.client import Client
from hkacommon.encoding import b64u_encode, sha256_hex
from hkacommon.keys import SigningKey, load_public_key, verify

from fakedevice import FakeDevice, sample
from fakerelay import Stream
from test_pull_build import HR, NOW, STEPS, SLEEP, Harness

ECG = "HKDataTypeIdentifierElectrocardiogram"


class Ready(Harness):
    def setUp(self):
        super().setUp()
        self.pull()
        self.confirm()

    def sync(self, records, now=NOW, dev=None):
        from hkacommon.encoding import format_instant
        dev = dev or self.dev
        self.relay.add_batch(dev.device_id, dev.seal(records), received_at=format_instant(now))
        self.assertEqual(self.pull(now).alarms, [])
        r = self.build(now)
        self.assertEqual(r.errors, [])
        return r


class LyingRelay(Ready):
    def test_omitted_device_is_an_alarm_and_still_watched(self):
        self.sync([sample(HR, "2026-11-12T01:00:00.000Z")])
        self.relay.streams.clear()
        alarms = self.pull(NOW + timedelta(days=4)).alarms
        self.assertTrue(any("no longer lists this device" in a for a in alarms))
        self.assertTrue(any("no new verified batch" in a for a in alarms))

    def test_future_received_at_does_not_hide_silence(self):
        self.relay.add_batch(self.dev.device_id, self.dev.seal([sample(HR, "2026-11-12T01:00:00.000Z")]),
                             received_at="2099-01-01T00:00:00.000Z")
        self.pull()
        self.assertTrue(any("no new verified batch" in a for a in self.pull(NOW + timedelta(days=4)).alarms))

    def test_garbage_received_at_is_rejected_and_does_not_poison_later_pulls(self):
        self.relay.add_batch(self.dev.device_id, self.dev.seal([sample(HR, "2026-11-12T01:00:00.000Z")]),
                             received_at="yesterday")
        self.assertTrue(any("malformed listing" in a for a in self.pull().alarms))
        self.relay.streams[self.dev.device_id].batches[1]["received_at"] = "2026-11-13T08:00:00.000Z"
        self.assertEqual(self.pull().alarms, [])
        self.assertEqual(self.state()["verified_seq"], 1)

    def test_listing_that_does_not_advance_stops(self):
        self.upload([sample(HR, "2026-11-12T01:00:00.000Z")])
        self.pull()
        stream = self.relay.streams[self.dev.device_id]
        original = self.relay.get
        calls = []

        def stuck(target):
            calls.append(target)
            if "/batches?" in target:
                return {"batches": [stream.batches[1]], "contiguous_seq": 1, "max_seq": 2}
            return original(target)
        self.relay.get = stuck
        self.assertTrue(any("does not advance" in a for a in self.pull().alarms))
        self.assertLess(len(calls), 5)

    def test_bad_stream_entries_write_nothing_outside_and_do_not_stop_others(self):
        good = FakeDevice(self.user)
        self.relay.streams[good.device_id] = Stream(good.key.pub_b64u, self.user)
        evil = dict(self.relay.get("/v1/streams")["streams"][0], user_id="../../evil")
        original = self.relay.get
        self.relay.get = lambda t: {"streams": [evil, {"device_id": 5}]
                                    + original(t)["streams"]} if t == "/v1/streams" else original(t)
        r = self.pull()
        self.assertTrue(any("malformed ids" in a for a in r.alarms))
        self.assertFalse((Path(self.tmp.name) / "evil").exists())
        self.assertIn(good.device_id, self.store.pins())

    def test_unexpected_exception_on_one_device_is_an_alarm(self):
        self.upload([sample(HR, "2026-11-12T01:00:00.000Z")])
        with mock.patch.object(P.Puller, "pull_grants", side_effect=TypeError("boom")):
            r = self.pull()
        self.assertTrue(any("boom" in a for a in r.alarms))

    def test_removed_device_still_uploading_is_reported_once(self):
        other = create_identity(Path(self.tmp.name) / "other", "Other", NOW)
        self.relay.add_grant(self.dev.device_id, *self.dev.make_grant([other.card]))
        stream = self.relay.streams[self.dev.device_id]
        stream.granted = False
        self.pull()
        self.relay.add_batch(self.dev.device_id, self.dev.seal([sample(HR, "2026-11-12T01:00:00.000Z")]))
        r = self.pull()
        self.assertTrue(any("still uploading" in line for line in r.lines))
        self.assertFalse(any("still uploading" in line for line in self.pull().lines))


class MalformedDeviceData(Ready):
    def test_bad_instants_null_source_and_lone_surrogate_do_not_crash(self):
        recs = [sample(HR, "2026-11-12T01:00:00Z"),            # no milliseconds: undated
                sample(STEPS, "2026-11-12T02:00:00.000Z", value=5, source=None),
                sample(HR, "2026-11-12T03:00:00.000Z", metadata={"note": "\ud800"}),
                {**sample(SLEEP, "2026-11-12T15:00:00.000Z", "2026-11-12T16:00:00.000Z", value=5), "kind": "category",
                 "value_name": 7}]
        import fakedevice
        ascii_json = lambda v: json.dumps(v, separators=(",", ":"), ensure_ascii=True).encode()
        with mock.patch.object(fakedevice, "compact_json", ascii_json):  # sends the escape \ud800, as JSON allows
            self.sync(recs)
        undated = self.store.user_dir(self.user) / "undated" / f"{HR}.ndjson"
        self.assertEqual(len(undated.read_text().splitlines()), 1)
        summary = json.loads((self.store.user_dir(self.user) / "days/2026/2026-11-12/summary.json").read_text())
        self.assertEqual(summary["totals"][STEPS]["source"], "?")
        self.assertEqual(summary["sleep"]["stages_min"], {"asleepREM": 60.0})

    def test_boolean_header_version_is_rejected(self):
        dev = self.dev
        with mock.patch("hka.build.is_int", return_value=False):
            self.upload([sample(HR, "2026-11-12T01:00:00.000Z")])
            self.pull()
            r = self.build()
        self.assertTrue(any("version 1 header" in e for e in r.errors))
        self.assertIsNotNone(dev)

    def test_unknown_kind_and_undated_records_follow_identity_rules(self):
        thing = {"k": "future_kind", "uuid": "U1", "type": "HKFutureType", "start": "2026-11-12T01:00:00.000Z"}
        undated = sample(HR, "not a time")
        self.sync([thing, undated])
        self.sync([dict(thing, extra=1), dict(undated, extra=1)])  # re-serialized copies
        self.assertEqual(len(self.day_records("2026-11-12", "HKFutureType")), 1)
        path = self.store.user_dir(self.user) / "undated" / f"{HR}.ndjson"
        self.assertEqual(len(path.read_text().splitlines()), 1)
        self.sync([{"k": "deleted", "uuid": undated["uuid"], "type": HR, "observed_at": "2026-11-13T07:00:00.000Z"}])
        self.assertFalse(path.exists())


class BuildRules(Ready):
    def test_dedup_and_deletion_across_devices(self):
        dev2 = FakeDevice(self.user)
        self.relay.streams[dev2.device_id] = Stream(dev2.key.pub_b64u, self.user, device_name="Watch")
        self.relay.add_grant(dev2.device_id, *dev2.make_grant([self.me.card]))
        self.pull()
        from hka.pull import confirm
        from hkacommon.keys import fingerprint
        confirm(self.store, dev2.device_id, fingerprint(dev2.key.pub), NOW)
        rec = sample(HR, "2026-11-12T01:00:00.000Z", value=60)
        self.sync([rec])
        self.sync([rec], dev=dev2)
        self.assertEqual(len(self.day_records("2026-11-12", HR)), 1)
        self.sync([{"k": "deleted", "uuid": rec["uuid"], "type": HR, "observed_at": "2026-11-13T07:00:00.000Z"}], dev=dev2)
        self.assertEqual(self.day_records("2026-11-12", HR), [])

    def test_deletion_reopens_a_final_day(self):
        rec = sample(HR, "2026-11-12T01:00:00.000Z")
        self.sync([rec, sample(HR, "2026-11-12T02:00:00.000Z")])
        later = NOW + timedelta(days=8)
        self.build(later)
        self.sync([{"k": "deleted", "uuid": rec["uuid"], "type": HR, "observed_at": "2026-11-21T00:00:00.000Z"}], now=later)
        man = json.loads((self.store.user_dir(self.user) / "manifest.json").read_text())
        self.assertEqual(man["days"]["2026-11-12"]["status"], "provisional")
        self.assertNotIn("archive", man["days"]["2026-11-12"])

    def test_multipart_ecg_dedup_and_record_deleted_in_same_batch(self):
        parts = [{"k": "ecg", "uuid": "E1", "type": ECG, "start": "2026-11-12T01:00:00.000Z",
                  "end": "2026-11-12T01:00:30.000Z", "part": i, "parts": 2, "voltages_uv": [i]} for i in range(2)]
        gone = sample(HR, "2026-11-12T01:00:00.000Z")
        self.sync(parts + [gone, {"k": "deleted", "uuid": gone["uuid"], "type": HR,
                                  "observed_at": "2026-11-13T07:00:00.000Z"}])
        self.sync(parts)
        self.assertEqual([r["part"] for r in self.day_records("2026-11-12", ECG)], [0, 1])
        self.assertEqual(self.day_records("2026-11-12", HR), [])

    def test_route_moves_to_its_workout_day_when_the_workout_arrives_later(self):
        route = {"k": "route", "uuid": "R1", "workout_uuid": "W1", "part": 0, "parts": 1,
                 "start": "2026-11-12T17:05:00.000Z", "end": "2026-11-12T17:20:00.000Z", "points": []}
        self.sync([route])  # 00:05 on the 13th local: filed there for now
        self.assertEqual(len(self.day_records("2026-11-13", "HKWorkoutRoute")), 1)
        workout = {"k": "workout", "type": "HKWorkoutTypeIdentifier", "uuid": "W1", "start": "2026-11-12T16:50:00.000Z",
                   "end": "2026-11-12T17:20:00.000Z", "activity_type": 37, "activity_name": "running"}
        self.sync([workout])
        self.assertEqual(self.day_records("2026-11-13", "HKWorkoutRoute"), [])
        self.assertEqual(len(self.day_records("2026-11-12", "HKWorkoutRoute")), 1)

    def test_summary_units_and_source_priority(self):
        km = {"bundle_id": "com.huawei.health", "name": "Huawei"}
        mi = {"bundle_id": "com.apple.health.PHONE", "name": "iPhone"}
        dist = "HKQuantityTypeIdentifierDistanceWalkingRunning"
        self.sync([sample(dist, "2026-11-12T01:00:00.000Z", value=5, unit="km", source=km),
                   sample(dist, "2026-11-12T02:00:00.000Z", value=4, unit="mi", source=mi)])
        s = json.loads((self.store.user_dir(self.user) / "days/2026/2026-11-12/summary.json").read_text())
        self.assertEqual((s["totals"][dist]["value"], s["totals"][dist]["unit"]), (5, "km"))
        conf = self.store.config()
        conf["source_priority"] = ["com.apple.health"]
        (self.store.root / "config.json").write_text(json.dumps(conf))
        self.sync([sample(dist, "2026-11-12T03:00:00.000Z", value=1, unit="mi", source=mi)])
        s = json.loads((self.store.user_dir(self.user) / "days/2026/2026-11-12/summary.json").read_text())
        self.assertEqual((s["totals"][dist]["value"], s["totals"][dist]["source"]), (5, "com.apple.health.PHONE"))

    def test_crash_after_commit_leaves_a_dirty_day_that_the_next_build_refreshes(self):
        self.sync([sample(HR, "2026-11-12T01:00:00.000Z")])
        later = NOW + timedelta(days=8)
        self.build(later)  # final, archived
        self.upload([sample(HR, "2026-11-12T02:00:00.000Z")], received_at="2026-11-21T08:00:00.000Z")
        self.pull(later)
        with mock.patch.object(B.UserBuild, "refresh_day", side_effect=RuntimeError("power cut")):
            r = self.build(later)
        self.assertTrue(r.errors)
        self.build(later)
        man = json.loads((self.store.user_dir(self.user) / "manifest.json").read_text())
        self.assertEqual(man["days"]["2026-11-12"]["status"], "provisional")
        self.assertNotIn("archive", man["days"]["2026-11-12"])
        self.assertEqual(man["days"]["2026-11-12"]["files"][f"{HR}.ndjson"]["records"], 2)


class Identity(Ready):
    def test_another_identity_is_refused(self):
        self.upload([sample(HR, "2026-11-12T01:00:00.000Z")])
        self.pull()
        other = create_identity(Path(self.tmp.name) / "other", "Other", NOW)
        with self.assertRaises(ConfigError):
            B.build(self.store, other, NOW)
        with self.assertRaises(ConfigError):
            P.pull(self.store, other, self.relay, NOW)
        self.assertEqual(self.build().batches, 1)


class ClientSigning(unittest.TestCase):
    def test_headers_and_clock_skew_retry(self):
        key = SigningKey.generate()
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                ts = int(self.headers["HKA-Timestamp"])
                payload = f"GET\n{origin}\n{self.path}\n{ts}\n{sha256_hex(b'')}".encode()
                ok = verify(load_public_key(key.pub_b64u), "request", payload, self.headers["HKA-Signature"])
                seen.append((ok, ts))
                server_time = int(time.time()) + 1000
                body = (json.dumps({"error": "stale_timestamp", "server_time": server_time}) if abs(ts - server_time) > 300
                        else json.dumps({"ok": True})).encode()
                self.send_response(401 if b"stale" in body else 200)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        class Server6(ThreadingHTTPServer):
            address_family = __import__("socket").AF_INET6  # "localhost" resolves to ::1 first on macOS

            def server_bind(self):  # skip HTTPServer's getfqdn(), which can take seconds
                __import__("socketserver").TCPServer.server_bind(self)
                self.server_name, self.server_port = "localhost", self.server_address[1]

        server = Server6(("::1", 0), Handler)
        origin = f"http://localhost:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self.assertEqual(Client(origin, key, "p").get("/v1/streams"), {"ok": True})
        finally:
            server.shutdown()
        self.assertEqual([ok for ok, _ in seen], [True, True])
        self.assertGreater(seen[1][1] - seen[0][1], 900)


class AdminCli(unittest.TestCase):
    def run_admin(self, *argv) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return admin_cli.main(list(argv))

    def test_invite_validation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"HKA_ADMIN_DIR": d}):
            self.assertEqual(self.run_admin("--server", "https://relay.example.test", "init"), 0)
            self.assertEqual(self.run_admin("invite", "--name", "Mom", "--days", "8", "--no-qr"), 2)
            self.assertEqual(self.run_admin("invite", "--name", "x" * 65, "--no-qr"), 2)
            self.assertEqual(self.run_admin("invite", "--name", "Mom", "--user", "u_bad", "--no-qr"), 2)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(admin_cli.main(["invite", "--name", "Mẹ", "--no-qr"]), 0)
            self.assertIn("--name 'Mẹ' --user u_", out.getvalue())
            self.assertTrue((Path(d) / "admin.pem").stat().st_mode & 0o777 == 0o600)


if __name__ == "__main__":
    unittest.main()
