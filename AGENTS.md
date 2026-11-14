# Agent guide: healthkit-archive

Read [spec/README.md](spec/README.md) before changing anything that touches the wire format; it is
normative for the Worker, the CLIs and the app, and `spec/vectors/` must keep passing in all three.

## Rules

- Never print, log or commit private keys: `data/keys/`, `~/.config/hka-admin/admin.pem`, device keys.
  Public keys (`ADMIN_KEYS`, cards) are not secret.
- `data/` (the processor's data directory) holds decrypted health data. Never commit it, never send it to
  an external service, and do not open day folders to answer questions unless the owner asks.
- A change to the protocol changes `spec/README.md`, `spec/tools/gen_vectors.py` and the regenerated
  vectors together (`--check` must pass), then all three implementations.
- Devices own their hash chain: never add code that renumbers a seq, takes `prev` from the relay, or
  re-signs a manifest or grant with different bytes.
- Processors never delete local data because the relay says so.
- Deploying the Worker or calling the real relay changes shared state: ask first.
- Never print or commit `.env`, `.env.test`, `medplum/.env` or any `HKA_FHIR_*` secret. Tests load made-up
  data only into the Medplum test project (`.env.test`), never the real one. `docker compose down -v` in
  `medplum/` deletes every loaded resource: ask first.

## FHIR

`docs/fhir-mapping.md` specifies the mapping. Values with no correct LOINC code use the `hka-metrics`
CodeSystem; every Observation also carries its HealthKit type in `healthkit-type`. Both live in
`cli/hka/terminology/` and are generated: to add a metric, add it to `METRICS` in `cli/hka/fhir.py` (with
definition, unit, value type and source), check any LOINC code against the NLM API first, bump
`MAPPING_VERSION`, run `python3 tools/fhir_terminology.py` and `python3 tools/fhir_examples.py`, update the
document, and validate (`tools/fhir-validate`). Never use a LOINC code that only looks right; the document
lists the rejected ones and why. Read a custom code's `definition` and `unit` before interpreting a value.
Date searches in Medplum must carry the member's UTC offset.

## Where things are

| Component | Entry points | Tests |
|---|---|---|
| Relay | `worker/src/app.ts` (router), `uploads.ts`, `enroll.ts`, `reads.ts`, `admin.ts`, `purge.ts` | `cd worker && npx vitest run` |
| Processor | `cli/hka/cli.py`, `pull.py` (verification), `build.py` (day folders), `days.py` (day rules, summary), `fhir.py` | `.venv/bin/python -m unittest discover -s cli/tests` |
| Admin | `cli/hka_admin/cli.py` | same |
| App core | `ios/HKACore/Sources/HKACore/` (`SyncEngine.swift` is the `Device` actor) | `cd ios/HKACore && swift test` |
| App | `ios/App/` | `xcodebuild … -sdk iphonesimulator CODE_SIGN_IDENTITY=- …` |
| FHIR | `cli/hka/fhir.py`, `cli/hka/terminology/`, `tools/fhir_terminology.py`, `tools/fhir_examples.py` | `cli/tests/test_fhir*.py` |
| Medplum | `medplum/docker-compose.yml`, `medplum/setup.py` (API on 8203) | `test_e2e_local.py` with the test project |
| CI | `.github/workflows/ci.yml` (required, Ubuntu), `macos.yml` (manual, self-hosted) | see `docs/ci.md` |

Node for the Worker and the FHIR validator: arm64 Node 24 (`worker/.nvmrc`). The Simulator test
(`cli/tests/test_e2e_simulator.py`) needs Xcode's first-launch components (`xcodebuild
-checkFirstLaunchStatus` exits 0; otherwise the owner runs `sudo xcodebuild -runFirstLaunch`): without them
the UI test runner never starts ("failed to bless service hub", "test runner hung before establishing
connection"). The test adds a seeded day to the Simulator's Health data; run it only when asked.
