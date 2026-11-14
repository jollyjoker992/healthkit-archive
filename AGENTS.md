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

## Where things are

| Component | Entry points | Tests |
|---|---|---|
| Relay | `worker/src/app.ts` (router), `uploads.ts`, `enroll.ts`, `reads.ts`, `admin.ts`, `purge.ts` | `cd worker && npx vitest run` |
| Processor | `cli/hka/cli.py`, `pull.py` (verification), `build.py` (day folders), `days.py` (day rules, summary), `fhir.py` | `.venv/bin/python -m unittest discover -s cli/tests` |
| Admin | `cli/hka_admin/cli.py` | same |
| App core | `ios/HKACore/Sources/HKACore/` (`SyncEngine.swift` is the `Device` actor) | `cd ios/HKACore && swift test` |
| App | `ios/App/` | `xcodebuild … -sdk iphonesimulator CODE_SIGN_IDENTITY=- …` |

Node for the Worker and the FHIR validator: arm64 Node 24 (`worker/.nvmrc`).
