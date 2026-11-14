"""Shared pieces of the end-to-end tests: a local relay under `wrangler dev`, and the test Medplum project.

The FHIR part uses only the separate test project created by `python3 medplum/setup.py project --test`
(credentials in .env.test), so made-up test data never reaches the project that holds real data.
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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from hka import fhir as F
from hka.store import Store
from hkacommon.keys import SigningKey

REPO = Path(__file__).resolve().parents[2]
WORKER = REPO / "worker"
VALIDATOR = REPO / "tools" / "fhir-validate"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def node_env() -> dict:
    env = dict(os.environ)
    if os.environ.get("HKA_NODE_BIN"):
        env["PATH"] = os.environ["HKA_NODE_BIN"] + os.pathsep + env["PATH"]
    return env


class LocalRelay:
    """The real Worker under `wrangler dev`, with local D1/R2 in a temporary folder."""

    def __init__(self, admin: SigningKey):
        self.tmp = tempfile.TemporaryDirectory()
        self.port = free_port()
        self.origin = f"http://localhost:{self.port}"
        env = node_env()
        persist = str(Path(self.tmp.name) / "wrangler-state")
        wrangler = [str(WORKER / "node_modules" / ".bin" / "wrangler")]
        subprocess.run(wrangler + ["d1", "migrations", "apply", "hka", "--local", "--persist-to", persist],
                       cwd=WORKER, env=env, check=True, capture_output=True)
        admin_keys = json.dumps({admin.id("a"): admin.pub_b64u})
        self.log_path = Path(self.tmp.name) / "wrangler.log"
        self.log = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            wrangler + ["dev", "--port", str(self.port), "--ip", "127.0.0.1", "--persist-to", persist,
                        "--var", f"ORIGIN:{self.origin}", "--var", f"ADMIN_KEYS:{admin_keys}"],
            cwd=WORKER, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                urllib.request.urlopen(self.origin + "/v1/nope", timeout=2)
            except urllib.error.HTTPError as exc:
                exc.close()  # any HTTP answer means the Worker is up
                return
            except OSError:
                time.sleep(0.5)
        log = self.log_path.read_text()[-2000:]
        self.stop()
        raise RuntimeError("wrangler dev did not start:\n" + log)

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()
        self.tmp.cleanup()


def read_env_file(path: Path) -> dict[str, str]:
    out = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def medplum_settings() -> dict[str, str] | None:
    """URL and loader credentials of the test project, or None (the FHIR steps are then skipped).

    HKA_E2E_FHIR_ENV names the file (default .env.test). With HKA_E2E_REQUIRE_FHIR=1 (CI) a missing
    project is an error instead of a skip."""
    env = read_env_file(Path(os.environ.get("HKA_E2E_FHIR_ENV") or REPO / ".env.test"))
    if all(env.get(k) for k in ("HKA_FHIR_URL", "HKA_FHIR_CLIENT_ID", "HKA_FHIR_CLIENT_SECRET")):
        return env
    if os.environ.get("HKA_E2E_REQUIRE_FHIR") == "1":
        raise RuntimeError("no test Medplum project: run python3 medplum/setup.py project --test")
    return None


class Medplum:
    def __init__(self, settings: dict[str, str]):
        self.client = F.FhirClient(settings["HKA_FHIR_URL"], settings["HKA_FHIR_CLIENT_ID"],
                                   settings["HKA_FHIR_CLIENT_SECRET"])

    def search(self, rtype: str, **params: str) -> list[dict]:
        bundle = self.client.request("GET", f"/fhir/R4/{rtype}?{urllib.parse.urlencode(params)}")
        return [e["resource"] for e in (bundle or {}).get("entry") or []]

    def observation(self, ident: str) -> dict | None:
        found = self.search("Observation", identifier=f"{F.DEFAULT_BASE}/identifier/observation|{ident}")
        return found[0] if found else None


def map_and_validate(case: unittest.TestCase, store: Store, user: str) -> None:
    """`hka fhir map`, then Medplum's validator on the output when Node and its packages are present."""
    report = F.map_user(store, user, F.DEFAULT_BASE)
    case.assertEqual(report["errors"], [])
    node = shutil.which("node", path=node_env()["PATH"])
    if node and (VALIDATOR / "node_modules").exists():
        out = subprocess.run([node, str(VALIDATOR / "validate.mjs"), str(store.user_dir(user) / "fhir")],
                             capture_output=True, text=True)
        case.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        case.assertNotIn("error", out.stdout)
