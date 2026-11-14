# healthkit-archive

A family-scale, end-to-end encrypted backup of Apple Health (HealthKit) data, ready for FHIR R4 and
AI health-advisor agents.

## Why

Apple Health collects years of health data on each phone, but keeping it safe is left to the owner:
the built-in export is a manual, one-off dump. This project backs it up automatically and continuously,
so a lost phone, a reset or a forgotten export doesn't take the history with it.

It does so without handing the data to anyone. HealthKit exporters commonly send plaintext to a server
behind a bearer token, which means trusting whoever runs that server. Here the phone encrypts every
batch, the relay only stores ciphertext, and each person chooses who can decrypt it: no one but the
owner and the people they delegate to (and Apple, which holds the data in Health in the first place).

The longer aim is a personal health vault that lasts decades: wearable data alongside medical records
and documents, owned by the person it describes and readable by AI health-advisor agents through FHIR
R4. Decades of such data could also serve medical research, shared only when and how its owner
chooses. [garmin-archive](https://github.com/jollyjoker992/garmin-archive) does the same for Garmin
data, with the same day-folder layout and Medplum setup.

## How it works

```
iPhone app ── encrypted + signed batches ──▶ Cloudflare Worker ──▶ R2 (ciphertext) + D1 (index)
   ▲  scans a processor card                      │
   │                                              ▼
hka (processor) ◀── downloads, verifies, decrypts
   └─ per-day folders → summary.json → FHIR R4 NDJSON → Medplum
```

- The phone encrypts each batch with [age](https://age-encryption.org) to the processors its owner
  chose, and signs it with a Secure Enclave key. The relay only ever holds ciphertext.
- There are no passwords or bearer tokens: devices, processors and the admin are identified by public
  keys; the relay keeps an allowlist.
- A processor verifies everything against keys its owner pinned by comparing fingerprints, so a
  dishonest relay can withhold data but not forge, reorder or swap it unnoticed.
- The encrypted inbox restores with the stock `age` tool and the processor's key file alone.
- The processor writes plain per-day folders and maps them to FHIR R4 with documented codes
  ([docs/fhir-mapping.md](docs/fhir-mapping.md)), for a local Medplum and the agents that read it.

## Status

Early stage and in active development, built for one family by its admin. Formats, commands and the
app may still change. Use it with caution, and keep another copy of your Health data until it has
proven itself for you.

| Part | Tested by |
|---|---|
| Protocol, relay, processor, FHIR | Unit tests, shared test vectors in Swift, TypeScript and Python, and end-to-end tests: the real Worker under `wrangler dev`, the app's Swift core, the processor, and a real Medplum. The required CI runs the relay, processor, FHIR and Python end-to-end parts on every pull request and push to main; the Swift parts run in the manual macOS workflow. |
| iOS app | A UI test writes real HealthKit data in the Simulator and checks it through a local relay and the processor to Medplum. Checks that need a phone or a deployed relay are listed in [ios/README.md](ios/README.md) and [worker/README.md](worker/README.md). |

## Repository

| Folder | What |
|---|---|
| [spec/](spec/README.md) | The protocol (normative), record formats, test vectors |
| [worker/](worker/README.md) | Relay: Cloudflare Worker, R2, D1 |
| [cli/](cli/README.md) | `hka` (processor: pull, verify, decrypt, day folders, FHIR) and `hka-admin` |
| [ios/](ios/README.md) | iOS app (English and Vietnamese) and its Swift core package |
| [medplum/](medplum/README.md) | Local Medplum FHIR server (Docker Compose) |
| [docs/design.md](docs/design.md) | Design and decisions |
| [docs/fhir-mapping.md](docs/fhir-mapping.md) | FHIR R4 mapping: rules, codes, examples |
| [cli/hka/terminology/](cli/hka/terminology/), [fhir/examples/](fhir/examples/) | The `hka-metrics` and `healthkit-type` CodeSystems; example resources |
| [docs/ci.md](docs/ci.md) | GitHub Actions: required Ubuntu CI, manual self-hosted macOS workflow |
| [AGENTS.md](AGENTS.md) | Rules for coding agents (and people) changing this repository |

## Getting started

You need: a Cloudflare account (Workers, R2, D1), an Apple Account to sign the app (a free account works
for your own phones, with 7-day signing; the paid Apple Developer Program is needed for TestFlight or
year-long ad hoc builds; see [ios/README.md](ios/README.md)), a Mac with Xcode to build the app, and a machine for the processor
with Python 3.11 or later.

1. Deploy the relay ([worker/README.md](worker/README.md)) with the public key from `hka-admin init`.
2. On the processor machine: `hka init --name …`, back up `keys/age.txt` offline, and optionally make
   an offline recovery identity with `hka init --recovery`.
3. Build the app with your Apple team ([ios/README.md](ios/README.md)) and install it on each phone.
4. For each person: `hka-admin invite --name …`, scan it in the app, scan the processor card (and the
   recovery card), then `hka pull` and `hka confirm d_…` after comparing fingerprints.
5. Optional: start the local Medplum ([medplum/README.md](medplum/README.md)).
6. Schedule `hka run` (and `hka fhir map && hka fhir load` if you use Medplum).

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e cli      # hka, hka-admin, tests
(cd worker && npm ci)                                       # Node 24 (worker/.nvmrc)
(cd tools/fhir-validate && npm ci)                          # optional: Medplum's FHIR validator
```

```sh
.venv/bin/python spec/tools/gen_vectors.py --check                  # vectors reproducible
(cd worker && npx vitest run)                                        # relay: spec vectors and behaviour
.venv/bin/python -m unittest discover -s cli/tests                   # processor, admin, FHIR mapping
(cd ios/HKACore && swift test)                                       # app core; age output checked with age and pyrage
HKA_E2E=1 HKA_NODE_BIN="$(dirname "$(command -v node)")" \
  .venv/bin/python -m unittest discover -s cli/tests -p test_e2e_local.py   # Swift device → Worker → Python → Medplum
HKA_E2E_SIM=1 HKA_NODE_BIN="$(dirname "$(command -v node)")" \
  .venv/bin/python -m unittest discover -s cli/tests -p test_e2e_simulator.py   # real app + HealthKit in the Simulator
```

The FHIR steps of the end-to-end tests need the local Medplum and its test project
([medplum/README.md](medplum/README.md)); the Simulator test needs a Mac with Xcode's first-launch components installed (`sudo xcodebuild -runFirstLaunch`). The
required Ubuntu CI runs the vectors, relay, CLI, FHIR and Python end-to-end tests (with Medplum) on every
pull request and push to the main branch; the Swift core, app builds and both device end-to-end tests run
in the manual macOS workflow on a self-hosted Mac ([docs/ci.md](docs/ci.md)).

A change to the wire format changes `spec/README.md`, the vector generator and all three implementations
together; [AGENTS.md](AGENTS.md) lists this and the other rules.

## Privacy

Health data is decrypted only on processors the phone's owner added. The relay sees who uploads, when,
how much, and to which processors, but not the data. Processors keep plaintext day folders and a FHIR
copy on their own disk; protect that machine accordingly. Never commit `data/`, `.env`, `.env.test` or
key files (all ignored by git). Report security problems privately: [SECURITY.md](SECURITY.md).

## Disclaimer

This is a personal backup tool, not a medical device. It does not diagnose, treat or give medical
advice, and nothing it stores or maps has been clinically validated. Not affiliated with, endorsed by or
supported by Apple. Apple, Apple Health, HealthKit, iPhone and Apple Watch are trademarks of Apple Inc.

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for third-party content and attribution (LOINC,
Medplum).
