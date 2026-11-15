"""Sleep days: a night stays whole across midnight, and archives built under day rules 1 are migrated."""
from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest import mock
from zoneinfo import ZoneInfo

from hka import build as B
from hka import days as D
from hka import fhir as F

from fakedevice import sample
from test_fhir import BASE, FakeFhir
from test_hardening import Ready
from test_pull_build import HR, NOW, SLEEP

HCM = ZoneInfo("Asia/Ho_Chi_Minh")  # the fake device's zone, UTC+7
WATCH = {"bundle_id": "com.apple.health.WATCH", "name": "Watch"}


def z(local: str, tz=HCM) -> str:
    """'2026-11-11 23:00' local -> protocol instant in UTC."""
    dt = datetime.fromisoformat(local).replace(tzinfo=tz).astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def stage(start: str, end: str, name: str, **extra) -> dict:
    values = {v: k for k, v in D.SLEEP_VALUES.items()}
    return {**sample(SLEEP, z(start), z(end), value=values[name]), "kind": "category", "value_name": name,
            "source": WATCH, **extra}


def night(day_before: str, day: str) -> list[dict]:
    """A night from 23:20 on `day_before` to 06:30 on `day`: 410 min asleep, 10 min awake, 430 min in bed."""
    return [stage(f"{day_before} 23:20", f"{day} 06:30", "inBed"),
            stage(f"{day_before} 23:30", f"{day_before} 23:50", "asleepCore"),   # ends before midnight
            stage(f"{day_before} 23:50", f"{day} 00:40", "asleepDeep"),           # crosses midnight
            stage(f"{day} 00:40", f"{day} 00:50", "awake"),
            stage(f"{day} 00:50", f"{day} 05:00", "asleepCore"),
            stage(f"{day} 05:00", f"{day} 06:30", "asleepREM")]


class SleepDayRule(unittest.TestCase):
    def test_boundary_is_18_00_local(self):
        self.assertEqual(D.sleep_day(z("2026-11-12 17:59:59.999"), "Asia/Ho_Chi_Minh"), "2026-11-12")
        self.assertEqual(D.sleep_day(z("2026-11-12 18:00:00.000"), "Asia/Ho_Chi_Minh"), "2026-11-13")
        self.assertEqual(D.sleep_day(z("2026-11-12 23:59:59.999"), "Asia/Ho_Chi_Minh"), "2026-11-13")
        self.assertEqual(D.sleep_day(z("2026-11-13 00:00:00.000"), "Asia/Ho_Chi_Minh"), "2026-11-13")
        self.assertEqual(D.sleep_day(z("2026-11-13 06:30:00.000"), "Asia/Ho_Chi_Minh"), "2026-11-13")

    def test_month_and_year_ends(self):
        self.assertEqual(D.sleep_day(z("2026-12-31 23:00"), "Asia/Ho_Chi_Minh"), "2027-01-01")
        self.assertEqual(D.sleep_day(z("2028-02-28 22:00"), "Asia/Ho_Chi_Minh"), "2028-02-29")

    def test_boundary_is_wall_clock_across_daylight_saving_changes(self):
        ny = ZoneInfo("America/New_York")  # clocks go back at 02:00 on 2026-11-01
        self.assertEqual(D.sleep_day(z("2026-10-31 23:30", ny), "America/New_York"), "2026-11-01")
        self.assertEqual(D.sleep_day(z("2026-11-01 17:59", ny), "America/New_York"), "2026-11-01")
        self.assertEqual(D.sleep_day(z("2026-11-01 18:00", ny), "America/New_York"), "2026-11-02")
        self.assertEqual(D.sleep_day(z("2026-03-08 18:00", ny), "America/New_York"), "2026-03-09")  # spring forward

    def test_record_time_zone_metadata_wins_over_the_batch_zone(self):
        london = ZoneInfo("Europe/London")
        rec = {**sample(SLEEP, z("2026-11-12 22:00", london), z("2026-11-12 23:00", london)),
               "metadata": {"HKMetadataKeyTimeZone": "Europe/London"}}
        self.assertEqual(D.assign_day(rec, "Asia/Ho_Chi_Minh", lambda _u: None), "2026-11-13")
        # In Ho Chi Minh City that end instant is 06:00 on the 13th: also the 13th, but by the morning rule.
        rec["metadata"] = {}
        self.assertEqual(D.assign_day(rec, "Asia/Ho_Chi_Minh", lambda _u: None), "2026-11-13")

    def test_other_types_still_use_their_start_date(self):
        rec = sample(HR, z("2026-11-12 23:30"))
        self.assertEqual(D.assign_day(rec, "Asia/Ho_Chi_Minh", lambda _u: None), "2026-11-12")

    def test_unusable_end_falls_back_to_start_and_extreme_dates_do_not_crash(self):
        rec = sample(SLEEP, z("2026-11-12 23:30"), "not an instant")
        self.assertEqual(D.assign_day(rec, "Asia/Ho_Chi_Minh", lambda _u: None), "2026-11-12")
        # The end's sleep day would be in year 10000: fall back to the start's date instead of crashing.
        far = sample(SLEEP, "9999-12-31T00:00:00.000Z", "9999-12-31T13:00:00.000Z")
        self.assertEqual(D.assign_day(far, "Asia/Ho_Chi_Minh", lambda _u: None), "9999-12-31")
        self.assertIsNone(D.sleep_day("9999-12-31T13:00:00.000Z", "Asia/Ho_Chi_Minh"))


