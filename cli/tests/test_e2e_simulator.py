"""End to end with the real iOS app and real HealthKit, in the Simulator (macOS with Xcode only).

    HKA_E2E_SIM=1 HKA_NODE_BIN=… python -m unittest discover -s cli/tests -p test_e2e_simulator.py

1. Starts the relay under `wrangler dev` and creates a processor and an invite.
2. Installs the app fresh in a Simulator and runs the UI test `testSeedEnrolAndBackUp`: the app writes a
   known day into HealthKit (ios/App/DebugSeed.swift), the test allows Health access, the app enrols,
   reads HealthKit through anchored queries and uploads.
3. The processor compares the fingerprint the app shows with the relay's, pulls, verifies, decrypts and
   builds day folders; the values must be the seeded ones.
4. With a test Medplum project (python3 medplum/setup.py project --test): map, validate, load, read back.
5. `testDeleteWeighInAndBackUp` deletes the weigh-in in Health and taps Back Up Now; the deletion must
   reach the day folder and Medplum.

Settings: HKA_E2E_SIMULATOR (device id; default: a booted iPhone, else the first available one),
HKA_E2E_DERIVED (derived data folder, reused between runs), HKA_E2E_SKIP_BUILD=1 (reuse the last build).
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hka import build as B
from hka import fhir as F
from hka import pull as P
from hka.store import Store, create_identity
from hkacommon.client import Client
from hkacommon.documents import make_invite, new_user_id
from hkacommon.encoding import format_instant
from hkacommon.keys import SigningKey

from e2e_support import REPO, LocalRelay, Medplum, map_and_validate, medplum_settings

PROJECT = REPO / "ios" / "HealthBackup.xcodeproj"
BUNDLE = "app.hka.healthbackup"
DAY = "2026-11-10"  # DebugSeed.day
TEST = "HealthBackupUITests/SimulatorEndToEndTests"


def simulator() -> str:
    if os.environ.get("HKA_E2E_SIMULATOR"):
        return os.environ["HKA_E2E_SIMULATOR"]
    listing = json.loads(subprocess.run(["xcrun", "simctl", "list", "devices", "available", "-j"],
                                        capture_output=True, text=True, check=True).stdout)
    phones = [d for runtime, devices in listing["devices"].items() if "iOS" in runtime
              for d in devices if d["name"].startswith("iPhone")]
    if not phones:
        raise RuntimeError("no available iPhone simulator")
    booted = [d for d in phones if d["state"] == "Booted"]
    return (booted or phones)[0]["udid"]


def lines(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


@unittest.skipUnless(os.environ.get("HKA_E2E_SIM") == "1", "set HKA_E2E_SIM=1 (macOS with Xcode and a simulator)")
class SimulatorEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if subprocess.run(["xcodebuild", "-checkFirstLaunchStatus"], capture_output=True).returncode != 0:
            # Without them xcodebuild cannot start the UI test runner and hangs for minutes before failing.
            raise RuntimeError("Xcode's first-launch components are missing: run `sudo xcodebuild -runFirstLaunch`")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.sim = simulator()
        cls.derived = Path(os.environ.get("HKA_E2E_DERIVED") or Path(cls.tmp.name) / "derived")
        subprocess.run(["xcrun", "simctl", "boot", cls.sim], capture_output=True)  # already booted is fine
        subprocess.run(["xcrun", "simctl", "bootstatus", cls.sim, "-b"], check=True, capture_output=True)
        if os.environ.get("HKA_E2E_SKIP_BUILD") != "1":
            cls.xcodebuild("build-for-testing")
        cls.admin = SigningKey.generate()
        cls.relay = LocalRelay(cls.admin)
        cls.medplum = medplum_settings()

    @classmethod
    def tearDownClass(cls):
        cls.relay.stop()
        cls.tmp.cleanup()

    @classmethod
    def xcodebuild(cls, action: str, *extra: str, env: dict | None = None) -> None:
        cmd = ["xcodebuild", action, "-project", str(PROJECT), "-scheme", "HealthBackup",
               "-destination", f"platform=iOS Simulator,id={cls.sim}", "-derivedDataPath", str(cls.derived),
               "CODE_SIGN_IDENTITY=-", "CODE_SIGN_STYLE=Manual", "DEVELOPMENT_TEAM=", *extra]
        out = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, **(env or {})})
        if out.returncode != 0:
            raise AssertionError(f"xcodebuild {action} failed:\n" + (out.stdout + out.stderr)[-6000:])

    def ui_test(self, name: str, **values: str) -> None:
        env = {f"TEST_RUNNER_HKA_E2E_{k.upper()}": v for k, v in values.items()}
        self.xcodebuild("test-without-building", f"-only-testing:{TEST}/{name}", env=env)

    def day_files(self, store: Store, user: str) -> Path:
        return store.user_dir(user) / "days" / DAY[:4] / DAY

    def test_real_healthkit_through_every_component(self):
        now = datetime.now(timezone.utc)
        out = Path(self.tmp.name) / "out"
        out.mkdir()
        store = Store(Path(self.tmp.name) / "data")
        me = create_identity(store.keys_dir, "Simulator E2E", now)
        client = Client(self.relay.origin, me.sign, "p")
        user = new_user_id()
        invite = make_invite(self.admin, self.relay.origin, user, "Mẹ", format_instant(now + timedelta(days=1)))
        subprocess.run(["xcrun", "simctl", "uninstall", self.sim, BUNDLE], capture_output=True)  # a new device

        # The app: seed HealthKit, allow access, enrol, read, upload.
        self.ui_test("testSeedEnrolAndBackUp", invite=invite,
                     card=me.card.text(), out=str(out))

        # The processor: the fingerprint the app shows must be the one the relay gives.
        P.pull(store, me, client, now)
        pending = {d: p for d, p in store.pins().items() if p["status"] == "pending"}
        self.assertEqual(len(pending), 1, pending)
        device_id, pin = next(iter(pending.items()))
        shown = (out / "fingerprint.txt").read_text().strip()
        self.assertEqual(shown, pin["fingerprint"])
        P.confirm(store, device_id, shown, now)
        self.assertEqual(P.pull(store, me, client, now).alarms, [])
        built = B.build(store, me, now)
        self.assertEqual(built.errors, [])

        day = self.day_files(store, user)
        summary = json.loads((day / "summary.json").read_text())
        totals = summary["totals"]
        # The app records Health's preferred unit for the region (km or mi, kg or lb, kcal): convert.
        si = lambda t, unit: F.ucum(t["value"], t["unit"], unit)[0]
        self.assertEqual(totals["HKQuantityTypeIdentifierStepCount"]["value"], 8345)
        self.assertEqual(totals["HKQuantityTypeIdentifierStepCount"]["source"], BUNDLE)
        self.assertAlmostEqual(si(totals["HKQuantityTypeIdentifierDistanceWalkingRunning"], "m"), 14300, places=0)
        self.assertAlmostEqual(si(totals["HKQuantityTypeIdentifierActiveEnergyBurned"], "kcal"), 1350, places=0)
        self.assertEqual((summary["heart_rate"]["min"], summary["heart_rate"]["max"]), (60, 159))
        sleep = summary["sleep"]
        self.assertEqual(sleep["stages_min"], {"inBed": 510.0, "asleepCore": 285.0, "asleepDeep": 90.0,
                                               "asleepREM": 90.0, "awake": 15.0})
        workouts = lines(day / "HKWorkoutTypeIdentifier.ndjson")
        self.assertEqual(sorted(w["activity_name"] for w in workouts), ["pickleball", "running"])
        running = next(w for w in workouts if w["activity_name"] == "running")
        self.assertEqual(running["duration_s"], 2400)
        route = lines(day / "HKWorkoutRoute.ndjson")
        self.assertEqual({r["workout_uuid"] for r in route}, {running["uuid"]})
        self.assertEqual(sum(len(r["points"]) for r in route), 80)
        weight = lines(day / "HKQuantityTypeIdentifierBodyMass.ndjson")
        self.assertEqual(len(weight), 1)
        self.assertAlmostEqual(si(weight[0], "kg"), 68.5, places=2)
        bp = lines(day / "HKCorrelationTypeIdentifierBloodPressure.ndjson")
        self.assertEqual(sorted(o["value"] for o in bp[0]["objects"]), [76, 118])

        fhir = Medplum(self.medplum) if self.medplum else None
        if fhir:
            map_and_validate(self, store, user)
            self.assertEqual(F.load_user(store, user, fhir.client, F.DEFAULT_BASE)["errors"], [])
            steps = fhir.observation(f"{user}/{DAY}/HKQuantityTypeIdentifierStepCount")
            self.assertEqual(steps["valueQuantity"]["value"], 8345)
            self.assertEqual(fhir.observation(f"{user}/{DAY}/sleep")["valueQuantity"]["value"], 465.0)
            run = fhir.observation(running["uuid"])
            self.assertEqual(run["valueQuantity"]["value"], 40.0)
            self.assertIn({"text": "running"}, [c.get("valueCodeableConcept") for c in run["component"]])
            doc = fhir.search("DocumentReference", identifier=f"{F.DEFAULT_BASE}/identifier/document|route/{running['uuid']}")
            url = doc[0]["content"][0]["attachment"]["url"]
            self.assertTrue(url.startswith("Binary/") or "/Binary/" in url, url)
            self.assertIsNotNone(fhir.observation(weight[0]["uuid"]))

        # Deleted in Health: gone from the day folder and from Medplum.
        self.ui_test("testDeleteWeighInAndBackUp", out=str(out))
        deadline = time.time() + 120
        while True:
            later = datetime.now(timezone.utc)
            self.assertEqual(P.pull(store, me, client, later).alarms, [])
            self.assertEqual(B.build(store, me, later).errors, [])
            if not lines(day / "HKQuantityTypeIdentifierBodyMass.ndjson"):
                break
            self.assertLess(time.time(), deadline, "the deletion did not arrive")
            time.sleep(3)
        if fhir:
            map_and_validate(self, store, user)
            self.assertEqual(F.load_user(store, user, fhir.client, F.DEFAULT_BASE)["errors"], [])
            self.assertIsNone(fhir.observation(weight[0]["uuid"]))
            self.assertIsNotNone(fhir.observation(f"{user}/{DAY}/HKQuantityTypeIdentifierStepCount"))


if __name__ == "__main__":
    unittest.main()
