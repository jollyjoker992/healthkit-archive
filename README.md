# healthkit-archive

A family-scale, end-to-end encrypted backup of Apple Health (HealthKit) data, ready for FHIR R4 and
AI health-advisor agents.

```
iPhone app ── encrypted + signed batches ──▶ Cloudflare Worker ──▶ R2 (ciphertext) + D1 (index)
   ▲  scans a processor card                      │
   │                                              ▼
hka (processor) ◀── downloads, verifies, decrypts
   └─ per-day folders → summary.json → FHIR R4 NDJSON → Medplum
```

- The phone encrypts each batch with `age` to the processors its owner chose, and signs it with a
  Secure Enclave key. The relay only ever holds ciphertext.
- There are no passwords or bearer tokens: devices, processors and the admin are identified by public
  keys; the relay keeps an allowlist.
- A processor verifies everything against keys its owner pinned by comparing fingerprints, so a
  dishonest relay can withhold data but not forge, reorder or swap it unnoticed.
- The encrypted inbox restores with the stock `age` tool and the processor's key file alone.

| Folder | What |
|---|---|
| [spec/](spec/README.md) | The protocol (normative), record formats, test vectors |
| [worker/](worker/README.md) | Relay: Cloudflare Worker, R2, D1 |
| [cli/](cli/README.md) | `hka` (processor: pull, verify, decrypt, day folders, FHIR) and `hka-admin` |
| [ios/](ios/README.md) | iOS app and its Swift core package |
| [docs/design.md](docs/design.md) | Design and decisions |
| [docs/fhir-mapping.md](docs/fhir-mapping.md) | FHIR R4 mapping |

## Tests

```sh
.venv/bin/python spec/tools/gen_vectors.py --check                  # vectors reproducible
(cd worker && npx vitest run)                                        # relay: spec vectors and behaviour
.venv/bin/python -m unittest discover -s cli/tests                   # processor and admin
(cd ios/HKACore && swift test)                                       # app core; age output checked with age and pyrage
HKA_E2E=1 HKA_NODE_BIN=~/.nvm/versions/node/v24.22.2/bin \
  .venv/bin/python -m unittest discover -s cli/tests -p test_e2e_local.py   # Swift device → Worker → Python
```

## Getting started

1. Deploy the relay ([worker/README.md](worker/README.md)) with the public key from `hka-admin init`.
2. On the processor machine: `hka init --name …`, back up `keys/age.txt` offline, and optionally make
   an offline recovery identity with `hka init --recovery`.
3. Build the app with your Apple team ([ios/README.md](ios/README.md)) and install it on each phone.
4. For each person: `hka-admin invite --name …`, scan it in the app, scan the processor card (and the
   recovery card), then `hka pull` and `hka confirm d_…` after comparing fingerprints.
5. Schedule `hka run` (and `hka fhir map && hka fhir load` if you use Medplum).