class WholeNights(Ready):
    def day_file(self, day: str) -> list[dict]:
        p = self.store.user_dir(self.user) / "days" / day[:4] / day / f"{SLEEP}.ndjson"
        return [json.loads(l) for l in B.read_lines(p)]

    def summary(self, day: str) -> dict:
        return json.loads((self.store.user_dir(self.user) / "days" / day[:4] / day / "summary.json").read_text())

    def test_a_night_across_midnight_is_one_day(self):
        recs = night("2026-11-11", "2026-11-12")
        self.sync(recs)
        self.assertEqual(len(self.day_file("2026-11-12")), len(recs))
        self.assertFalse((self.store.user_dir(self.user) / "days/2026/2026-11-11").exists())
        s = self.summary("2026-11-12")["sleep"]
        self.assertEqual(s["asleep_min"], 410.0)
        self.assertEqual(s["stages_min"], {"asleepCore": 270.0, "asleepDeep": 50.0, "asleepREM": 90.0, "awake": 10.0,
                                           "inBed": 430.0})

    def test_a_night_split_across_batches_lands_on_the_same_day(self):
        recs = night("2026-11-11", "2026-11-12")
        self.sync(recs[1:2])  # 23:30-23:50, uploaded before midnight
        self.sync(recs[:1] + recs[2:], now=NOW + timedelta(hours=8))
        self.assertEqual(len(self.day_file("2026-11-12")), len(recs))
        self.assertEqual(self.summary("2026-11-12")["sleep"]["asleep_min"], 410.0)

    def test_consecutive_nights_do_not_mix(self):
        self.sync(night("2026-11-10", "2026-11-11") + night("2026-11-11", "2026-11-12"))
        for day in ("2026-11-11", "2026-11-12"):
            self.assertEqual(self.summary(day)["sleep"]["asleep_min"], 410.0)
            self.assertEqual(self.summary(day)["sleep"]["stages_min"]["inBed"], 430.0)

    def test_naps(self):
        self.sync([stage("2026-11-12 14:00", "2026-11-12 15:00", "asleepUnspecified"),    # afternoon nap
                   stage("2026-11-12 18:30", "2026-11-12 19:00", "asleepUnspecified")])   # evening nap
        self.assertEqual(self.summary("2026-11-12")["sleep"]["asleep_min"], 60.0)
        self.assertEqual(self.summary("2026-11-13")["sleep"]["asleep_min"], 30.0)         # counts for the next night

    def test_census_is_unaffected(self):
        recs = night("2026-11-11", "2026-11-12")
        census = [{"k": "census", "type": SLEEP, "day": "2026-11-11", "tz": "Asia/Ho_Chi_Minh", "count": 3,
                   "uuid_sha256": "0" * 64},
                  {"k": "census", "type": SLEEP, "day": "2026-11-12", "tz": "Asia/Ho_Chi_Minh", "count": 3,
                   "uuid_sha256": "0" * 64}]
        self.sync(recs + census)  # the census counts by start day, as the spec says
        self.assertEqual(B.census_mismatches(self.store, self.user), [])

    def test_new_archives_record_the_current_day_rules(self):
        self.sync(night("2026-11-11", "2026-11-12"))
        man = json.loads((self.store.user_dir(self.user) / "manifest.json").read_text())
        self.assertEqual(man["day_rules"], D.DAY_RULES)

    def test_unknown_day_rules_are_refused(self):
        self.sync([sample(HR, "2026-11-12T01:00:00.000Z")])
        path = self.store.user_dir(self.user) / "manifest.json"
        for bad in (D.DAY_RULES + 1, 0, True, "2"):
            man = json.loads(path.read_text())
            man["day_rules"] = bad
            path.write_text(json.dumps(man))
            r = self.build()
            self.assertTrue(any("day-rules version" in e for e in r.errors), bad)


