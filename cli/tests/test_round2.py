"""Regression tests for the second review of the CLIs."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from unittest import mock

from hka import cli as hka_cli
from hka.store import ConfigError
from hkacommon.client import ApiError

from fakedevice import sample
from test_hardening import Ready
from test_pull_build import HR, NOW, SLEEP

WORKOUT = {"k": "workout", "type": "HKWorkoutTypeIdentifier", "uuid": "W1", "start": "2026-11-12T16:50:00.000Z",
           "end": "2026-11-12T17:20:00.000Z", "activity_type": 37, "activity_name": "running"}


def route(part: int, start: str) -> dict:
    return {"k": "route", "uuid": "R1", "workout_uuid": "W1", "part": part, "parts": 2, "start": start,
            "end": start, "points": []}


class Round2(Ready):
    def test_route_deleted_in_the_same_batch_as_its_workout_stays_deleted(self):
        self.sync([route(0, "2026-11-12T17:05:00.000Z"), route(1, "2026-11-13T17:05:00.000Z")])
        self.sync([WORKOUT, {"k": "deleted", "uuid": "R1", "type": "HKWorkoutRoute",
                             "observed_at": "2026-11-13T07:00:00.000Z"}])
        for day in ("2026-11-12", "2026-11-13", "2026-11-14"):
            self.assertEqual(self.day_records(day, "HKWorkoutRoute"), [], day)
        self.assertEqual(len(self.day_records("2026-11-12", "HKWorkoutTypeIdentifier")), 1)

    def test_deleting_a_workout_deletes_its_routes(self):
        self.sync([WORKOUT, route(0, "2026-11-12T17:05:00.000Z")])
        self.assertEqual(len(self.day_records("2026-11-12", "HKWorkoutRoute")), 1)
        self.sync([{"k": "deleted", "uuid": "W1", "type": "HKWorkoutTypeIdentifier", "observed_at": "2026-11-13T07:00:00.000Z"}])
        self.assertEqual(self.day_records("2026-11-12", "HKWorkoutRoute"), [])
        self.assertEqual(self.day_records("2026-11-12", "HKWorkoutTypeIdentifier"), [])

    def test_workout_and_route_deleted_in_the_same_batch(self):
        self.sync([WORKOUT, route(0, "2026-11-12T17:05:00.000Z"),
                   {"k": "deleted", "uuid": "W1", "type": "HKWorkoutTypeIdentifier", "observed_at": "2026-11-13T07:00:00.000Z"}])
        for day in ("2026-11-12", "2026-11-13"):
            self.assertEqual(self.day_records(day, "HKWorkoutRoute"), [])

    def test_bad_sleep_value_does_not_block_later_batches(self):
        bad = {**sample(SLEEP, "2026-11-12T15:00:00.000Z", "2026-11-12T16:00:00.000Z"), "kind": "category",
               "value": [1]}
        self.sync([bad])
        self.sync([sample(HR, "2026-11-12T05:00:00.000Z", value=70, unit="count/min")])
        s = json.loads((self.store.user_dir(self.user) / "days/2026/2026-11-12/summary.json").read_text())
        self.assertEqual(s["heart_rate"]["max"], 70)

    def test_a_summary_crash_is_reported_and_does_not_block(self):
        with mock.patch("hka.days.summarize", side_effect=RuntimeError("bug")):
            self.upload([sample(HR, "2026-11-12T05:00:00.000Z")])
            self.pull()
            r = self.build()
        self.assertEqual(r.errors, [])
        s = json.loads((self.store.user_dir(self.user) / "days/2026/2026-11-12/summary.json").read_text())
        self.assertIn("summary failed", s["error"])
        self.sync([sample(HR, "2026-11-12T06:00:00.000Z")])

    def test_record_with_a_raw_carriage_return(self):
        import fakedevice
        real = fakedevice.compact_json
        with mock.patch.object(fakedevice, "compact_json", lambda v: real(v).replace(b'"start":', b'"start":\r', 1)):
            self.sync([sample(HR, "2026-11-12T05:00:00.000Z", value=71)])
        self.sync([sample(HR, "2026-11-12T06:00:00.000Z", value=72)])
        self.assertEqual(sorted(r["value"] for r in self.day_records("2026-11-12", HR)), [71, 72])

    def test_extreme_timestamps_go_undated(self):
        self.dev.tz = "America/Los_Angeles"
        self.sync([sample(HR, "0001-01-01T00:00:00.000Z"), sample(HR, "2026-11-12T06:00:00.000Z")])
        undated = self.store.user_dir(self.user) / "undated" / f"{HR}.ndjson"
        self.assertEqual(len(undated.read_text().splitlines()), 1)

    def test_streams_failure_still_checks_pinned_devices(self):
        self.sync([sample(HR, "2026-11-12T05:00:00.000Z")])
        with mock.patch.object(self.relay, "get", side_effect=ApiError(0, "bad_response", "not JSON", {})):
            alarms = self.pull(NOW + timedelta(days=4)).alarms
        self.assertTrue(any("cannot list devices" in a for a in alarms))
        self.assertTrue(any("no new verified batch" in a for a in alarms))
        self.assertFalse(any("no longer lists" in a for a in alarms))

    def test_unusable_streams_responses_are_alarms(self):
        import http.client
        self.sync([sample(HR, "2026-11-12T05:00:00.000Z")])
        for exc in (RecursionError("deep"), http.client.IncompleteRead(b"")):
            with mock.patch.object(self.relay, "get", side_effect=exc):
                alarms = self.pull(NOW + timedelta(days=4)).alarms
            self.assertTrue(any("cannot reach the relay" in a for a in alarms))
            self.assertTrue(any("no new verified batch" in a for a in alarms))

    def test_extreme_census_day_does_not_crash_status(self):
        from hka import build as B
        self.sync([{"k": "census", "type": HR, "day": "0001-01-01", "tz": "UTC", "count": 1, "uuid_sha256": "0" * 64}])
        self.assertEqual(B.census_mismatches(self.store, self.user), [])

    def test_a_retired_device_dropped_by_the_relay_gets_no_silence_alarm(self):
        self.sync([sample(HR, "2026-11-12T05:00:00.000Z")])
        self.relay.streams[self.dev.device_id].status = "retired"
        self.pull()
        self.relay.streams.clear()
        alarms = self.pull(NOW + timedelta(days=10)).alarms
        self.assertTrue(any("no longer lists" in a for a in alarms))
        self.assertFalse(any("no new verified batch" in a for a in alarms))

    def test_string_source_priority_is_ignored(self):
        conf = self.store.config()
        conf["source_priority"] = "com"
        (self.store.root / "config.json").write_text(json.dumps(conf))
        self.sync([sample("HKQuantityTypeIdentifierStepCount", "2026-11-12T05:00:00.000Z", value=3)])

    def test_existing_data_dir_rejects_a_key_no_grant_lists(self):
        self.sync([sample(HR, "2026-11-12T05:00:00.000Z")])
        conf = self.store.config()
        conf.pop("processor_id")
        (self.store.root / "config.json").write_text(json.dumps(conf))
        from pathlib import Path
        from hka.store import create_identity
        from hka import build as B
        other = create_identity(Path(self.tmp.name) / "other", "Other", NOW)
        with self.assertRaises(ConfigError):
            B.build(self.store, other, NOW)
        B.build(self.store, self.me, NOW)
        self.assertEqual(self.store.config()["processor_id"], self.me.processor_id)

    def test_all_records_deleted_removes_the_day(self):
        rec = sample(HR, "2026-11-12T05:00:00.000Z")
        self.sync([rec])
        self.sync([{"k": "deleted", "uuid": rec["uuid"], "type": HR, "observed_at": "2026-11-13T07:00:00.000Z"}])
        man = json.loads((self.store.user_dir(self.user) / "manifest.json").read_text())
        self.assertNotIn("2026-11-12", man["days"])

    def run_verify(self) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return hka_cli.main(["--data-dir", str(self.store.root), "verify"])

    def test_verify_clean_then_tampered(self):
        self.sync([sample(HR, "2026-11-12T05:00:00.000Z")])
        with mock.patch.object(hka_cli, "store_from", lambda args: self.store):
            self.assertEqual(self.run_verify(), 0)
            ct = self.store.device_dir(self.user, self.dev.device_id) / "batches" / "0000000001.age"
            data = bytearray(ct.read_bytes())
            data[-1] ^= 1
            ct.write_bytes(bytes(data))
            self.assertEqual(self.run_verify(), 1)
