"""FHIR mapping and loading."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hka import fhir as F

from fakedevice import sample
from test_hardening import Ready
from test_pull_build import HR, NOW, SLEEP

VALIDATOR = Path(__file__).resolve().parents[2] / "tools" / "fhir-validate"
BASE = F.DEFAULT_BASE
WATCH = {"bundle_id": "com.apple.health.WATCH", "name": "Apple Watch"}


def day_records() -> list[dict]:
    hr = [sample(HR, f"2026-11-12T0{h}:00:{s:02d}.000Z", value=60 + h + s / 10, unit="count/min", source=WATCH)
          for h in range(3) for s in (0, 30)]
    sleep = [{**sample(SLEEP, a, b, value=v), "kind": "category", "value_name": n, "source": WATCH}
             for a, b, v, n in [("2026-11-11T16:00:00.000Z", "2026-11-11T17:00:00.000Z", 3, "asleepCore"),
                                ("2026-11-11T17:00:00.000Z", "2026-11-11T17:30:00.000Z", 4, "asleepDeep"),
                                ("2026-11-11T17:30:00.000Z", "2026-11-11T18:00:00.000Z", 5, "asleepREM"),
                                ("2026-11-11T18:00:00.000Z", "2026-11-11T18:10:00.000Z", 2, "awake")]]
    workout = {"k": "workout", "type": "HKWorkoutTypeIdentifier", "uuid": "WORKOUT-1", "start": "2026-11-12T10:00:00.000Z",
               "end": "2026-11-12T10:45:00.000Z", "activity_type": 79, "activity_name": "pickleball", "duration_s": 2700,
               "stats": {HR: {"unit": "count/min", "avg": 128.0, "max": 161.0},
                         "HKQuantityTypeIdentifierActiveEnergyBurned": {"unit": "kcal", "sum": 410.5},
                         "HKQuantityTypeIdentifierDistanceWalkingRunning": {"unit": "km", "sum": 2.3}},
               "events": [], "activities": [], "source": WATCH, "device": None, "metadata": {}}
    route = {"k": "route", "uuid": "ROUTE-1", "workout_uuid": "WORKOUT-1", "part": 0, "parts": 1,
             "start": "2026-11-12T10:00:00.000Z", "end": "2026-11-12T10:45:00.000Z",
             "points": [[1794564000000, 10.77, 106.70, 5.0, 4, 3, 1.2, 90, None, None],
                        [1794564005000, 10.7701, 106.7002, None, 4, None, None, None, None, None]]}
    ecg = [{"k": "ecg", "uuid": "ECG-1", "type": "HKDataTypeIdentifierElectrocardiogram", "start": "2026-11-12T11:00:00.000Z",
            "end": "2026-11-12T11:00:30.000Z", "classification": "sinusRhythm", "avg_hr": 64, "sampling_hz": 512,
            "symptoms": "none", "part": p, "parts": 2, "voltages_uv": [p * 10 + 1.5, None], "source": WATCH,
            "device": None, "metadata": {}} for p in range(2)]
    bp = {**sample("HKCorrelationTypeIdentifierBloodPressure", "2026-11-12T07:00:00.000Z"), "kind": "correlation",
          "objects": [{"kind": "quantity", "type": "HKQuantityTypeIdentifierBloodPressureSystolic", "uuid": "S",
                       "value": 121, "unit": "mmHg"},
                      {"kind": "quantity", "type": "HKQuantityTypeIdentifierBloodPressureDiastolic", "uuid": "D",
                       "value": 79, "unit": "mmHg"}]}
    return hr + sleep + [workout, route, bp, *ecg,
                         sample("HKQuantityTypeIdentifierStepCount", "2026-11-12T02:00:00.000Z", value=5432, source=WATCH),
                         sample("HKQuantityTypeIdentifierRestingHeartRate", "2026-11-12T03:00:00.000Z", value=55,
                                unit="count/min", source=WATCH),
                         sample("HKQuantityTypeIdentifierOxygenSaturation", "2026-11-12T03:00:00.000Z", value=0.97, unit="%"),
                         sample("HKQuantityTypeIdentifierBodyMass", "2026-11-12T00:30:00.000Z", value=150, unit="lb"),
                         sample("HKQuantityTypeIdentifierDietaryWater", "2026-11-12T04:00:00.000Z", value=250, unit="mL"),
                         {**sample("HKCategoryTypeIdentifierMindfulSession", "2026-11-12T05:00:00.000Z",
                                   "2026-11-12T05:10:00.000Z", value=0), "kind": "category"},
                         {"k": "profile", "observed_at": "2026-11-13T07:00:00.000Z", "date_of_birth": "1960-02-03",
                          "biological_sex": "female"}]


class Mapping(Ready):
    def mapped(self) -> dict[str, dict]:
        r = F.map_user(self.store, self.user, BASE)
        self.assertEqual(r["errors"], [])
        path = self.store.user_dir(self.user) / "fhir/days/2026/2026-11-12.ndjson"
        out = {}
        for line in path.read_text().splitlines():
            res = json.loads(line)
            out[res["identifier"][0]["value"]] = res
        return out

    def test_day_maps_with_correct_codes_and_units(self):
        recs = day_records()
        self.sync(recs)
        out = self.mapped()
        codes = lambda r: {c["code"] for c in r["code"]["coding"]}
        uuids = {r.get("type"): r.get("uuid") for r in recs if r.get("k") == "sample"}

        hrm = out[f"{self.user}/2026-11-12/hr-minutes"]["valueSampledData"]
        self.assertEqual(len(hrm["data"].split()), 1440)
        # 00:00 UTC is minute 420 of the local day (UTC+7); its samples are 60.0 and 63.0.
        self.assertEqual(hrm["data"].split()[7 * 60], "61.5")
        self.assertEqual(hrm["data"].split()[7 * 60 + 1], "E")
        self.assertEqual(out[f"{self.user}/2026-11-12/hr-max"]["valueQuantity"]["value"], 65.0)

        spo2 = out[uuids["HKQuantityTypeIdentifierOxygenSaturation"]]
        self.assertEqual(codes(spo2), {"2708-6", "59408-5", "HKQuantityTypeIdentifierOxygenSaturation"})
        self.assertEqual(spo2["valueQuantity"], {"value": 97.0, "unit": "%", "system": F.UCUM, "code": "%"})
        weight = out[uuids["HKQuantityTypeIdentifierBodyMass"]]
        self.assertAlmostEqual(weight["valueQuantity"]["value"], 68.0389, places=3)
        self.assertEqual(weight["valueQuantity"]["unit"], "kg")
        self.assertEqual(weight["category"][0]["coding"][0]["code"], "vital-signs")

        steps = out[f"{self.user}/2026-11-12/HKQuantityTypeIdentifierStepCount"]
        self.assertEqual(steps["valueQuantity"]["value"], 5432)
        self.assertEqual(steps["valueQuantity"]["unit"], "/d")
        self.assertEqual(steps["device"]["reference"],
                         f"Device?identifier={BASE}/identifier/hka-source|{self.user}/com.apple.health.WATCH")

        bp = out[uuids["HKCorrelationTypeIdentifierBloodPressure"]]
        self.assertEqual(sorted(c["valueQuantity"]["value"] for c in bp["component"]), [79, 121])

        sleep = out[f"{self.user}/2026-11-12/sleep"]
        self.assertEqual(sleep["valueQuantity"]["value"], 120.0)
        stage = {next(c["code"] for c in comp["code"]["coding"] if c["system"] == F.LOINC_SYSTEM): comp["valueQuantity"]["value"]
                 for comp in sleep["component"] if any(c["system"] == F.LOINC_SYSTEM for c in comp["code"]["coding"])}
        self.assertEqual(stage, {"93830-8": 60.0, "93831-6": 30.0, "93829-0": 30.0, "93828-2": 10.0})

        w = out["WORKOUT-1"]
        self.assertEqual(w["valueQuantity"]["value"], 45.0)
        self.assertIn({"code": {"coding": [{"system": F.LOINC_SYSTEM, "code": "73985-4", "display": "Exercise activity"}]},
                       "valueCodeableConcept": {"text": "pickleball"}}, w["component"])
        dist = [c for c in w["component"] if c["code"]["coding"][0]["code"] == "workout-distance"][0]
        self.assertEqual(dist["valueQuantity"]["value"], 2300.0)
        doc = out["route/WORKOUT-1"]
        self.assertEqual(w["derivedFrom"][0]["reference"].split("|")[-1], "route/WORKOUT-1")
        import base64
        ext = doc["content"][0]["attachment"]["extension"][0]["valueString"]
        gpx = (self.store.user_dir(self.user) / ext).read_text()
        self.assertNotIn("data", doc["content"][0]["attachment"])
        self.assertIn('<trkpt lat="10.77" lon="106.7"><ele>5</ele>', gpx)

        ecg = out["ECG-1"]
        lead = [c for c in ecg["component"] if "valueSampledData" in c][0]["valueSampledData"]
        self.assertEqual(lead["data"], "1.5 E 11.5 E")
        self.assertAlmostEqual(lead["period"], 1.953125)

        water = out[uuids["HKQuantityTypeIdentifierDietaryWater"]]
        self.assertEqual(codes(water), {"HKQuantityTypeIdentifierDietaryWater"})
        self.assertEqual(water["valueQuantity"]["unit"], "mL")

        shared = [json.loads(l) for l in (self.store.user_dir(self.user) / "fhir/shared.ndjson").read_text().splitlines()]
        patient = shared[0]
        self.assertEqual((patient["gender"], patient["birthDate"]), ("female", "1960-02-03"))
        self.assertIn(f"{self.user}/com.apple.health.WATCH", {d["identifier"][0]["value"] for d in shared[1:]})

    def test_validates_with_the_medplum_validator(self):
        node = os.environ.get("HKA_NODE_BIN")
        node = str(Path(node) / "node") if node else shutil.which("node")
        if not node or not (VALIDATOR / "node_modules").exists():
            self.skipTest("cd tools/fhir-validate && npm ci, and set HKA_NODE_BIN")
        self.sync(day_records())
        F.map_user(self.store, self.user, BASE)
        out = subprocess.run([node, str(VALIDATOR / "validate.mjs"), str(self.store.user_dir(self.user) / "fhir")],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("error", out.stdout)

    def test_unchanged_days_are_skipped_and_changed_days_remapped(self):
        self.sync(day_records())
        self.assertEqual(F.map_user(self.store, self.user, BASE)["mapped"], 1)  # everything falls on 2026-11-12 local
        self.assertEqual(F.map_user(self.store, self.user, BASE)["mapped"], 0)
        self.sync([sample(HR, "2026-11-12T05:00:00.000Z", value=90, unit="count/min")])
        self.assertEqual(F.map_user(self.store, self.user, BASE)["mapped"], 1)


class FakeFhir(BaseHTTPRequestHandler):
    state: dict = {}

    def _reply(self, status: int, body: dict | None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        if self.path == "/oauth2/token":
            return self._reply(200, {"access_token": "t"})
        assert self.headers["Authorization"] == "Bearer t"
        doc = json.loads(body)
        if self.path == "/fhir/R4":
            self.state.setdefault("transactions", []).append([e["request"]["url"] for e in doc["entry"]])
            return self._reply(200, {"entry": [{"response": {"status": "201 Created"}} for _ in doc["entry"]]})
        self.state.setdefault("posts", []).append(self.path)
        return self._reply(201, {"id": "x"})

    def do_GET(self):
        return self._reply(200, {"resourceType": "Bundle", "entry": []})

    def do_DELETE(self):
        self.state.setdefault("deletes", []).append(self.path)
        return self._reply(200, None)

    def log_message(self, *a):
        pass


class Loading(Ready):
    def test_load_is_incremental_and_deletes_vanished_resources(self):
        rec = sample("HKQuantityTypeIdentifierRestingHeartRate", "2026-11-12T03:00:00.000Z", value=55, unit="count/min")
        self.sync([rec])
        F.map_user(self.store, self.user, BASE)

        class Server(ThreadingHTTPServer):
            def server_bind(self):
                __import__("socketserver").TCPServer.server_bind(self)
                self.server_name, self.server_port = "localhost", self.server_address[1]

        FakeFhir.state = {}
        server = Server(("127.0.0.1", 0), FakeFhir)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = F.FhirClient(f"http://127.0.0.1:{server.server_address[1]}", "id", "secret", sleep=lambda s: None)
            self.assertEqual(F.load_user(self.store, self.user, client, BASE), {"loaded": 2, "skipped": 0, "removed": 0, "errors": []})
            self.assertEqual(sorted(FakeFhir.state["posts"]),  # the terminology: two CodeSystems and the ValueSet
                             ["/fhir/R4/CodeSystem", "/fhir/R4/CodeSystem", "/fhir/R4/ValueSet"])
            self.assertEqual(F.load_user(self.store, self.user, client, BASE), {"loaded": 0, "skipped": 2, "removed": 0, "errors": []})
            self.sync([{"k": "deleted", "uuid": rec["uuid"], "type": rec["type"], "observed_at": "2026-11-13T07:00:00.000Z"},
                       sample(HR, "2026-11-12T05:00:00.000Z", value=90, unit="count/min")])
            F.map_user(self.store, self.user, BASE)
            F.load_user(self.store, self.user, client, BASE)
            self.assertTrue(any(rec["uuid"] in d for d in FakeFhir.state["deletes"]))
        finally:
            server.shutdown()


class ReviewFixes(Ready):
    def resources(self, user=None) -> dict[str, dict]:
        user = user or self.user
        out = {}
        for p in sorted((self.store.user_dir(user) / "fhir/days").rglob("*.ndjson")):
            for line in p.read_text().splitlines():
                r = json.loads(line)
                out[r["identifier"][0]["value"]] = r
        return out

    def test_daily_identifiers_are_per_user(self):
        self.sync([sample("HKQuantityTypeIdentifierStepCount", "2026-11-12T02:00:00.000Z", value=10)])
        F.map_user(self.store, self.user, BASE)
        keys = set(self.resources())
        self.assertIn(f"{self.user}/2026-11-12/HKQuantityTypeIdentifierStepCount", keys)
        self.assertFalse(any(k.startswith("2026-") for k in keys))

    def test_locale_units_convert(self):
        uuids = {}
        recs = [sample("HKQuantityTypeIdentifierVO2Max", "2026-11-12T03:00:00.000Z", value=41.2, unit="mL/min·kg"),
                sample("HKQuantityTypeIdentifierBodyTemperature", "2026-11-12T03:00:00.000Z", value=98.6, unit="degF"),
                sample("HKQuantityTypeIdentifierBloodGlucose", "2026-11-12T03:00:00.000Z", value=5.5,
                       unit="mmol<180.1558800000541>/L"),
                sample("HKQuantityTypeIdentifierBodyMass", "2026-11-12T03:00:00.000Z", value=11, unit="st"),
                sample("HKQuantityTypeIdentifierActiveEnergyBurned", "2026-11-12T03:00:00.000Z", value=418.4, unit="kJ")]
        for r in recs:
            uuids[r["type"]] = r["uuid"]
        self.sync(recs)
        F.map_user(self.store, self.user, BASE)
        out = self.resources()
        v = lambda t: out[uuids[t]]["valueQuantity"]
        self.assertEqual(v("HKQuantityTypeIdentifierVO2Max")["unit"], "mL/(kg.min)")
        self.assertAlmostEqual(v("HKQuantityTypeIdentifierBodyTemperature")["value"], 37.0)
        self.assertAlmostEqual(v("HKQuantityTypeIdentifierBloodGlucose")["value"], 99.086, places=2)
        self.assertAlmostEqual(v("HKQuantityTypeIdentifierBodyMass")["value"], 69.853, places=2)
        energy = out[f"{self.user}/2026-11-12/HKQuantityTypeIdentifierActiveEnergyBurned"]["valueQuantity"]
        self.assertEqual((energy["value"], energy["unit"]), (100.0, "kcal"))

    def test_dst_day_has_1500_minutes(self):
        self.dev.tz = "America/Los_Angeles"
        # 2026-11-01 is 25 hours long in Los Angeles; 23:30 PST is 07:30 UTC on 11-02.
        self.sync([sample(HR, "2026-11-02T07:30:00.000Z", value=70, unit="count/min"),
                   sample(HR, "2026-11-01T08:00:00.000Z", value=60, unit="count/min")])
        F.map_user(self.store, self.user, BASE)
        data = self.resources()[f"{self.user}/2026-11-01/hr-minutes"]["valueSampledData"]["data"].split()
        self.assertEqual(len(data), 1500)
        self.assertEqual(data[1470], "70")

    def test_blood_pressure_needs_both_members_for_the_panel(self):
        one = {**sample("HKCorrelationTypeIdentifierBloodPressure", "2026-11-12T07:00:00.000Z"), "kind": "correlation",
               "objects": [{"type": "HKQuantityTypeIdentifierBloodPressureSystolic", "value": 120, "unit": "mmHg"}]}
        self.sync([one])
        F.map_user(self.store, self.user, BASE)
        codes = {c["code"] for c in self.resources()[one["uuid"]]["code"]["coding"]}
        self.assertNotIn("85354-9", codes)

    def test_ecg_with_a_missing_part_has_no_waveform(self):
        rec = {"k": "ecg", "uuid": "E", "type": "HKDataTypeIdentifierElectrocardiogram", "start": "2026-11-12T11:00:00.000Z",
               "end": "2026-11-12T11:00:30.000Z", "sampling_hz": 512, "part": 1, "parts": 2, "voltages_uv": [1.0],
               "classification": None}
        self.sync([rec])
        F.map_user(self.store, self.user, BASE)
        ecg = self.resources()["E"]
        self.assertFalse(any("valueSampledData" in c for c in ecg.get("component", [])))

    def audiogram(self, uuid: str, points: list[dict]) -> dict:
        return {"k": "object", "class": "HKAudiogramSample", "type": F.AUDIOGRAM, "uuid": uuid,
                "start": "2026-11-12T09:00:00.000Z", "end": "2026-11-12T09:06:00.000Z",
                "source": {"bundle_id": "com.apple.Health", "name": "Health"}, "device": {"model": "AirPods Pro"},
                "metadata": {}, "data": {"sensitivity_points": points}}

    def thresholds(self, obs: dict) -> dict[str, dict]:
        return {c["code"]["coding"][0]["code"]: c for c in obs["component"]}

    def test_audiogram_maps_air_thresholds_with_bounds_and_masking(self):
        t = lambda side, db, **kw: {"side": side, "conduction": kw.get("conduction", "air"), "masked": kw.get("masked", False),
                                    "sensitivity_db_hl": db, **({"clamping": kw["clamp"]} if "clamp" in kw else {})}
        rec = self.audiogram("AUD-1", [
            {"frequency_hz": 1000, "left_db_hl": -5, "right_db_hl": 30, "tests": [
                t("left", -5, clamp={"lower_db_hl": -5, "upper_db_hl": None}),
                t("right", 25), t("right", 30, masked=True)]},
            {"frequency_hz": 8000, "left_db_hl": 90, "right_db_hl": 20, "tests": [
                t("left", 90, clamp={"lower_db_hl": -10, "upper_db_hl": 90}), t("right", 20),
                t("right", 15, conduction="bone")]},
            {"frequency_hz": 125, "left_db_hl": 10, "right_db_hl": 10, "tests": [t("left", 10), t("right", 10)]}])
        self.sync([rec])
        report = F.map_user(self.store, self.user, BASE)
        obs = self.resources()["AUD-1"]
        self.assertEqual([c["code"] for c in obs["code"]["coding"]], ["89015-2", F.AUDIOGRAM])
        self.assertEqual(obs["category"][0]["coding"][0]["code"], "procedure")
        comps = self.thresholds(obs)
        self.assertEqual(sorted(comps), sorted(["89016-0", "89017-8", "89028-5", "89029-3"]))
        left_1k = comps["89016-0"]["valueQuantity"]
        self.assertEqual((left_1k["value"], left_1k["comparator"], left_1k["code"]), (-5, "<=", "dB"))
        self.assertEqual(comps["89028-5"]["valueQuantity"]["comparator"], ">=")
        self.assertNotIn("comparator", comps["89029-3"]["valueQuantity"])
        right_1k = comps["89017-8"]
        self.assertEqual(right_1k["valueQuantity"]["value"], 30)  # the masked threshold wins
        self.assertTrue(right_1k["extension"][0]["valueBoolean"])
        self.assertEqual(comps["89029-3"]["valueQuantity"]["value"], 20)  # the bone test is not air conduction
        self.assertEqual(report["unmapped_samples"], 3)  # one bone test, two 125 Hz thresholds

    def test_audiogram_without_tests_uses_the_point_values(self):
        rec = self.audiogram("AUD-2", [{"frequency_hz": 500, "left_db_hl": 15, "right_db_hl": None}])
        self.sync([rec])
        F.map_user(self.store, self.user, BASE)
        comps = self.thresholds(self.resources()["AUD-2"])
        self.assertEqual(list(comps), ["89024-4"])
        self.assertNotIn("extension", comps["89024-4"])  # masking unknown, so not stated

    def test_audiogram_with_nothing_mappable_has_no_observation(self):
        rec = self.audiogram("AUD-3", [{"frequency_hz": 125, "left_db_hl": 10, "right_db_hl": 10}])
        self.sync([rec])
        report = F.map_user(self.store, self.user, BASE)
        self.assertNotIn("AUD-3", self.resources())
        self.assertEqual(report["unmapped_samples"], 3)  # two thresholds without a code, then the empty audiogram

    def test_a_deleted_day_is_removed_from_the_server(self):
        rec = sample("HKQuantityTypeIdentifierRestingHeartRate", "2026-11-12T03:00:00.000Z", value=55, unit="count/min")
        self.sync([rec])
        F.map_user(self.store, self.user, BASE)
        self.sync([{"k": "deleted", "uuid": rec["uuid"], "type": rec["type"], "observed_at": "2026-11-13T07:00:00.000Z"}])
        self.assertEqual(F.map_user(self.store, self.user, BASE)["removed"], 1)
        self.assertEqual(self.resources(), {})


class LoaderPaths(Ready):
    def server(self, handler):
        class Server(ThreadingHTTPServer):
            def server_bind(self):
                __import__("socketserver").TCPServer.server_bind(self)
                self.server_name, self.server_port = "localhost", self.server_address[1]
        s = Server(("127.0.0.1", 0), handler)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        self.addCleanup(s.shutdown)
        return F.FhirClient(f"http://127.0.0.1:{s.server_address[1]}", "id", "secret", sleep=lambda s: None)

    def test_route_binary_token_refresh_quota_and_version_change(self):
        workout = {"k": "workout", "type": "HKWorkoutTypeIdentifier", "uuid": "W", "start": "2026-11-12T10:00:00.000Z",
                   "end": "2026-11-12T10:30:00.000Z", "activity_name": "running", "duration_s": 1800, "stats": {}}
        route = {"k": "route", "uuid": "R", "workout_uuid": "W", "part": 0, "parts": 1, "start": "2026-11-12T10:00:00.000Z",
                 "end": "2026-11-12T10:30:00.000Z", "points": [[1794564000000, 0.00001, 106.7, None]]}
        self.sync([workout, route])
        F.map_user(self.store, self.user, BASE)
        state = {"tokens": 0, "transactions": 0, "binaries": [], "deletes": [], "unauthorized_once": True}

        class H(FakeFhir):
            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = self.rfile.read(length)
                if self.path == "/oauth2/token":
                    state["tokens"] += 1
                    return self._reply(200, {"access_token": f"t{state['tokens']}"})
                if state["unauthorized_once"]:
                    state["unauthorized_once"] = False
                    return self._reply(401, {})
                if self.path == "/fhir/R4/Binary":
                    state["binaries"].append(body)
                    return self._reply(201, {"id": f"b{len(state['binaries'])}"})
                if self.path == "/fhir/R4":
                    state["transactions"] += 1
                    doc = json.loads(body)
                    status = "429 Too Many" if state["transactions"] == 2 else "201 Created"
                    return self._reply(200, {"entry": [{"response": {"status": status}} for _ in doc["entry"]]})
                return self._reply(201, {"id": "cs"})

            def do_DELETE(self):
                state["deletes"].append(self.path)
                return self._reply(200, None)

        client = self.server(H)
        r = F.load_user(self.store, self.user, client, BASE, sleep=lambda s: None)
        self.assertEqual(r["loaded"], 2)
        self.assertEqual(state["tokens"], 2)  # refreshed after the 401
        self.assertEqual(state["transactions"], 3)  # the day was resent after the 429
        # Uploaded with the batch that hit the quota, removed with it, and uploaded again.
        self.assertEqual(len(state["binaries"]), 2)
        self.assertIn("/fhir/R4/Binary/b1", state["deletes"])
        self.assertIn(b'lat="0.00001"', state["binaries"][1])
        # A new mapping version maps again and removes batches it no longer produces.
        lman = json.loads((self.store.user_dir(self.user) / "fhir/load-manifest.json").read_text())
        lman["days"]["2026-01-01"] = {"sha256": "x", "identifiers": ["Observation|s|gone"], "binaries": {}}
        (self.store.user_dir(self.user) / "fhir/load-manifest.json").write_text(json.dumps(lman))
        r = F.load_user(self.store, self.user, client, BASE, sleep=lambda s: None)
        self.assertEqual(r["removed"], 1)
        self.assertTrue(any("gone" in d for d in state["deletes"]))


class Round2(LoaderPaths):
    def recording(self):
        state = {"deletes": [], "transactions": 0}

        class H(FakeFhir):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                if self.path == "/oauth2/token":
                    return self._reply(200, {"access_token": "t"})
                if self.path == "/fhir/R4":
                    state["transactions"] += 1
                    doc = json.loads(body)
                    return self._reply(200, {"entry": [{"response": {"status": "201 Created"}} for _ in doc["entry"]]})
                return self._reply(201, {"id": "x"})

            def do_DELETE(self):
                state["deletes"].append(self.path)
                return self._reply(200, None)
        return self.server(H), state

    def test_a_day_that_fails_after_a_version_change_stays_on_the_server(self):
        self.sync([sample("HKQuantityTypeIdentifierRestingHeartRate", "2026-11-12T03:00:00.000Z", value=55, unit="count/min")])
        F.map_user(self.store, self.user, BASE)
        client, state = self.recording()
        F.load_user(self.store, self.user, client, BASE)
        from unittest import mock
        with mock.patch.object(F, "MAPPING_VERSION", "9.9.9"), mock.patch.object(F.Mapper, "run", side_effect=RuntimeError("bug")):
            self.assertEqual(len(F.map_user(self.store, self.user, BASE)["errors"]), 1)
            r = F.load_user(self.store, self.user, client, BASE)
        self.assertEqual(r["removed"], 0)
        self.assertEqual(state["deletes"], [])

    def test_a_removed_day_is_cleaned_up_once(self):
        rec = sample("HKQuantityTypeIdentifierRestingHeartRate", "2026-11-12T03:00:00.000Z", value=55, unit="count/min")
        self.sync([rec])
        F.map_user(self.store, self.user, BASE)
        client, state = self.recording()
        F.load_user(self.store, self.user, client, BASE)
        self.sync([{"k": "deleted", "uuid": rec["uuid"], "type": rec["type"], "observed_at": "2026-11-13T07:00:00.000Z"}])
        F.map_user(self.store, self.user, BASE)
        F.load_user(self.store, self.user, client, BASE)
        self.assertTrue(any(rec["uuid"] in d for d in state["deletes"]))
        before = (len(state["deletes"]), state["transactions"])
        r = F.load_user(self.store, self.user, client, BASE)
        self.assertEqual(r["loaded"], 0)
        self.assertEqual((len(state["deletes"]), state["transactions"]), before)

    def test_provisional_days_are_preliminary_and_first_zone_wins(self):
        self.sync([sample("HKQuantityTypeIdentifierRestingHeartRate", "2026-11-12T03:00:00.000Z", value=55, unit="count/min")])
        self.dev.tz = "Europe/London"
        self.sync([sample("HKQuantityTypeIdentifierRestingHeartRate", "2026-11-12T04:00:00.000Z", value=56, unit="count/min")])
        man = json.loads((self.store.user_dir(self.user) / "manifest.json").read_text())
        self.assertEqual(man["days"]["2026-11-12"]["tz"], "Asia/Ho_Chi_Minh")
        F.map_user(self.store, self.user, BASE)
        line = (self.store.user_dir(self.user) / "fhir/days/2026/2026-11-12.ndjson").read_text().splitlines()[0]
        self.assertEqual(json.loads(line)["status"], "preliminary")

    def test_sleep_without_an_asleep_stage_is_data_absent(self):
        self.sync([{**sample(SLEEP, "2026-11-11T16:00:00.000Z", "2026-11-11T23:00:00.000Z", value=0), "kind": "category",
                    "value_name": "inBed", "source": WATCH}])
        F.map_user(self.store, self.user, BASE)
        recs = [json.loads(l) for l in (self.store.user_dir(self.user) / "fhir/days/2026/2026-11-12.ndjson").read_text().splitlines()]
        sleep = [r for r in recs if r["identifier"][0]["value"].endswith("/sleep")][0]
        self.assertIn("dataAbsentReason", sleep)
        self.assertNotIn("valueQuantity", sleep)

    def test_device_names_are_not_replaced_by_bundle_ids(self):
        self.sync([sample("HKQuantityTypeIdentifierRestingHeartRate", "2026-11-12T03:00:00.000Z", value=55, unit="count/min",
                          source=WATCH)])
        F.map_user(self.store, self.user, BASE)
        self.sync([sample("HKQuantityTypeIdentifierStepCount", "2026-11-13T03:00:00.000Z", value=5, source=WATCH)])
        F.map_user(self.store, self.user, BASE)
        shared = [json.loads(l) for l in (self.store.user_dir(self.user) / "fhir/shared.ndjson").read_text().splitlines()]
        names = {d["identifier"][0]["value"]: d["deviceName"][0]["name"] for d in shared[1:]}
        self.assertEqual(names[f"{self.user}/com.apple.health.WATCH"], "Apple Watch")


class Round3(Round2):
    def test_a_refused_delete_does_not_block_other_batches(self):
        rec = sample("HKQuantityTypeIdentifierRestingHeartRate", "2026-11-12T03:00:00.000Z", value=55, unit="count/min")
        self.sync([rec])
        F.map_user(self.store, self.user, BASE)
        state = {"transactions": 0, "deletes": 0}

        class H(FakeFhir):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                if self.path == "/oauth2/token":
                    return self._reply(200, {"access_token": "t"})
                if self.path == "/fhir/R4":
                    state["transactions"] += 1
                    doc = json.loads(body)
                    return self._reply(200, {"entry": [{"response": {"status": "201 Created"}} for _ in doc["entry"]]})
                return self._reply(201, {"id": "x"})

            def do_DELETE(self):
                state["deletes"] += 1
                return self._reply(403, {})

        client = self.server(H)
        F.load_user(self.store, self.user, client, BASE)
        self.sync([{"k": "deleted", "uuid": rec["uuid"], "type": rec["type"], "observed_at": "2026-11-13T07:00:00.000Z"},
                   sample("HKQuantityTypeIdentifierStepCount", "2026-11-13T03:00:00.000Z", value=5)])
        F.map_user(self.store, self.user, BASE)
        r = F.load_user(self.store, self.user, client, BASE)
        self.assertTrue(r["errors"])
        lman = json.loads((self.store.user_dir(self.user) / "fhir/load-manifest.json").read_text())
        self.assertIn("2026-11-13", lman["days"], "the next day still loaded")
        self.assertTrue(lman["days"]["2026-11-12"]["pending_deletes"], "the refused delete is kept for a retry")


class Round4(LoaderPaths):
    def alive_server(self, refuse_deletes):
        """A server that tracks live identifiers; `refuse_deletes` is a list of booleans consumed per DELETE."""
        state = {"alive": set(), "refuse": refuse_deletes}

        class H(FakeFhir):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                if self.path == "/oauth2/token":
                    return self._reply(200, {"access_token": "t"})
                if self.path == "/fhir/R4":
                    doc = json.loads(body)
                    for e in doc["entry"]:
                        state["alive"].add(urllib.parse.unquote(e["request"]["url"]).split("|")[-1])
                    return self._reply(200, {"entry": [{"response": {"status": "201 Created"}} for _ in doc["entry"]]})
                return self._reply(201, {"id": "x"})

            def do_DELETE(self):
                if state["refuse"] and state["refuse"].pop(0):
                    return self._reply(403, {})
                state["alive"].discard(urllib.parse.unquote(self.path).split("|")[-1])
                return self._reply(200, None)

        return self.server(H), state

    def test_a_carried_delete_never_removes_a_rewritten_resource(self):
        a = sample("HKQuantityTypeIdentifierRestingHeartRate", "2026-11-12T03:00:00.000Z", value=55, unit="count/min")
        k = sample("HKQuantityTypeIdentifierRestingHeartRate", "2026-11-12T04:00:00.000Z", value=56, unit="count/min")
        self.sync([a, k])
        F.map_user(self.store, self.user, BASE)
        client, state = self.alive_server([])
        F.load_user(self.store, self.user, client, BASE)
        day = self.store.user_dir(self.user) / "fhir/days/2026/2026-11-12.ndjson"
        original = day.read_text()
        # Run 2: K leaves the mapping; its delete is refused and kept.
        day.write_text("".join(l + "\n" for l in original.splitlines() if k["uuid"] not in l))
        state["refuse"] = [True]
        F.load_user(self.store, self.user, client, BASE)
        self.assertIn(k["uuid"], state["alive"])
        # Run 3: K returns. The opening retry of the delete is refused again; the closing cleanup succeeds.
        day.write_text(original)
        state["refuse"] = [True]
        F.load_user(self.store, self.user, client, BASE)
        self.assertIn(k["uuid"], state["alive"], "K was deleted after the batch wrote it again")

    def test_a_delete_never_removes_an_identifier_another_batch_now_holds(self):
        a = sample("HKQuantityTypeIdentifierRestingHeartRate", "2026-11-12T03:00:00.000Z", value=55, unit="count/min")
        b = sample("HKQuantityTypeIdentifierRestingHeartRate", "2026-11-13T03:00:00.000Z", value=56, unit="count/min")
        self.sync([a, b])
        F.map_user(self.store, self.user, BASE)
        client, state = self.alive_server([])
        F.load_user(self.store, self.user, client, BASE)
        # The resource of 11-13 moves to 11-12 (as a route re-filed under its workout's day does).
        d12 = self.store.user_dir(self.user) / "fhir/days/2026/2026-11-12.ndjson"
        d13 = self.store.user_dir(self.user) / "fhir/days/2026/2026-11-13.ndjson"
        moved = [l for l in d13.read_text().splitlines() if b["uuid"] in l]
        d12.write_text(d12.read_text() + "".join(l + "\n" for l in moved))
        d13.write_text("".join(l + "\n" for l in d13.read_text().splitlines() if b["uuid"] not in l))
        F.load_user(self.store, self.user, client, BASE)
        self.assertIn(b["uuid"], state["alive"])