class Migration(Ready):
    """Archives built by an hka with day rules 1 (each sleep record on the calendar date it ends)."""

    def setUp(self):
        super().setUp()
        self.nights = night("2026-11-10", "2026-11-11") + night("2026-11-11", "2026-11-12")
        self.other = sample(HR, z("2026-11-11 12:00"), value=60, unit="count/min")
        with mock.patch.object(D, "SLEEP_DAY_SHIFT", timedelta(0)):
            self.sync(self.nights + [self.other])
        self.manifest_path = self.store.user_dir(self.user) / "manifest.json"
        man = json.loads(self.manifest_path.read_text())
        del man["day_rules"]  # what version 1 wrote
        self.manifest_path.write_text(json.dumps(man))
        # Under rules 1 the 23:30-23:50 stage of each night sat on the evening before.
        self.assertEqual(len(self.day_file("2026-11-10")), 1)
        self.assertEqual(len(self.day_file("2026-11-11")), 6)
        self.assertEqual(len(self.day_file("2026-11-12")), 5)
        self.before = {r["uuid"]: raw for day in ("2026-11-10", "2026-11-11", "2026-11-12")
                       for raw, r in self.raw_file(day)}

    def raw_file(self, day: str) -> list[tuple[bytes, dict]]:
        p = self.store.user_dir(self.user) / "days" / day[:4] / day / f"{SLEEP}.ndjson"
        return [(raw, json.loads(raw)) for raw in B.read_lines(p)]

    def day_file(self, day: str) -> list[dict]:
        return [r for _, r in self.raw_file(day)]

    def summary(self, day: str) -> dict | None:
        p = self.store.user_dir(self.user) / "days" / day[:4] / day / "summary.json"
        return json.loads(p.read_text()) if p.exists() else None

    def index_days(self) -> dict[str, str]:
        db = B.UserBuild(self.store, self.user, NOW)
        try:
            return dict(db.db.execute("SELECT uuid, day FROM records WHERE type = ?", (SLEEP,)).fetchall())
        finally:
            db.close()

    def assert_migrated(self):
        old = self.store.user_dir(self.user) / "days/2026/2026-11-10"
        self.assertEqual(list(old.iterdir()) if old.exists() else [], [])  # emptied, like a day whose records were deleted
        self.assertNotIn("2026-11-10", json.loads(self.manifest_path.read_text())["days"])
        for day in ("2026-11-11", "2026-11-12"):
            recs = self.day_file(day)
            self.assertEqual(len(recs), 6, day)
            self.assertEqual(len({r["uuid"] for r in recs}), 6, day)  # no duplicates
            self.assertEqual(self.summary(day)["sleep"]["asleep_min"], 410.0, day)
        # Bytes are the device's, unchanged; the index agrees with the files.
        after = {r["uuid"]: raw for day in ("2026-11-11", "2026-11-12") for raw, r in self.raw_file(day)}
        self.assertEqual(after, self.before)
        expected = {r["uuid"]: D.sleep_day(r["end"], "Asia/Ho_Chi_Minh") for r in self.nights}
        self.assertEqual(self.index_days(), expected)
        self.assertEqual(json.loads(self.manifest_path.read_text())["day_rules"], D.DAY_RULES)
        # Untouched data stays where it was.
        hr = self.store.user_dir(self.user) / "days/2026/2026-11-11" / f"{HR}.ndjson"
        self.assertEqual(len(B.read_lines(hr)), 1)

    def test_migration_moves_split_nights_together(self):
        r = self.build()
        self.assertEqual(r.errors, [])
        self.assertEqual((r.records_moved, r.records_unplaced), (2, 0))
        self.assert_migrated()

    def test_migration_runs_once(self):
        self.build()
        self.assertEqual(self.build().records_moved, 0)
        self.assert_migrated()

    def test_migration_reopens_a_final_day_it_changes(self):
        later = NOW + timedelta(days=8)
        with mock.patch.object(D, "DAY_RULES", 1):  # finalize under rules 1 first
            self.build(later)
        man = json.loads(self.manifest_path.read_text())
        self.assertEqual(man["days"]["2026-11-11"]["status"], "final")
        archive = self.store.user_dir(self.user) / man["days"]["2026-11-11"]["archive"]["file"]
        self.assertTrue(archive.exists())
        self.build(later)
        man = json.loads(self.manifest_path.read_text())
        self.assertEqual(man["days"]["2026-11-11"]["status"], "provisional")
        self.assertFalse(archive.exists())
        self.assert_migrated()

    def test_interrupted_after_adding_to_new_days(self):
        real = B.UserBuild.merge_file

        def crash_on_remove(ub, day, fname, add, remove):
            if remove:
                raise OSError("power cut")
            return real(ub, day, fname, add, remove)

        with mock.patch.object(B.UserBuild, "merge_file", crash_on_remove):
            r = self.build()
        self.assertTrue(any("power cut" in e for e in r.errors))
        self.assertNotIn("day_rules", json.loads(self.manifest_path.read_text()))
        self.assertEqual(len(self.day_file("2026-11-11")), 7)  # the moved stage is in both days for now
        self.assertEqual(self.build().errors, [])
        self.assert_migrated()

    def test_interrupted_before_the_index_update(self):
        with mock.patch.object(B.UserBuild, "commit_moves", side_effect=OSError("power cut")):
            r = self.build()
        self.assertTrue(any("power cut" in e for e in r.errors))
        self.assertEqual(len(self.day_file("2026-11-10")), 0)  # files moved, index still says the 10th
        self.assertEqual(self.build().errors, [])
        self.assert_migrated()

    def test_interrupted_before_day_rules_are_saved(self):
        with mock.patch.object(B.UserBuild, "refresh_dirty", side_effect=[None, OSError("power cut")]):
            r = self.build()
        self.assertTrue(any("power cut" in e for e in r.errors))
        self.assertNotIn("day_rules", json.loads(self.manifest_path.read_text()))
        r = self.build()
        self.assertEqual((r.errors, r.records_moved), ([], 0))
        self.assert_migrated()

    def test_interrupted_part_way_through_the_removals(self):
        real, calls = B.UserBuild.merge_file, []

        def crash_on_second_remove(ub, day, fname, add, remove):
            if remove:
                calls.append(day)
                if len(calls) == 2:
                    raise OSError("power cut")
            return real(ub, day, fname, add, remove)

        with mock.patch.object(B.UserBuild, "merge_file", crash_on_second_remove):
            self.assertTrue(any("power cut" in e for e in self.build().errors))
        self.assertEqual(self.build().errors, [])
        self.assert_migrated()

    def test_index_rebuilt_from_the_inbox(self):
        (self.store.user_dir(self.user) / "index.sqlite").unlink()
        r = self.build()
        self.assertEqual(r.errors, [])
        self.assert_migrated()

    def test_lost_manifest_still_migrates(self):
        self.manifest_path.unlink()
        self.assertEqual(self.build().errors, [])
        man = json.loads(self.manifest_path.read_text())
        self.assertEqual(man["day_rules"], D.DAY_RULES)
        for day in ("2026-11-11", "2026-11-12"):
            self.assertEqual(len(self.day_file(day)), 6, day)
            self.assertEqual(self.summary(day)["sleep"]["asleep_min"], 410.0, day)

    def test_old_day_with_other_data_is_recomputed_not_dropped(self):
        evening = sample(HR, z("2026-11-10 21:00"), value=70, unit="count/min")
        with mock.patch.object(D, "SLEEP_DAY_SHIFT", timedelta(0)), mock.patch.object(D, "DAY_RULES", 1):
            self.sync([evening])
        self.build()
        s = self.summary("2026-11-10")
        self.assertIsNone(s["sleep"])
        self.assertEqual(s["heart_rate"]["samples"], 1)
        man = json.loads(self.manifest_path.read_text())
        self.assertNotIn(f"{SLEEP}.ndjson", man["days"]["2026-11-10"]["files"])

    def test_census_after_the_migration(self):
        self.build()
        self.sync([{"k": "census", "type": SLEEP, "day": d, "tz": "Asia/Ho_Chi_Minh", "count": n, "uuid_sha256": "0" * 64}
                   for d, n in (("2026-11-10", 3), ("2026-11-11", 6))])  # by start day: 3 each evening, 3 after midnight
        self.assertEqual(B.census_mismatches(self.store, self.user), [])

    def test_a_deletion_in_the_same_build_as_the_migration(self):
        moved = next(r for r in self.nights if r["start"] == z("2026-11-10 23:30"))
        self.relay.add_batch(self.dev.device_id, self.dev.seal(
            [{"k": "deleted", "uuid": moved["uuid"], "type": SLEEP, "observed_at": "2026-11-13T07:00:00.000Z"}]))
        self.pull()
        self.assertEqual(self.build().errors, [])
        self.assertNotIn(moved["uuid"], {r["uuid"] for r in self.day_file("2026-11-11")})
        self.assertEqual(self.summary("2026-11-11")["sleep"]["asleep_min"], 390.0)

    def test_batches_after_the_migration_use_the_new_rule(self):
        self.build()
        self.sync(night("2026-11-12", "2026-11-13"))
        self.assertEqual(len(self.day_file("2026-11-13")), 6)
        self.assertEqual(len(self.day_file("2026-11-12")), 6)

    def test_fhir_follows_the_move_and_deletes_the_stale_observation(self):
        F.map_user(self.store, self.user, BASE)
        fdir = self.store.user_dir(self.user) / "fhir/days/2026"
        self.assertTrue(any(json.loads(l)["identifier"][0]["value"].endswith("2026-11-10/sleep")
                            for l in (fdir / "2026-11-10.ndjson").read_text().splitlines()))

        class Server(ThreadingHTTPServer):
            def server_bind(self):
                __import__("socketserver").TCPServer.server_bind(self)
                self.server_name, self.server_port = "localhost", self.server_address[1]

        FakeFhir.state = {}
        server = Server(("127.0.0.1", 0), FakeFhir)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = F.FhirClient(f"http://127.0.0.1:{server.server_address[1]}", "id", "secret", sleep=lambda s: None)
            F.load_user(self.store, self.user, client, BASE)
            self.build()
            report = F.map_user(self.store, self.user, BASE)
            self.assertEqual((report["mapped"], report["removed"]), (2, 1))  # the 11th and 12th; the 10th is gone
            F.load_user(self.store, self.user, client, BASE)
            self.assertTrue(any("2026-11-10%2Fsleep" in d or "2026-11-10/sleep" in d
                                for d in FakeFhir.state.get("deletes", [])), FakeFhir.state.get("deletes"))
        finally:
            server.shutdown()
        for day in ("2026-11-11", "2026-11-12"):
            obs = [json.loads(l) for l in (fdir / f"{day}.ndjson").read_text().splitlines()]
            sleep = [o for o in obs if o["identifier"][0]["value"].endswith(f"{day}/sleep")][0]
            self.assertEqual(sleep["valueQuantity"]["value"], 410.0)
            prev = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
            self.assertTrue(sleep["effectivePeriod"]["start"].startswith(f"{prev}T23:20"), sleep["effectivePeriod"])


