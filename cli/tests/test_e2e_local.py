"""End to end against the real Worker under `wrangler dev` (local D1/R2, no Cloudflare account).

Runs only with HKA_E2E=1. Needs the Worker's node_modules (cd worker && npm ci) and an arm64
Node; set HKA_NODE_BIN to its bin directory if `node` on PATH is not the right one.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hka import build as B
from hka import pull as P
from hka.store import Store, create_identity
from hkacommon.client import Client
from hkacommon.documents import make_invite, new_user_id
from hkacommon.encoding import format_instant
from hkacommon.keys import SigningKey, fingerprint

from fakedevice import FakeDevice, sample

WORKER = Path(__file__).resolve().parents[2] / "worker"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(os.environ.get("HKA_E2E") == "1", "set HKA_E2E=1 to run against wrangler dev")
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = free_port()
        cls.origin = f"http://localhost:{cls.port}"
        cls.admin = SigningKey.generate()
        env = dict(os.environ)
        if os.environ.get("HKA_NODE_BIN"):
            env["PATH"] = os.environ["HKA_NODE_BIN"] + os.pathsep + env["PATH"]
        persist = str(Path(cls.tmp.name) / "wrangler-state")
        wrangler = [str(WORKER / "node_modules" / ".bin" / "wrangler")]
        subprocess.run(wrangler + ["d1", "migrations", "apply", "hka", "--local", "--persist-to", persist],
                       cwd=WORKER, env=env, check=True, capture_output=True)
        admin_keys = json.dumps({cls.admin.id("a"): cls.admin.pub_b64u})
        cls.log = open(Path(cls.tmp.name) / "wrangler.log", "w")
        cls.proc = subprocess.Popen(
            wrangler + ["dev", "--port", str(cls.port), "--ip", "127.0.0.1", "--persist-to", persist,
                        "--var", f"ORIGIN:{cls.origin}", "--var", f"ADMIN_KEYS:{admin_keys}"],
            cwd=WORKER, env=env, stdout=cls.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                urllib.request.urlopen(cls.origin + "/v1/nope", timeout=2)
            except urllib.error.HTTPError:
                return
            except OSError:
                time.sleep(0.5)
        cls.tearDownClass()
        raise RuntimeError("wrangler dev did not start; see " + str(Path(cls.tmp.name) / "wrangler.log"))

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.log.close()
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
        s2 = dev.seal([sample("HKQuantityTypeIdentifierStepCount", "2026-11-12T02:00:00.000Z", value=1234)])
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

        users = admin.get("/v1/admin/users")["users"]
        self.assertEqual(users[0]["devices"][0]["max_seq"], 2)
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


if __name__ == "__main__":
    unittest.main()
