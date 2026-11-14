"""End to end against the real Worker under `wrangler dev` (local D1/R2, no Cloudflare account).

Runs only with HKA_E2E=1. Needs the Worker's node_modules (cd worker && npm ci) and Node 24; set
HKA_NODE_BIN to its bin directory if `node` on PATH is not the right one. With a test Medplum project
(python3 medplum/setup.py project --test) the processor's output also goes through FHIR: map, Medplum's
validator, load, read back, delete.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
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
from hkacommon.keys import SigningKey, fingerprint

from e2e_support import WORKER, LocalRelay, Medplum, map_and_validate, medplum_settings
from fakedevice import FakeDevice, sample


@unittest.skipUnless(os.environ.get("HKA_E2E") == "1", "set HKA_E2E=1 to run against wrangler dev")
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.admin = SigningKey.generate()
        cls.relay = LocalRelay(cls.admin)
        cls.origin = cls.relay.origin
        cls.medplum = medplum_settings()

    @classmethod
    def tearDownClass(cls):
        cls.relay.stop()
        cls.tmp.cleanup()

    def test_full_flow(self):
        now = datetime.now(timezone.utc)
        store = Store(Path(self.tmp.name) / "data")
        me = create_identity(store.keys_dir, "E2E Mac", now)
        client = Client(self.origin, me.sign, "p")
        admin = Client(self.origin, self.admin, "a")

        user = new_user_id()
        invite = make_invite(self.admin, self.origin, user, "Mẹ", format_instant(now + timedelta(days=1)))
        dev = FakeDevice(user)
        status, body = dev.enroll(self.origin, invite)
        self.assertEqual(status, 200, body)
        self.assertEqual((body["user_id"], body["device_id"]), (user, dev.device_id))

        self.assertEqual(dev.upload_grant(self.origin, *dev.make_grant([me.card]))[0], 200)
        s1 = dev.seal([sample("HKQuantityTypeIdentifierHeartRate", "2026-11-12T01:00:00.000Z", value=60, unit="count/min")])
        steps_rec = sample("HKQuantityTypeIdentifierStepCount", "2026-11-12T02:00:00.000Z", value=1234)
        s2 = dev.seal([steps_rec])
        self.assertEqual(dev.upload(self.origin, s2)[0], 200)  # out of order
        self.assertEqual(dev.upload(self.origin, s1)[0], 200)
        self.assertEqual(dev.upload(self.origin, s1)[0], 200)  # idempotent

        r = P.pull(store, me, client, now)
        self.assertIn("hka confirm", r.lines[0])
        P.confirm(store, dev.device_id, fingerprint(dev.key.pub), now)
        r = P.pull(store, me, client, now)
        self.assertEqual(r.alarms, [])
        built = B.build(store, me, now)
        self.assertEqual((built.batches, built.errors), (2, []))
        summary = json.loads((store.user_dir(user) / "days/2026/2026-11-12/summary.json").read_text())
        self.assertEqual(summary["totals"]["HKQuantityTypeIdentifierStepCount"]["value"], 1234)
        self.assertEqual(summary["heart_rate"]["max"], 60)

        if self.medplum:
            self.fhir_round_trip(store, me, client, dev, user, steps_rec, now)

        users = admin.get("/v1/admin/users")["users"]
        self.assertEqual(users[0]["devices"][0]["max_seq"], 3 if self.medplum else 2)  # + the deletion batch
        self.assertEqual(admin.post(f"/v1/admin/devices/{dev.device_id}/retire"), {"status": "retired"})
        s3 = dev.seal([sample("HKQuantityTypeIdentifierHeartRate", "2026-11-12T03:00:00.000Z")])
        self.assertEqual(dev.upload(self.origin, s3), (403, {"error": "device_inactive", "message": "device is not active",
                                                             "status": "retired"}))

    def test_swift_device_to_python_processor(self):
        """The iOS app's core code (as the hka-swift-device tool) talks to the Worker; hka decrypts its batches."""
        tool = WORKER.parent / "ios" / "HKACore" / ".build" / "debug" / "hka-swift-device"
        if not tool.exists():
            self.skipTest("build it first: cd ios/HKACore && swift build")
        now = datetime.now(timezone.utc)
        store = Store(Path(self.tmp.name) / "swift-data")
        me = create_identity(store.keys_dir, "Swift E2E", now)
        client = Client(self.origin, me.sign, "p")
        user = new_user_id()
        invite = make_invite(self.admin, self.origin, user, "Bố", format_instant(now + timedelta(days=1)))
        dev_dir = Path(self.tmp.name) / "swift-device"

        def swift(*args: str, env: dict | None = None) -> str:
            out = subprocess.run([str(tool), str(dev_dir), *args], capture_output=True, text=True,
                                 env={**os.environ, **(env or {})})
            self.assertEqual(out.returncode, 0, out.stderr)
            return out.stdout

        device_id, device_fp = swift("enroll", invite, "Bố's iPhone").split()
        swift("processors", me.card.text())
        records = [sample("HKQuantityTypeIdentifierHeartRate", f"2026-11-12T0{h}:00:00.000Z", value=60 + h,
                          unit="count/min") for h in range(6)]
        records.append({**sample("HKCategoryTypeIdentifierSleepAnalysis", "2026-11-11T16:00:00.000Z",
                                 "2026-11-11T22:00:00.000Z", value=3), "kind": "category", "value_name": "asleepCore"})
        path = Path(self.tmp.name) / "records.ndjson"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        out = swift("upload", str(path), env={"HKA_SOFT_BATCH": "800"})  # several batches
        self.assertIn("outbox 0", out)

        P.pull(store, me, client, now)
        P.confirm(store, device_id, device_fp, now)
        r = P.pull(store, me, client, now)
        self.assertEqual(r.alarms, [])
        built = B.build(store, me, now)
        self.assertEqual(built.errors, [])
        self.assertGreater(built.batches, 1)
        day = store.user_dir(user) / "days/2026/2026-11-12"
        summary = json.loads((day / "summary.json").read_text())
        self.assertEqual(summary["heart_rate"]["max"], 65)
        sleep = json.loads((store.user_dir(user) / "days/2026/2026-11-12/summary.json").read_text())["sleep"]
        self.assertEqual(sleep["stages_min"], {"asleepCore": 360.0})
        if self.medplum:
            map_and_validate(self, store, user)
            self.assertEqual(F.load_user(store, user, Medplum(self.medplum).client, F.DEFAULT_BASE)["errors"], [])
            fhir = Medplum(self.medplum)
            self.assertEqual(fhir.observation(f"{user}/2026-11-12/hr-max")["valueQuantity"]["value"], 65)
            self.assertEqual(fhir.observation(f"{user}/2026-11-12/sleep")["valueQuantity"]["value"], 360.0)

    def fhir_round_trip(self, store, me, client, dev, user, steps_rec, now):
        """Map, validate, load into the test Medplum, read back; then a deletion on the phone removes it there."""
        fhir = Medplum(self.medplum)
        map_and_validate(self, store, user)
        report = F.load_user(store, user, fhir.client, F.DEFAULT_BASE)
        self.assertEqual(report["errors"], [])
        self.assertGreater(report["loaded"], 0)
        steps_id = f"{user}/2026-11-12/HKQuantityTypeIdentifierStepCount"
        steps = fhir.observation(steps_id)
        self.assertEqual(steps["valueQuantity"], {"value": 1234, "unit": "/d", "system": F.UCUM, "code": "/d"})
        self.assertEqual(fhir.observation(f"{user}/2026-11-12/hr-max")["valueQuantity"]["value"], 60)
        patient = fhir.search("Patient", identifier=f"{F.DEFAULT_BASE}/identifier/hka-user|{user}")
        self.assertEqual(patient[0]["name"][0]["text"], "Mẹ")
        self.assertTrue(steps["subject"]["reference"].startswith("Patient/"))  # conditional reference resolved
        cs = fhir.search("CodeSystem", url=f"{F.DEFAULT_BASE}/CodeSystem/hka-metrics")
        self.assertEqual(cs[0]["version"], F.MAPPING_VERSION)
        # Loading again changes nothing.
        self.assertEqual(F.load_user(store, user, fhir.client, F.DEFAULT_BASE)["loaded"], 0)

        # The step sample is deleted in Health: the deletion travels to the server.
        gone = dev.seal([{"k": "deleted", "uuid": steps_rec["uuid"], "type": "HKQuantityTypeIdentifierStepCount",
                          "observed_at": format_instant(now)}])
        self.assertEqual(dev.upload(self.origin, gone)[0], 200)
        self.assertEqual(P.pull(store, me, client, now).alarms, [])
        self.assertEqual(B.build(store, me, now).errors, [])
        map_and_validate(self, store, user)
        self.assertEqual(F.load_user(store, user, fhir.client, F.DEFAULT_BASE)["errors"], [])
        self.assertIsNone(fhir.observation(steps_id))
        self.assertIsNotNone(fhir.observation(f"{user}/2026-11-12/hr-max"))


if __name__ == "__main__":
    unittest.main()