class MigrationAfterTravel(Ready):
    """Version 1 filed each record in the zone of the batch it came in; the day's manifest zone may differ."""

    def setUp(self):
        super().setUp()
        ny = ZoneInfo("America/New_York")
        with mock.patch.object(D, "SLEEP_DAY_SHIFT", timedelta(0)):
            self.dev.tz = "America/New_York"   # a batch sent while travelling fills the 11th and 12th first
            self.sync([sample(HR, z("2026-11-11 10:00", ny), value=60, unit="count/min"),
                       sample(HR, z("2026-11-12 10:00", ny), value=61, unit="count/min")])
            self.dev.tz = "Asia/Ho_Chi_Minh"   # home again: the nights are filed in Ho Chi Minh City time
            self.nights = night("2026-11-10", "2026-11-11") + night("2026-11-11", "2026-11-12")
            self.night_batch = self.dev.seq + 1
            self.sync(self.nights, now=NOW + timedelta(hours=1))
        self.manifest_path = self.store.user_dir(self.user) / "manifest.json"
        man = json.loads(self.manifest_path.read_text())
        del man["day_rules"]
        self.manifest_path.write_text(json.dumps(man))
        self.assertEqual(man["days"]["2026-11-11"]["tz"], "America/New_York")

    def sleep_days(self) -> dict[str, list[str]]:
        out = {}
        for day in ("2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"):
            p = self.store.user_dir(self.user) / "days" / day[:4] / day / f"{SLEEP}.ndjson"
            out[day] = sorted(json.loads(l)["uuid"] for l in B.read_lines(p))
        return out

    def test_the_batch_zone_is_used(self):
        r = self.build()
        self.assertEqual((r.errors, r.records_moved, r.records_unplaced), ([], 2, 0))
        days = self.sleep_days()
        self.assertEqual((len(days["2026-11-10"]), len(days["2026-11-11"]), len(days["2026-11-12"])), (0, 6, 6))

    def test_without_the_batch_nothing_moves_to_a_wrong_day(self):
        before = self.sleep_days()
        dev_dir = self.store.device_dir(self.user, self.dev.device_id)
        _, cpath = B.batch_paths(dev_dir, self.night_batch)
        cpath.unlink()  # the zone of the nights' batch can no longer be read
        r = self.build()
        self.assertEqual(r.errors, [])
        after = self.sleep_days()
        backward = [u for u in before["2026-11-11"] if u in after["2026-11-10"]] + \
                   [u for u in before["2026-11-12"] if u in after["2026-11-11"]]
        self.assertEqual(backward, [])
        self.assertGreater(r.records_unplaced, 0)
        self.assertEqual(sum(map(len, after.values())), len(self.nights))  # nothing lost or duplicated

    def test_a_record_with_its_own_zone(self):
        London = ZoneInfo("Europe/London")
        own = {**stage("2026-11-12 05:30", "2026-11-12 06:00", "asleepCore"),
               "start": z("2026-11-12 23:30", London), "end": z("2026-11-12 23:50", London),
               "metadata": {"HKMetadataKeyTimeZone": "Europe/London"}}
        with mock.patch.object(D, "SLEEP_DAY_SHIFT", timedelta(0)), mock.patch.object(D, "DAY_RULES", 1):
            self.sync([own], now=NOW + timedelta(hours=2))
        self.build()
        self.assertNotIn(own["uuid"], self.sleep_days()["2026-11-12"])  # 23:50 in London: the night ending on the 13th
        p = self.store.user_dir(self.user) / "days/2026/2026-11-13" / f"{SLEEP}.ndjson"
        self.assertIn(own["uuid"], [json.loads(l)["uuid"] for l in B.read_lines(p)])


if __name__ == "__main__":
    unittest.main()
